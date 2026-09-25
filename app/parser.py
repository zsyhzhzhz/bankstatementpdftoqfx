"""
银行对账单 PDF 解析模块。

策略（通用兼容多家银行）：
1. 优先用 pdfplumber 提取表格（extract_tables），按关键字识别表头列
   （日期 / 摘要 / 借方 / 贷方 / 金额 / 余额 / 收支类型等，中英文均支持）。
2. 如果没有可用的表格结构，回退为逐行文本 + 正则匹配交易行
   （日期 + 金额 出现在同一行的场景）。
3. 同时从全文中用正则提取账户信息（账号、户名、开户行、币种、
   起止日期、期初/期末余额），仅作为“猜测”供前端预填，允许用户在
   网页上修正——因为不同银行的对账单版式差异很大，无法保证 100% 命中。

解析结果不追求“自动完全正确”，而是把最有把握的猜测抛给前端，
由用户在可编辑表格里核对/修正后再生成 QFX，这样即使遇到没见过的
银行格式，也有兜底的编辑通道。
"""

from __future__ import annotations

import re
import io
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pdfplumber
from dateutil import parser as dateutil_parser


# ---------------------------------------------------------------------------
# 关键字词典（中英文），用于表头列识别
# ---------------------------------------------------------------------------

DATE_KEYWORDS = [
    "交易日期", "记账日期", "交易时间", "记账时间", "日期",
    "trans date", "transaction date", "posting date", "date",
]
DESC_KEYWORDS = [
    "摘要说明", "交易摘要", "交易说明", "交易描述", "摘要", "备注", "说明", "对方户名", "交易对方",
    "description", "detail", "memo", "narrative", "particulars",
]
DEBIT_KEYWORDS = [
    "支出金额", "借方金额", "支出", "借方", "出账金额", "付款金额",
    "debit", "withdrawal",
]
CREDIT_KEYWORDS = [
    "收入金额", "贷方金额", "收入", "贷方", "入账金额", "存入金额",
    "credit", "deposit",
]
AMOUNT_KEYWORDS = [
    "交易金额", "发生额", "金额",
    "amount",
]
BALANCE_KEYWORDS = [
    "账户余额", "余额", "结余",
    "balance",
]
TYPE_KEYWORDS = [
    "收支类型", "收支", "交易类型", "类型",
    "type",
]
REF_KEYWORDS = [
    "流水号", "交易单号", "凭证号", "单号", "交易流水号",
    "reference", "ref no", "ref.no",
]

INCOME_MARKS = {"收入", "贷", "存入", "credit", "in"}
EXPENSE_MARKS = {"支出", "借", "取出", "debit", "out"}


def _norm(s: Optional[str]) -> str:
    if s is None:
        return ""
    return re.sub(r"\s+", "", str(s)).strip().lower()


def _match_keyword(cell: str, keywords: list[str]) -> bool:
    c = _norm(cell)
    if not c:
        return False
    for k in keywords:
        if _norm(k) in c:
            return True
    return False


@dataclass
class Transaction:
    date_raw: str
    date_iso: Optional[str]  # YYYY-MM-DD，解析失败则为 None
    description: str
    amount: Optional[float]  # 有符号：收入为正，支出为负
    balance: Optional[float] = None
    ref: Optional[str] = None
    source: str = ""  # 调试用：来自哪个表格/行


@dataclass
class AccountInfo:
    bank_name: str = ""
    holder_name: str = ""
    account_number: str = ""
    bank_id: str = ""  # 开户行/联行号，储蓄卡账户可选
    currency: str = "CNY"
    account_type_guess: str = "BANK"  # BANK 或 CC
    statement_start: Optional[str] = None
    statement_end: Optional[str] = None
    opening_balance: Optional[float] = None
    closing_balance: Optional[float] = None


@dataclass
class ParseResult:
    account: AccountInfo
    transactions: list[Transaction] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw_text_preview: str = ""


# ---------------------------------------------------------------------------
# 数字/日期解析辅助函数
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"^-?[\d,]+\.?\d*$")


def parse_number(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    s = str(s).strip()
    if not s or s in {"-", "--", "—"}:
        return None
    neg = False
    # 中式负数标记：括号、"借"字后缀等常见写法
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1]
        neg = True
    s = s.replace(",", "").replace("¥", "").replace("￥", "").replace("$", "").strip()
    s = s.replace("+", "")
    if s.endswith("-"):
        neg = True
        s = s[:-1]
    if s.startswith("-"):
        neg = True
        s = s[1:]
    if not _NUM_RE.match(s.replace("-", "")):
        # 尝试去掉非数字字符后再判断
        cleaned = re.sub(r"[^\d.]", "", s)
        if not cleaned:
            return None
        s = cleaned
    try:
        val = float(s)
    except ValueError:
        return None
    return -val if neg else val


_CN_DATE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?")
_YMD_RE = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
_MDY_RE = re.compile(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})")
_YMD_COMPACT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")


