"""
扫描版 PDF（没有文字层，整页就是一张图片）的 OCR 兜底解析模块。

策略：
1. 用 PyMuPDF 把每一页渲染成高分辨率图片（不依赖 poppler/pdftoppm 等
   系统命令）。
2. 用 tesseract.js（通过 Node.js 子进程调用 `app/ocr_node/recognize.js`）
   对图片做文字识别，得到带像素坐标的单词列表——之所以选 tesseract.js
   而不是 pytesseract，是因为它是纯 JS + WASM 实现，不需要额外安装
   Tesseract 系统二进制/Homebrew，只需要项目里已经有的 Node.js 环境。
3. 把同一视觉行的单词按坐标聚类成"单元格"，参照表头单元格的位置把
   后续每一行的单元格归位到对应列（复用 `parser.py` 里表头关键字识别的
   逻辑），从而在没有文字层的情况下也能尽量还原出表格结构。
4. 因为是 OCR，识别错误在所难免（尤其是扫描件本身有噪点、倾斜、水印时），
   解析结果只作为"尽力而为"的猜测，务必提示用户仔细核对。

依赖：
- Python 包：pymupdf（渲染 PDF 页面为图片）
- Node.js + `app/ocr_node/` 下的 tesseract.js（首次使用某种语言时会自动从
  网络下载对应的 *.traineddata 语言包并缓存在 `app/ocr_node/` 目录下）
"""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from .parser import (
    AccountInfo,
    ParseResult,
    Transaction,
    _identify_header,
    _row_to_transaction,
    extract_account_info,
)

_OCR_NODE_DIR = Path(__file__).resolve().parent / "ocr_node"
_RECOGNIZE_SCRIPT = _OCR_NODE_DIR / "recognize.js"

# 渲染 PDF 页面为图片时用的分辨率：太低文字太糊识别不出来，太高又会明显
# 拖慢速度，200~300 DPI 是常见银行对账单扫描件比较合适的区间。
RENDER_DPI = 250

# 同一视觉行内，相邻两个单词之间的间隔（像素，按 RENDER_DPI≈250 估算）
# 超过这个值就认为是跨列了（另起一个单元格），而不是同一段文字里的空格。
_CELL_GAP_PX = 32


class OcrUnavailableError(RuntimeError):
    """Node.js / tesseract.js 环境不可用，或识别过程本身出错。"""


def is_ocr_available() -> bool:
    """粗略检查一下 OCR 所需的外部环境（Node.js + 依赖）是否齐备。"""
    if shutil.which("node") is None:
        return False
    if not _RECOGNIZE_SCRIPT.exists():
        return False
    if not (_OCR_NODE_DIR / "node_modules" / "tesseract.js").exists():
        return False
    return True


def render_pages_to_png(file_bytes: bytes, dpi: int = RENDER_DPI) -> list[bytes]:
    """用 PyMuPDF 把 PDF 每一页渲染成 PNG 图片字节。"""
    import fitz  # PyMuPDF；延迟导入，避免没装这个包时影响其它功能

    images: list[bytes] = []
    with fitz.open(stream=file_bytes, filetype="pdf") as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi)
            images.append(pix.tobytes("png"))
    return images


