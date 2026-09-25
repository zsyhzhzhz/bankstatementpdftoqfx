"""
生成 QFX（Quicken 专用的 OFX SGML 变体）文件。

QFX 本质上是 OFX 1.02 的 SGML 格式（非 XML，标签不闭合），
Quicken / 大多数记账软件都按这个格式导入。

支持两种账户：
- 储蓄卡/借记卡账户 -> <BANKACCTFROM> + <STMTTRNRS>（放在 BANKMSGSRSV1 下）
- 信用卡账户       -> <CCACCTFROM>   + <CCSTMTTRNRS>（放在 CREDITCARDMSGSRSV1 下）
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass
class QfxTransaction:
    date_iso: str  # YYYY-MM-DD
    description: str
    amount: float  # 收入为正，支出为负
    ref: str | None = None


@dataclass
class QfxAccount:
    account_type: Literal["BANK", "CC"]
    account_number: str
    bank_id: str = ""  # 仅 BANK 需要（联行号/开户行代码），可留空
    bank_acct_type: str = "CHECKING"  # CHECKING / SAVINGS，仅 BANK 用
    currency: str = "CNY"
    bank_name: str = ""
    statement_start: str | None = None  # YYYY-MM-DD
    statement_end: str | None = None
    opening_balance: float | None = None
    closing_balance: float | None = None


def _fmt_date(d: str | None, fallback: str) -> str:
    if not d:
        return fallback
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%Y%m%d")
    except ValueError:
        return fallback


def _fmt_amount(v: float) -> str:
    return f"{v:.2f}"


def _make_fitid(account_number: str, tx: QfxTransaction, index: int) -> str:
    if tx.ref:
        base = f"{account_number}-{tx.ref}"
    else:
        base = f"{account_number}-{tx.date_iso}-{tx.description}-{tx.amount:.2f}-{index}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:24].upper()


def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def build_qfx(account: QfxAccount, transactions: list[QfxTransaction]) -> str:
    now = datetime.now().strftime("%Y%m%d%H%M%S")

    dated_txs = [t for t in transactions if t.date_iso]
    if dated_txs:
        dt_start = _fmt_date(min(t.date_iso for t in dated_txs), now[:8])
        dt_end = _fmt_date(max(t.date_iso for t in dated_txs), now[:8])
    else:
        dt_start = dt_end = now[:8]

    if account.statement_start:
        dt_start = _fmt_date(account.statement_start, dt_start)
    if account.statement_end:
        dt_end = _fmt_date(account.statement_end, dt_end)

    lines: list[str] = []
    lines.append("OFXHEADER:100")
    lines.append("DATA:OFXSGML")
    lines.append("VERSION:102")
    lines.append("SECURITY:NONE")
    # 交易描述常含中文，用 UTF-8 声明以保证 Quicken/大多数记账软件能正确显示，
    # 若内容全是 ASCII 也兼容。
    lines.append("ENCODING:UTF-8")
    lines.append("CHARSET:NONE")
    lines.append("COMPRESSION:NONE")
    lines.append("OLDFILEUID:NONE")
    lines.append("NEWFILEUID:NONE")
    lines.append("")
    lines.append("<OFX>")
    lines.append("<SIGNONMSGSRSV1>")
    lines.append("<SONRS>")
    lines.append("<STATUS>")
    lines.append("<CODE>0")
    lines.append("<SEVERITY>INFO")
    lines.append("</STATUS>")
    lines.append(f"<DTSERVER>{now}")
    lines.append("<LANGUAGE>ENG")
    if account.bank_name:
        lines.append("<FI>")
        lines.append(f"<ORG>{_escape(account.bank_name)}")
        if account.bank_id:
            lines.append(f"<FID>{_escape(account.bank_id)}")
        lines.append("</FI>")
    lines.append("</SONRS>")
    lines.append("</SIGNONMSGSRSV1>")

    trnrs_uid = "1"

    if account.account_type == "CC":
        lines.append("<CREDITCARDMSGSRSV1>")
        lines.append("<CCSTMTTRNRS>")
        lines.append(f"<TRNUID>{trnrs_uid}")
        lines.append("<STATUS>")
        lines.append("<CODE>0")
        lines.append("<SEVERITY>INFO")
        lines.append("</STATUS>")
        lines.append("<CCSTMTRS>")
        lines.append(f"<CURDEF>{account.currency}")
        lines.append("<CCACCTFROM>")
        lines.append(f"<ACCTID>{_escape(account.account_number)}")
        lines.append("</CCACCTFROM>")
    else:
        lines.append("<BANKMSGSRSV1>")
        lines.append("<STMTTRNRS>")
        lines.append(f"<TRNUID>{trnrs_uid}")
        lines.append("<STATUS>")
        lines.append("<CODE>0")
        lines.append("<SEVERITY>INFO")
        lines.append("</STATUS>")
        lines.append("<STMTRS>")
        lines.append(f"<CURDEF>{account.currency}")
        lines.append("<BANKACCTFROM>")
        lines.append(f"<BANKID>{_escape(account.bank_id or '000000000')}")
        lines.append(f"<ACCTID>{_escape(account.account_number)}")
        lines.append(f"<ACCTTYPE>{account.bank_acct_type}")
        lines.append("</BANKACCTFROM>")

    lines.append("<BANKTRANLIST>")
    lines.append(f"<DTSTART>{dt_start}")
    lines.append(f"<DTEND>{dt_end}")

    for idx, tx in enumerate(transactions):
        trntype = "CREDIT" if tx.amount >= 0 else "DEBIT"
        dtposted = _fmt_date(tx.date_iso, dt_end)
        fitid = _make_fitid(account.account_number, tx, idx)
        name = _escape(tx.description.strip() or "TRANSACTION")[:120]
        lines.append("<STMTTRN>")
        lines.append(f"<TRNTYPE>{trntype}")
        lines.append(f"<DTPOSTED>{dtposted}")
        lines.append(f"<TRNAMT>{_fmt_amount(tx.amount)}")
        lines.append(f"<FITID>{fitid}")
        lines.append(f"<NAME>{name}")
        lines.append("</STMTTRN>")

    lines.append("</BANKTRANLIST>")

    if account.closing_balance is not None:
        lines.append("<LEDGERBAL>")
        lines.append(f"<BALAMT>{_fmt_amount(account.closing_balance)}")
        lines.append(f"<DTASOF>{dt_end}")
        lines.append("</LEDGERBAL>")

    if account.account_type == "CC":
        lines.append("</CCSTMTRS>")
        lines.append("</CCSTMTTRNRS>")
        lines.append("</CREDITCARDMSGSRSV1>")
    else:
        lines.append("</STMTRS>")
        lines.append("</STMTTRNRS>")
        lines.append("</BANKMSGSRSV1>")

    lines.append("</OFX>")

    return "\n".join(lines) + "\n"