def parse_date(s: Optional[str]) -> Optional[str]:
    """尽量把各种日期写法解析为 YYYY-MM-DD，失败返回 None。"""
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None

    m = _CN_DATE_RE.search(s)
    if m:
        y, mo, d = m.groups()
        try:
            return datetime(int(y), int(mo), int(d)).strftime("%Y-%m-%d")
        except ValueError:
            pass

    m = _YMD_RE.search(s)
    if m:
        y, mo, d = m.groups()
        try:
            return datetime(int(y), int(mo), int(d)).strftime("%Y-%m-%d")
        except ValueError:
            pass

    m = _YMD_COMPACT_RE.match(s)
    if m:
        y, mo, d = m.groups()
        try:
            return datetime(int(y), int(mo), int(d)).strftime("%Y-%m-%d")
        except ValueError:
            pass

    m = _MDY_RE.search(s)
    if m:
        a, b, y = m.groups()
        # 优先按月/日/年尝试，不行再按日/月/年
        for mo, d in ((a, b), (b, a)):
            try:
                return datetime(int(y), int(mo), int(d)).strftime("%Y-%m-%d")
            except ValueError:
                continue

    try:
        dt = dateutil_parser.parse(s, fuzzy=True, dayfirst=False)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, OverflowError):
        return None


# ---------------------------------------------------------------------------
# 表头识别
# ---------------------------------------------------------------------------

def _identify_header(row: list[str]) -> Optional[dict]:
    """给定一行单元格，尝试判断是否为表头行，并返回列索引映射。"""
    mapping: dict[str, int] = {}
    hits = 0
    for idx, cell in enumerate(row):
        if cell is None:
            continue
        if "date" not in mapping and _match_keyword(cell, DATE_KEYWORDS):
            mapping["date"] = idx
            hits += 1
            continue
        if "desc" not in mapping and _match_keyword(cell, DESC_KEYWORDS):
            mapping["desc"] = idx
            hits += 1
            continue
        if "debit" not in mapping and _match_keyword(cell, DEBIT_KEYWORDS):
            mapping["debit"] = idx
            hits += 1
            continue
        if "credit" not in mapping and _match_keyword(cell, CREDIT_KEYWORDS):
            mapping["credit"] = idx
            hits += 1
            continue
        if "amount" not in mapping and _match_keyword(cell, AMOUNT_KEYWORDS):
            mapping["amount"] = idx
            hits += 1
            continue
        if "balance" not in mapping and _match_keyword(cell, BALANCE_KEYWORDS):
            mapping["balance"] = idx
            hits += 1
            continue
        if "type" not in mapping and _match_keyword(cell, TYPE_KEYWORDS):
            mapping["type"] = idx
            hits += 1
            continue
        if "ref" not in mapping and _match_keyword(cell, REF_KEYWORDS):
            mapping["ref"] = idx
            hits += 1
            continue
    # 至少要能同时定位到“日期”和（描述 或 金额/借贷）才认为是有效表头
    has_date = "date" in mapping
    has_value = any(k in mapping for k in ("amount", "debit", "credit"))
    if has_date and has_value and hits >= 2:
        return mapping
    return None


def _row_to_transaction(row: list[str], mapping: dict, source: str) -> Optional[Transaction]:
    def cell(key: str) -> Optional[str]:
        idx = mapping.get(key)
        if idx is None or idx >= len(row):
            return None
        return row[idx]

    date_raw = cell("date") or ""
    date_iso = parse_date(date_raw)
    desc = (cell("desc") or "").replace("\n", " ").strip()

    amount = None
    debit_val = parse_number(cell("debit"))
    credit_val = parse_number(cell("credit"))
    amount_val = parse_number(cell("amount"))
    type_val = _norm(cell("type"))

    if debit_val is not None or credit_val is not None:
        c = credit_val or 0.0
        d = debit_val or 0.0
        amount = c - abs(d)
    elif amount_val is not None:
        amount = amount_val
        if type_val:
            if any(m in type_val for m in EXPENSE_MARKS) and amount > 0:
                amount = -amount
            elif any(m in type_val for m in INCOME_MARKS) and amount < 0:
                amount = abs(amount)

    balance = parse_number(cell("balance"))
    ref = (cell("ref") or "").strip() or None

    if not date_iso and not desc and amount is None:
        return None  # 空行

    return Transaction(
        date_raw=date_raw,
        date_iso=date_iso,
        description=desc or "(无描述)",
        amount=amount,
        balance=balance,
        ref=ref,
        source=source,
    )


# ---------------------------------------------------------------------------
# 账户信息提取（正则，仅作猜测）
# ---------------------------------------------------------------------------