def ocr_image_words(image_bytes: bytes, lang: str = "eng", timeout: int = 120) -> list[dict]:
    """调用 Node 版 tesseract.js 识别一张图片，返回带坐标的单词列表。"""
    if shutil.which("node") is None:
        raise OcrUnavailableError(
            "未检测到 Node.js，OCR 功能需要先安装 Node.js，并在 app/ocr_node/ 目录下运行 `npm install`。"
        )
    if not _RECOGNIZE_SCRIPT.exists() or not (_OCR_NODE_DIR / "node_modules").exists():
        raise OcrUnavailableError(
            "OCR 依赖尚未安装，请在项目的 app/ocr_node/ 目录下运行 `npm install` 后重试。"
        )

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp.write(image_bytes)
        tmp_path = tmp.name
    try:
        proc = subprocess.run(
            ["node", str(_RECOGNIZE_SCRIPT), tmp_path, lang],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            raise OcrUnavailableError(f"OCR 识别失败：{proc.stderr.strip()[:500]}")
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise OcrUnavailableError(f"OCR 输出解析失败：{e}") from e
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 从 OCR 单词坐标重建表格（思路和 index.html 里针对 PDF.js 文本坐标做的
# reconstructTable 一致：先按行分组，再按 x 间隔聚类成单元格，再用表头
# 单元格的 x 位置作为"列锚点"给后续每一行的单元格归位）。
# ---------------------------------------------------------------------------

def _group_words_into_lines(words: list[dict]) -> list[list[dict]]:
    """按 tesseract 给出的 block/par/line 分组，得到按阅读顺序排列的"行"列表。"""
    groups: dict[tuple[int, int, int], list[dict]] = {}
    for w in words:
        key = (w.get("block", 0), w.get("par", 0), w.get("line", 0))
        groups.setdefault(key, []).append(w)
    lines = []
    for key in sorted(groups.keys()):
        line_words = sorted(groups[key], key=lambda w: w["x0"])
        lines.append(line_words)
    return lines


def _build_cells_with_pos(line_words: list[dict]) -> list[dict]:
    """把一行里的单词按 x 间隔聚类成"单元格"，记录每个单元格的起始 x 坐标。"""
    cells: list[dict] = []
    cur_parts: list[str] = []
    cur_x: Optional[float] = None
    last_end: Optional[float] = None
    for w in line_words:
        gap = 0 if last_end is None else w["x0"] - last_end
        if last_end is not None and gap > _CELL_GAP_PX:
            if cur_parts:
                cells.append({"text": " ".join(cur_parts).strip(), "x": cur_x})
            cur_parts = [w["text"]]
            cur_x = w["x0"]
        else:
            cur_parts.append(w["text"])
            if cur_x is None:
                cur_x = w["x0"]
        last_end = w["x1"]
    if cur_parts:
        cells.append({"text": " ".join(cur_parts).strip(), "x": cur_x})
    return cells


_SUMMARY_ROW_HINT_RE = re.compile(
    r"^(total|totals|fee period|cash deposited|transactions?$|minimum daily balance|"
    r"average (?:ledger|daily) balance|service charge|standard monthly service fee|"
    r"monthly service fee)\b",
    re.IGNORECASE,
)


def _looks_like_summary_row(row: list[str], mapping: dict) -> bool:
    """页面底部常见的"Totals / Fee period / Cash Deposited / Transactions /
    Minimum daily balance"这类统计行，不是单笔交易，但因为同一页里没有另
    起一个表头，会被表格重建逻辑当成普通数据行、把汇总金额误当成交易金额
    （这是导致总金额被严重放大的主要原因之一），需要单独识别并过滤掉。"""
    date_idx = mapping.get("date")
    desc_idx = mapping.get("desc")
    date_cell = row[date_idx] if date_idx is not None and date_idx < len(row) else ""
    desc_cell = row[desc_idx] if desc_idx is not None and desc_idx < len(row) else ""
    combined = f"{date_cell} {desc_cell}".strip()
    return bool(_SUMMARY_ROW_HINT_RE.match(combined))


def _assign_column(x: float, boundaries: list[float]) -> int:
    for i, b in enumerate(boundaries):
        if x < b:
            return i
    return len(boundaries)


def _reconstruct_table_from_ocr(lines: list[list[dict]]) -> Optional[dict]:
    for r in range(min(30, len(lines))):
        header_cells = _build_cells_with_pos(lines[r])
        header_texts = [c["text"] for c in header_cells]
        mapping = _identify_header(header_texts)
        if not mapping:
            continue

        xs = sorted(c["x"] for c in header_cells)
        boundaries = [(xs[i] + xs[i + 1]) / 2 for i in range(len(xs) - 1)]
        num_cols = len(header_cells)

        rows: list[list[str]] = []
        for i in range(r, len(lines)):
            cells = _build_cells_with_pos(lines[i])
            row_arr = [""] * num_cols
            for c in cells:
                idx = _assign_column(c["x"], boundaries)
                if idx >= num_cols:
                    idx = num_cols - 1
                row_arr[idx] = f"{row_arr[idx]} {c['text']}".strip() if row_arr[idx] else c["text"]
            rows.append(row_arr)
        return {"mapping": mapping, "rows": rows}
    return None


def _sanitize_ocr_transaction(t: Transaction) -> tuple[Transaction, bool, bool]:
    """OCR 把数字识别错（多认/漏认一位数字、逗号看成小数点等）时，经常会
    产出金额大到离谱（比如几千万/几十亿）或者日期离谱（年份变成 0113）的
    "交易"。这类明显不合理的值不如直接清空，让它们走"日期/金额未能识别"
    的人工核对提示，比直接把一个错得很离谱的数字混进汇总里更安全。"""
    amount_suspect = False
    date_suspect = False
    amount = t.amount
    if amount is not None and abs(amount) > 1_000_000:
        amount = None
        amount_suspect = True
    date_iso = t.date_iso
    if date_iso:
        year = int(date_iso[:4])
        if year < 2015 or year > datetime.now().year + 2:
            date_iso = None
            date_suspect = True
    t.amount = amount
    t.date_iso = date_iso
    return t, amount_suspect, date_suspect


def _merge_continuation_rows(transactions: list[Transaction]) -> list[Transaction]:
    """OCR 按视觉行还原表格时，一笔交易如果描述换行成多行，每一行都会被
    当成单独一行（后续行没有日期也没有金额），这里把这些"纯描述续行"
    合并回上一笔真正的交易里，避免产出一堆有描述没金额的空交易。"""
    merged: list[Transaction] = []
    for t in transactions:
        is_continuation = not t.date_iso and t.amount is None and t.description and t.description != "(无描述)"
        if is_continuation and merged:
            prev = merged[-1]
            prev.description = f"{prev.description} {t.description}".strip()[:200]
            continue
        merged.append(t)
    return merged


def parse_scanned_pdf(file_bytes: bytes) -> ParseResult:
    """整份 PDF 都没有文字层（典型的扫描件）时的 OCR 兜底解析入口。"""
    warnings: list[str] = [
        "该 PDF 没有可提取的文字层（很可能是扫描件/图片），已启用 OCR 文字识别，"
        "识别结果可能存在错行、错字、金额识别错误等问题，务必仔细核对每一条记录。"
    ]

    if not is_ocr_available():
        warnings.append(
            "OCR 所需环境不完整（需要本机已安装 Node.js，并在 app/ocr_node/ 目录下运行过 `npm install`），"
            "本次未能自动识别任何内容，请手动在网页上添加交易记录，或先完成 OCR 环境安装后重新上传。"
        )
        return ParseResult(account=AccountInfo(), transactions=[], warnings=warnings, raw_text_preview="")

    try:
        images = render_pages_to_png(file_bytes)
    except Exception as e:  # noqa: BLE001
        warnings.append(f"渲染 PDF 页面为图片失败：{e}")
        return ParseResult(account=AccountInfo(), transactions=[], warnings=warnings, raw_text_preview="")

    transactions: list[Transaction] = []
    all_text_parts: list[str] = []
    table_found = False
    suspect_amount_count = 0
    suspect_date_count = 0

    for page_idx, img_bytes in enumerate(images):
        try:
            words = ocr_image_words(img_bytes)
        except OcrUnavailableError as e:
            warnings.append(f"第 {page_idx + 1} 页 OCR 识别失败：{e}")
            continue

        lines = _group_words_into_lines(words)
        page_text = "\n".join(" ".join(w["text"] for w in line) for line in lines)
        all_text_parts.append(page_text)

        table = _reconstruct_table_from_ocr(lines)
        if table:
            table_found = True
            page_txs: list[Transaction] = []
            for row in table["rows"][1:]:
                if _looks_like_summary_row(row, table["mapping"]):
                    continue
                tx = _row_to_transaction(row, table["mapping"], f"ocr-p{page_idx + 1}")
                if tx:
                    tx, amount_suspect, date_suspect = _sanitize_ocr_transaction(tx)
                    suspect_amount_count += int(amount_suspect)
                    suspect_date_count += int(date_suspect)
                    page_txs.append(tx)
            transactions.extend(_merge_continuation_rows(page_txs))

    full_text = "\n".join(all_text_parts)
    account = extract_account_info(full_text)

    if not table_found:
        warnings.append("OCR 识别出了文字，但未能在其中定位到规整的交易表格，暂不支持这种版式的扫描件，请手动添加交易记录。")

    if not transactions:
        warnings.append("未能从 OCR 结果里提取到任何交易记录，请在网页上手动添加。")

    no_date_count = sum(1 for t in transactions if not t.date_iso)
    if no_date_count:
        warnings.append(f"有 {no_date_count} 条记录的日期未能自动识别，请手动填写（OCR 对扫描件的日期数字容易识别出错，务必核对）。")
    if suspect_date_count:
        warnings.append(f"有 {suspect_date_count} 条记录识别出的日期明显不合理（如年份错乱），已清空为待填写，请手动核对。")
    if suspect_amount_count:
        warnings.append(f"有 {suspect_amount_count} 条记录识别出的金额明显不合理（数值离谱地大，很可能是数字识别错误），已清空为待填写，请对照原图手动核对。")

    # OCR 对单个数字的识别错误（比如漏看/多看一位数字、小数点位置错了）
    # 往往不会大到被前面的"离谱金额"过滤器拦下来，但累积起来还是会让所有
    # 交易金额的加总明显偏离对账单本身打印的期初/期末余额差——这是一个比
    # "肉眼抽查几条"更可靠的整体质量信号，加总差得离谱就要明确提醒用户
    # 不要直接相信这份 OCR 结果的金额，务必对照原图逐条核对。
    if account.opening_balance is not None and account.closing_balance is not None:
        expected_net = account.closing_balance - account.opening_balance
        actual_net = sum(t.amount for t in transactions if t.amount is not None)
        diff = abs(actual_net - expected_net)
        if diff > max(500.0, abs(expected_net) * 0.05):
            warnings.append(
                "警告：逐条交易金额加总（约 {:.2f}）与对账单本身期初/期末余额算出的应有净变动（{:.2f}）相差较大，"
                "说明 OCR 识别出的金额很可能有明显错误，请不要直接信任逐条金额，务必对照原始 PDF 图片仔细核对每一笔。".format(
                    actual_net, expected_net
                )
            )

    return ParseResult(
        account=account,
        transactions=transactions,
        warnings=warnings,
        raw_text_preview=full_text[:2000],
    )