_ACCOUNT_NO_PATTERNS = [
    r"账\s*号[:：]\s*([\dXx\*]{4}(?:[\s-]?[\dXx\*]{2,6}){1,6})",
    r"卡\s*号[:：]\s*([\dXx\*]{4}(?:[\s-]?[\dXx\*]{2,6}){1,6})",
    r"Account\s*(?:No\.?|Number)[:：]?\s*([\dXx\*]{4}(?:[\s-]?[\dXx\*]{2,6}){1,6})",
    # 部分信用社账单把账号写在"...SUMMARY | 254521193"这种用竖线分隔的标题行里
    r"SUMMARY\s*\|\s*(\d{5,20})\b",
]
_HOLDER_PATTERNS = [
    r"户\s*名[:：]\s*([^\s，,。\n]{2,20})",
    r"客户名称[:：]\s*([^\s，,。\n]{2,20})",
    r"Account\s*Holder[:：]?\s*([A-Za-z ,.'-]{2,40})",
]
_BANK_PATTERNS = [
    r"([\u4e00-\u9fa5]{2,10}银行)",
]
_PERIOD_PATTERNS = [
    r"(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)\s*[-~至到]\s*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)",
    # 英文写法，如 "August 01, 2026 through August 31, 2026"
    r"([A-Za-z]{3,9}\.?\s+\d{1,2},?\s*\d{4})\s*(?:through|thru|to)\s*([A-Za-z]{3,9}\.?\s+\d{1,2},?\s*\d{4})",
    # "06/22/2026 through 06/30/2026" 这种纯数字日期写法（常见于信用社账单）
    r"(\d{1,2}/\d{1,2}/\d{4})\s*(?:through|thru|to)\s*(\d{1,2}/\d{1,2}/\d{4})",
]
_OPEN_BAL_PATTERNS = [
    r"期初余额[:：]?\s*([\d,]+\.?\d*)",
    r"上期余额[:：]?\s*([\d,]+\.?\d*)",
    r"Opening\s*Balance[:：]?\s*\$?([\d,]+\.?\d*)",
    r"Beginning\s*Balance[:：]?\s*(?:\d+\s+)?\$?([\d,]+\.?\d*)",
    # Bank of America 等写法："Beginning balance on February 1, 2026 $203,079.96"
    # 余额金额前面夹了一段日期文字，用排除 "$" 和换行的字符类跳过它。
    r"Beginning\s*balance\b[^$\n]*\$\s*([\d,]+\.\d{2})",
]
_CLOSE_BAL_PATTERNS = [
    r"期末余额[:：]?\s*([\d,]+\.?\d*)",
    r"本期余额[:：]?\s*([\d,]+\.?\d*)",
    r"Closing\s*Balance[:：]?\s*\$?([\d,]+\.?\d*)",
    r"Ending\s*Balance[:：]?\s*(?:\d+\s+)?\$?([\d,]+\.?\d*)",
    # 同上，兼容 "Ending balance on February 28, 2026 $234,996.54" 这种写法。
    r"Ending\s*balance\b[^$\n]*\$\s*([\d,]+\.\d{2})",
]
# 部分信用社账单把"上期余额/本期贷方合计/本期借方合计/本期余额"四个标签
# 集中写在一起，再把对应的四个金额集中写在后面（标签和数值不是紧挨着的），
# 例如："Previous Statement Balance Total Credits Total Debits Current
# Statement Balance $0.00 $47,866.50 $1,486.06 $46,380.44"。用一个专门的
# 正则一次性把四个金额按顺序取出来，比按标签就近匹配更可靠。
_SUMMARY_4VALUE_RE = re.compile(
    r"Previous\s*Statement\s*Balance[\s\S]{0,120}?Total\s*Credits[\s\S]{0,120}?"
    r"Total\s*Debits[\s\S]{0,120}?Current\s*Statement\s*Balance[\s|]*\$?([\d,]+\.\d{2})"
    r"[\s|]*\$?([\d,]+\.\d{2})[\s|]*\$?([\d,]+\.\d{2})[\s|]*\$?([\d,]+\.\d{2})",
    re.IGNORECASE,
)

_CC_HINTS = [
    "信用卡账单", "信用卡对账单", "信用卡明细", "信用卡账户", "信用卡交易明细",
    "贷记卡对账单", "貸記卡對賬單", "credit card statement", "credit card account",
]

# 常见英文银行/信用社名称（按长度从长到短匹配，优先命中更具体的全称）
_KNOWN_EN_BANK_NAMES = sorted(
    [
        "JPMorgan Chase Bank, N.A.", "JPMorgan Chase Bank", "Chase Bank", "Chase",
        "Bank of America, N.A.", "Bank of America",
        "Wells Fargo Bank, N.A.", "Wells Fargo",
        "Citibank, N.A.", "Citibank",
        "U.S. Bank", "US Bank",
        "Capital One", "PNC Bank", "TD Bank", "Truist Bank", "Regions Bank",
        "Randolph-Brooks Federal Credit Union", "RBFCU",
    ],
    key=len,
    reverse=True,
)


def extract_account_info(full_text: str) -> AccountInfo:
    info = AccountInfo()

    for pat in _ACCOUNT_NO_PATTERNS:
        m = re.search(pat, full_text, re.IGNORECASE)
        if m:
            info.account_number = re.sub(r"[\s-]", "", m.group(1))
            break

    for pat in _HOLDER_PATTERNS:
        m = re.search(pat, full_text, re.IGNORECASE)
        if m:
            info.holder_name = m.group(1).strip()
            break

    for pat in _BANK_PATTERNS:
        m = re.search(pat, full_text)
        if m:
            info.bank_name = m.group(1)
            break
    if not info.bank_name:
        for name in _KNOWN_EN_BANK_NAMES:
            if name in full_text:
                info.bank_name = name
                break

    for pat in _PERIOD_PATTERNS:
        m = re.search(pat, full_text)
        if m:
            info.statement_start = parse_date(m.group(1))
            info.statement_end = parse_date(m.group(2))
            break

    for pat in _OPEN_BAL_PATTERNS:
        m = re.search(pat, full_text, re.IGNORECASE)
        if m:
            info.opening_balance = parse_number(m.group(1))
            break

    # 期末余额取"最后一次"匹配而不是第一次：像本次遇到的扫描件那样，如果
    # 一份 PDF 实际上是把好几个月的对账单前后拼接在一起（每个月都各自打
    # 印一次"Ending balance"），第一次出现的其实是第一个月的期末余额，
    # 真正代表"整份文件最终余额"的是最后一次出现的那个数字。
    for pat in _CLOSE_BAL_PATTERNS:
        matches = list(re.finditer(pat, full_text, re.IGNORECASE))
        if matches:
            info.closing_balance = parse_number(matches[-1].group(1))
            break

    if info.opening_balance is None or info.closing_balance is None:
        sm = _SUMMARY_4VALUE_RE.search(full_text)
        if sm:
            if info.opening_balance is None:
                info.opening_balance = parse_number(sm.group(1))
            if info.closing_balance is None:
                info.closing_balance = parse_number(sm.group(4))

    has_rmb_symbol = ("¥" in full_text) or ("￥" in full_text) or ("人民币" in full_text) or ("RMB" in full_text.upper())
    has_usd_hint = ("$" in full_text) or ("USD" in full_text.upper())
    # 银行名称精确匹配到我们认识的美国银行/信用社，是比"¥"/"$"符号更可靠
    # 的信号：像扫描件 OCR 这种噪声很大的文本，偶尔会把某个笔画误认成
    # "¥"，如果只看符号很容易被这种误判带偏，所以银行名匹配优先。
    is_known_us_bank = info.bank_name in _KNOWN_EN_BANK_NAMES
    if is_known_us_bank:
        info.currency = "USD"
    elif has_rmb_symbol and not has_usd_hint:
        info.currency = "CNY"
    elif has_usd_hint and not has_rmb_symbol:
        info.currency = "USD"
    elif has_usd_hint:
        # 两种符号都出现时（少见），优先信任更明确的 RMB/人民币 字样
        info.currency = "CNY" if has_rmb_symbol else "USD"
    else:
        info.currency = "CNY"

    low = full_text.lower()
    if any(h.lower() in low for h in _CC_HINTS):
        info.account_type_guess = "CC"

    return info


# ---------------------------------------------------------------------------
# “分节叙述式”对账单解析（常见于美国银行，如 Chase / Bank of America）
#
# 这类 PDF 不是规整的网格表格，而是按“DEPOSITS AND ADDITIONS / CHECKS PAID /
# ELECTRONIC WITHDRAWALS / FEES”等小节组织的纯文本列表：每笔交易第一行以
# 「MM/DD 日期」开头、以金额结尾，后面可能跟着若干行没有日期/金额的换行
# 续行（附言、备注等）。用小节标题判断收入/支出符号，用“日期在行首附近 +
# 金额在行尾”识别每笔交易的起始行，其余行归并为描述的延续。
# ---------------------------------------------------------------------------

# 小节标题 -> 该小节内交易金额的符号（+1 收入 / -1 支出）。
# 需要与整行（去除首尾空白、大写、合并连续空格、去掉末尾的"(CONTINUED)"）
# 完全一致才算命中，避免把普通描述文字里偶然出现的相同词语误判为小节标题。
SECTION_SIGN: dict[str, int] = {
    "DEPOSITS AND ADDITIONS": 1,
    "DEPOSITS AND OTHER CREDITS": 1,
    "DEPOSITS AND OTHER ADDITIONS": 1,
    "ELECTRONIC DEPOSITS": 1,
    "CREDITS": 1,
    "OTHER CREDITS": 1,
    "CHECKS PAID": -1,
    "CHECKS": -1,
    "ELECTRONIC WITHDRAWALS": -1,
    "WITHDRAWALS AND OTHER DEBITS": -1,
    "OTHER WITHDRAWALS": -1,
    "ATM & DEBIT CARD WITHDRAWALS": -1,
    "ATM AND DEBIT CARD WITHDRAWALS": -1,
    "CARD WITHDRAWALS": -1,
    "DEBIT CARD WITHDRAWALS": -1,
    "FEES": -1,
    "SERVICE FEES": -1,
    "OTHER FEES": -1,
    "WITHDRAWALS": -1,
}
# 遇到这些小节标题（或 *start*/*end* 标记）就退出当前的交易小节，
# 避免把余额表/支票影像列表等非交易内容误当成交易。
SECTION_STOP = [
    "DAILY ENDING BALANCE", "IMAGES", "CHECKING SUMMARY", "ACCOUNT SUMMARY",
    "SAVINGS SUMMARY", "SERVICE CHARGE SUMMARY", "OVERDRAFT PROTECTION",
    "MONTHLY SERVICE FEE",
]
_STOP_HEADER_HINT_RE = re.compile(r"BALANC|SUMMARY|\bIMAGES?\b")
_CONTINUED_SUFFIX_RE = re.compile(r"\s*[-\u2013\u2014]?\s*\(?CONTINUED\)?\s*$", re.IGNORECASE)


def _norm_header_line(line: str) -> str:
    line = _CONTINUED_SUFFIX_RE.sub("", line.strip().upper())
    return re.sub(r"\s+", " ", line).strip()


LINE_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})\b")
LINE_TRAILING_AMOUNT_RE = re.compile(r"([+-]?\$?[\d,]+\.\d{2})\s*$")
# 常见 ACH 元数据里对方姓名/公司名的字段，不同银行写法不同：Chase 用
# "Ind Name:"，Bank of America 用 "INDN:"，尽量提取出来做更干净的描述。
_IND_NAME_RE = re.compile(r"(?:Ind\s*Name|INDN)\s*:?\s*([^\n]+?)(?:\s+CO\b|\s+ID:|\s+Trn:|$)", re.IGNORECASE)
_LEADING_CHECK_NO_RE = re.compile(r"^(\d{3,7})\b")
_TOTAL_LINE_RE = re.compile(r"^total\b", re.IGNORECASE)
_START_END_MARK_RE = re.compile(r"^\*(?:start|end)\*", re.IGNORECASE)
# 有些 PDF 里存在与可见文字重叠的隐藏标记文字（如无障碍标签"*end*checks
# paid section*"），个别 PDF 引擎（如 pdfplumber）按字符坐标重新排序时会把
# 这段隐藏标记和紧挨着的真实交易行"糅"在一起（例如把支票号/日期/金额的
# 字符插进了标记文字里）。真正的标记行本身都很短，一旦一行长度明显超过
# 正常标记的长度，很可能是被这种重叠糅合污染过的真实交易行，不能直接当
# 标记跳过，要让它继续走正常的日期/金额识别逻辑（哪怕支票号可能因此错位）。
_MARKER_LINE_MAX_LEN = 40
# 有些银行（如 Bank of America）为了省地方，把支票列表排成两栏，一行里塞了
# 两笔"日期 支票号 金额"（顺序可能是 日期→支票号→金额，也可能是 支票号→
# 日期→金额，如 Chase 的单栏格式），用 finditer 一次性把一行里出现的所有
# 笔数都取出来，而不是像普通交易行那样只取“行首日期 + 行尾金额”一组。
_CHECK_ROW_RE = re.compile(
    r"(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s+(\d{3,7})\*?\s+(-?\$?[\d,]+\.\d{2})"
    r"|(\d{3,7})\s*[*^]*\s+(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s+(-?\$?[\d,]+\.\d{2})"
)


def _to_iso(year: int, month: int, day: int) -> Optional[str]:
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _infer_year(month: int, account: AccountInfo) -> Optional[int]:
    """大多数美国银行对账单里每笔交易只写“MM/DD”不带年份，需要结合账单周期
    （账户信息里识别出的起止日期）推断出正确的年份。"""
    sy = int(account.statement_start[0:4]) if account.statement_start else None
    sm = int(account.statement_start[5:7]) if account.statement_start else None
    ey = int(account.statement_end[0:4]) if account.statement_end else None
    em = int(account.statement_end[5:7]) if account.statement_end else None
    if sm is not None and month == sm:
        return sy
    if em is not None and month == em:
        return ey
    if sy is not None and ey is not None:
        return sy if (sy == ey or month >= (sm or 1)) else ey
    if sy is not None:
        return sy
    if ey is not None:
        return ey
    return datetime.now().year


def parse_sectioned_statement(full_text: str, account: AccountInfo) -> list[Transaction]:
    lines = full_text.split("\n")
    txs: list[Transaction] = []
    sign: Optional[int] = None
    section_name: Optional[str] = None
    cur: Optional[dict] = None

    def flush() -> None:
        nonlocal cur
        if cur:
            desc = re.sub(r"\s{2,}", " ", " ".join(cur["descParts"])).strip()
            ind_match = _IND_NAME_RE.search(desc)
            check_no_match = _LEADING_CHECK_NO_RE.match(desc)
            if ind_match and ind_match.group(1).strip():
                desc = ind_match.group(1).strip()
            elif cur["isCheck"] and check_no_match:
                desc = f"Check #{check_no_match.group(1)}"
            if not desc:
                desc = "Check" if cur["isCheck"] else "Transaction"
            txs.append(
                Transaction(
                    date_raw=cur["dateRaw"],
                    date_iso=cur["dateIso"],
                    description=desc[:200],
                    amount=cur["sign"] * abs(cur["amount"]),
                    balance=None,
                    ref=None,
                    source="sectioned",
                )
            )
        cur = None

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        norm = _norm_header_line(line)

        if norm in SECTION_SIGN:
            flush()
            sign = SECTION_SIGN[norm]
            section_name = norm
            continue

        # 安全网：除了精确匹配的标题列表外，任何看起来像“余额表/汇总/影像”类
        # 的标题行（不同银行措辞差异很大，如 "Daily ledger balances"、"DAILY
        # ENDING BALANCE"、"Checking Summary" 等）都强制结束当前小节，避免把
        # 这些非交易的数字表格误吞进上一个小节（这类表格里全是"日期+金额"，
        # 一旦被当成交易会产生离谱的错误汇总）。
        looks_like_stop_header = norm in SECTION_STOP or (
            len(norm) < 60 and _STOP_HEADER_HINT_RE.search(norm) and not re.search(r"\d", norm)
        )
        is_marker_line = _START_END_MARK_RE.match(line) and len(line) <= _MARKER_LINE_MAX_LEN
        if looks_like_stop_header or is_marker_line:
            flush()
            sign = None
            section_name = None
            continue

        if sign is None:
            continue  # 不在已识别的交易小节内，忽略

        if _TOTAL_LINE_RE.match(line):
            flush()
            sign = None
            section_name = None
            continue

        # "支票"小节常见的两栏并排格式：一行里有两笔支票记录，需要用专门的
        # 正则一次性取出这一行里所有的（日期, 支票号, 金额）组合。
        if section_name in ("CHECKS", "CHECKS PAID"):
            check_row_matches = list(_CHECK_ROW_RE.finditer(line))
            if check_row_matches:
                flush()
                for cm in check_row_matches:
                    date_str = cm.group(1) if cm.group(1) is not None else cm.group(5)
                    check_no = cm.group(2) if cm.group(1) is not None else cm.group(4)
                    amount_str = cm.group(3) if cm.group(1) is not None else cm.group(6)
                    dm = LINE_DATE_RE.search(date_str)
                    if not dm:
                        continue
                    mm, dd = int(dm.group(1)), int(dm.group(2))
                    year = _infer_year(mm, account)
                    date_iso = _to_iso(year, mm, dd) if year else None
                    amt = parse_number(amount_str) or 0
                    txs.append(
                        Transaction(
                            date_raw=date_str,
                            date_iso=date_iso,
                            description=f"Check #{check_no}",
                            amount=sign * abs(amt),
                            balance=None,
                            ref=None,
                            source="sectioned",
                        )
                    )
                continue

        date_match = LINE_DATE_RE.search(line)
        amount_match = LINE_TRAILING_AMOUNT_RE.search(line)

        if date_match and amount_match and date_match.start() < 20 and date_match.start() < amount_match.start():
            flush()
            mm, dd = int(date_match.group(1)), int(date_match.group(2))
            year = _infer_year(mm, account)
            date_iso = _to_iso(year, mm, dd) if year else None
            rest = line[: amount_match.start()] + line[amount_match.end():]
            rest = rest.replace(date_match.group(0), " ", 1).strip()
            cur = {
                "dateRaw": date_match.group(0),
                "dateIso": date_iso,
                "amount": parse_number(amount_match.group(1)) or 0,
                "sign": sign,
                "descParts": [rest],
                "isCheck": section_name in ("CHECKS PAID", "CHECKS"),
            }
        elif cur:
            cur["descParts"].append(line)
        # 既不是新交易起始行、又没有正在处理的交易时，说明是小节内的列头/说明文字，忽略即可。

    flush()
    return txs


# ---------------------------------------------------------------------------
# 双栏“左右并排”式对账单解析（如部分信用社账单：一页里把交易列表排成
# 左右两栏，表头写作 "Date Description Amount Date Description Amount"，
# 支票列表写作 "Check Amount Date Check Amount Date"）。
#
# 这类 PDF 按视觉行拼接文本会把同一行里的左右两笔交易错误拼接，所以改用
# pdfplumber 的 extract_words(use_text_flow=True) 得到的“原始文本流顺序平铺
# 全文”（flat_text），其顺序天然是“一条完整交易的全部文字，紧跟着下一条
# 完整交易的全部文字”，可以直接用“日期 → 描述 → 金额”的状态机识别，不需要
# 关心左右栏。好消息是这类对账单里金额本身通常已经带好正负号（存入为正/
# 无符号，支出为 "-$xx.xx"），不需要再按小节名称推断收支方向。
# ---------------------------------------------------------------------------

_TWO_COL_TX_HEADER_RE = re.compile(r"Date\s*Description\s*Amount\s*Date\s*Description\s*Amount", re.IGNORECASE)
_TWO_COL_CHECK_HEADER_RE = re.compile(r"Check\s*Amount\s*Date\s*Check\s*Amount\s*Date", re.IGNORECASE)
_TWO_COL_SECTION_STOP_RE = re.compile(r"rbfcu\.org|\bNote:|\bOther Information\b|\bPAGE\s*\d+\s*of\s*\d+", re.IGNORECASE)
_TWO_COL_TX_ENTRY_RE = re.compile(r"(\d{1,2}/\d{1,2})\s+([\s\S]*?)\s*(-?\$[\d,]+\.\d{2})(?=\s|$)")
_TWO_COL_CHECK_ENTRY_RE = re.compile(r"\b(\d{3,7})\s+(-?\$[\d,]+\.\d{2})\s+(\d{1,2}/\d{1,2})\b")


def looks_like_two_column_statement(flat_text: str) -> bool:
    return bool(_TWO_COL_TX_HEADER_RE.search(flat_text) or _TWO_COL_CHECK_HEADER_RE.search(flat_text))


def _find_two_col_header_positions(flat_text: str) -> list[dict]:
    positions = []
    for m in _TWO_COL_TX_HEADER_RE.finditer(flat_text):
        positions.append({"type": "tx", "start": m.start(), "end": m.end()})
    for m in _TWO_COL_CHECK_HEADER_RE.finditer(flat_text):
        positions.append({"type": "check", "start": m.start(), "end": m.end()})
    positions.sort(key=lambda p: p["start"])
    return positions


def parse_two_column_statement(flat_text: str, account: AccountInfo) -> list[Transaction]:
    txs: list[Transaction] = []
    headers = _find_two_col_header_positions(flat_text)
    for i, h in enumerate(headers):
        seg_end = headers[i + 1]["start"] if i + 1 < len(headers) else len(flat_text)
        stop_match = _TWO_COL_SECTION_STOP_RE.search(flat_text[h["end"]:seg_end])
        if stop_match:
            seg_end = h["end"] + stop_match.start()
        segment = flat_text[h["end"]:seg_end]

        if h["type"] == "check":
            for m in _TWO_COL_CHECK_ENTRY_RE.finditer(segment):
                check_no, amount_str, date_raw = m.group(1), m.group(2), m.group(3)
                amount = parse_number(amount_str)
                dm = LINE_DATE_RE.search(date_raw)
                if not dm or amount is None:
                    continue
                mm, dd = int(dm.group(1)), int(dm.group(2))
                year = _infer_year(mm, account)
                txs.append(
                    Transaction(
                        date_raw=date_raw,
                        date_iso=_to_iso(year, mm, dd) if year else None,
                        description=f"Check #{check_no}",
                        amount=amount,
                        balance=None,
                        ref=None,
                        source="two_col",
                    )
                )
        else:
            for m in _TWO_COL_TX_ENTRY_RE.finditer(segment):
                date_raw = m.group(1)
                desc = re.sub(r"\s{2,}", " ", m.group(2)).strip()
                amount = parse_number(m.group(3))
                dm = LINE_DATE_RE.search(date_raw)
                if not dm or amount is None:
                    continue
                mm, dd = int(dm.group(1)), int(dm.group(2))
                year = _infer_year(mm, account)
                txs.append(
                    Transaction(
                        date_raw=date_raw,
                        date_iso=_to_iso(year, mm, dd) if year else None,
                        description=(desc or "Transaction")[:200],
                        amount=amount,
                        balance=None,
                        ref=None,
                        source="two_col",
                    )
                )
    return txs


# ---------------------------------------------------------------------------
# 文本行兜底解析（没有表格结构时）
# ---------------------------------------------------------------------------

_LINE_TX_RE = re.compile(
    r"(?P<date>\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{4}年\d{1,2}月\d{1,2}日)"
    r"[\s\S]{0,80}?"
    r"(?P<amount>-?[\d,]+\.\d{2})"
)


def _parse_lines_fallback(text: str) -> list[Transaction]:
    txs = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _LINE_TX_RE.search(line)
        if not m:
            continue
        date_raw = m.group("date")
        amount = parse_number(m.group("amount"))
        desc = line.replace(m.group("date"), "").replace(m.group("amount"), "").strip()
        desc = re.sub(r"\s{2,}", " ", desc)
        txs.append(
            Transaction(
                date_raw=date_raw,
                date_iso=parse_date(date_raw),
                description=desc[:120] or "(无描述)",
                amount=amount,
                source="text-fallback",
            )
        )
    return txs


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def parse_pdf(file_bytes: bytes) -> ParseResult:
    warnings: list[str] = []
    all_text_parts: list[str] = []
    all_flat_parts: list[str] = []
    transactions: list[Transaction] = []
    table_found = False

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page_idx, page in enumerate(pdf.pages):
            page_text = page.extract_text() or ""
            all_text_parts.append(page_text)

            # 原始文本流顺序的“平铺全文”，专门给双栏对账单解析用（见
            # parse_two_column_statement 的说明）：use_text_flow=True 让
            # pdfplumber 按 PDF 内容流的原始顺序返回文字，而不是按视觉位置
            # 重新排序，这样同一视觉行里左右两栏的内容不会被交叉打乱。
            try:
                words = page.extract_words(use_text_flow=True, keep_blank_chars=False)
                all_flat_parts.append(" ".join(w["text"] for w in words))
            except Exception:  # noqa: BLE001
                pass

            try:
                tables = page.extract_tables()
            except Exception as e:  # noqa: BLE001
                warnings.append(f"第 {page_idx + 1} 页表格提取出错：{e}")
                tables = []

            for t_idx, table in enumerate(tables):
                if not table or len(table) < 2:
                    continue
                header_mapping = None
                header_row_idx = None
                # 表头可能在前几行里（有些 PDF 会有合并单元格/多行表头）
                for r_idx in range(min(3, len(table))):
                    mapping = _identify_header(table[r_idx])
                    if mapping:
                        header_mapping = mapping
                        header_row_idx = r_idx
                        break
                if not header_mapping:
                    continue
                table_found = True
                source = f"p{page_idx + 1}-t{t_idx + 1}"
                for row in table[header_row_idx + 1:]:
                    if row is None:
                        continue
                    tx = _row_to_transaction(row, header_mapping, source)
                    if tx:
                        transactions.append(tx)

    full_text = "\n".join(all_text_parts)
    flat_text = " ".join(all_flat_parts)

    # 整份 PDF 都提取不出任何文字，基本可以判定是扫描件/图片型 PDF（没有
    # 文字层，pdfplumber 天然提取不到任何字符），这种情况下前面的表格/文本
    # 解析注定一无所获，改用 OCR 兜底（见 app/ocr.py），而不是继续往下走。
    if not full_text.strip() and not flat_text.strip():
        from . import ocr as _ocr  # 延迟导入，避免让"没用到 OCR"的场景也强制依赖 pymupdf

        return _ocr.parse_scanned_pdf(file_bytes)

    account = extract_account_info(full_text)

    def _valid_count(items: list[Transaction]) -> int:
        return sum(1 for t in items if t.date_iso and t.amount is not None)

    table_valid_count = _valid_count(transactions) if table_found else -1
    sectioned = parse_sectioned_statement(full_text, account)
    sectioned_valid_count = _valid_count(sectioned)
    # 有些信用社/银行把交易列表排成左右两栏，这是最强的结构信号：一旦命中，
    # 且产出的有效交易数不比另外两种方式差，优先采用双栏解析结果——但不能
    # 无条件信任（某个小节恰好没有交易导致上下两个表头紧挨在一起，也会被
    # 误判为双栏表头），所以仍需要跟另外两种解析方式比较数量。
    looks_two_column = looks_like_two_column_statement(flat_text)
    two_column = parse_two_column_statement(flat_text, account) if looks_two_column else []
    two_column_valid_count = _valid_count(two_column)
    # 全文里出现了我们认识的“存款/支票/扣款”类小节标题，就当作强信号：这是
    # 分节叙述式对账单，直接采用分节解析结果，不再跟表格重建的结果比数量——
    # 因为叙述式 PDF 的表头文字（如 "DATE DESCRIPTION AMOUNT"）在每一页顶部
    # 都重复出现，很容易被表格提取逻辑误当成一份“规整表格”，进而把说明性
    # 续行也拆成一行、甚至把页码/账号里的数字误拼成天文数字般的金额。
    upper_full_text = full_text.upper()
    looks_sectioned = any(h in upper_full_text for h in SECTION_SIGN)

    if two_column_valid_count > 0 and two_column_valid_count >= max(table_valid_count, sectioned_valid_count):
        transactions = two_column
        if not table_found or table_valid_count <= 0:
            warnings.append("识别到左右两栏排版的交易列表，已按“日期/描述/金额”双栏格式解析，请重点核对交易日期的年份是否正确。")
    elif sectioned_valid_count > 0 and (looks_sectioned or sectioned_valid_count >= table_valid_count):
        transactions = sectioned
        if not table_found or table_valid_count <= 0:
            warnings.append("未能识别出规整的交易表格，已按“存款/支票/扣款”等分节文本格式解析，请重点核对交易日期的年份和金额正负是否正确。")
    elif not table_found or table_valid_count <= 0:
        warnings.append("未能在 PDF 中识别出规整的交易表格，已切换为按文本行兜底解析，建议仔细核对结果。")
        transactions = _parse_lines_fallback(full_text)

    if not transactions:
        warnings.append("未能自动提取到任何交易记录，请在网页上手动添加，或联系维护者补充该银行的解析规则。")

    no_date_count = sum(1 for t in transactions if not t.date_iso)
    if no_date_count:
        warnings.append(f"有 {no_date_count} 条记录的日期未能自动识别，请手动填写。")

    return ParseResult(
        account=account,
        transactions=transactions,
        warnings=warnings,
        raw_text_preview=full_text[:2000],
    )
