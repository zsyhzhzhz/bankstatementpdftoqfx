from __future__ import annotations

import io
import re
from dataclasses import asdict

from flask import Flask, jsonify, render_template, request, send_file

from .parser import parse_pdf
from .qfx import QfxAccount, QfxTransaction, build_qfx

app = Flask(
    __name__,
    template_folder="../templates",
    static_folder="../static",
)

MAX_CONTENT_LENGTH = 25 * 1024 * 1024  # 25MB
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/parse", methods=["POST"])
def api_parse():
    if "file" not in request.files:
        return jsonify({"error": "未收到文件，请选择一个 PDF 对账单。"}), 400
    f = request.files["file"]
    if not f.filename or not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "请上传 PDF 文件。"}), 400

    file_bytes = f.read()
    try:
        result = parse_pdf(file_bytes)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"解析 PDF 时出错：{e}"}), 500

    return jsonify(
        {
            "account": asdict(result.account),
            "transactions": [
                {
                    "date": t.date_iso or "",
                    "date_raw": t.date_raw,
                    "description": t.description,
                    "amount": t.amount,
                    "balance": t.balance,
                    "ref": t.ref,
                }
                for t in result.transactions
            ],
            "warnings": result.warnings,
        }
    )


def _validate_account(payload: dict) -> tuple[QfxAccount | None, str | None]:
    account_type = payload.get("account_type")
    if account_type not in ("BANK", "CC"):
        return None, "账户类型必须是 BANK（储蓄卡）或 CC（信用卡）。"

    account_number = (payload.get("account_number") or "").strip()
    if not account_number:
        return None, "请填写账号。"

    currency = (payload.get("currency") or "CNY").strip().upper()
    if not re.match(r"^[A-Z]{3}$", currency):
        return None, "币种需为 3 位字母代码，如 CNY、USD。"

    account = QfxAccount(
        account_type=account_type,
        account_number=account_number,
        bank_id=(payload.get("bank_id") or "").strip(),
        bank_acct_type=(payload.get("bank_acct_type") or "CHECKING").strip().upper(),
        currency=currency,
        bank_name=(payload.get("bank_name") or "").strip(),
        statement_start=(payload.get("statement_start") or None),
        statement_end=(payload.get("statement_end") or None),
        opening_balance=payload.get("opening_balance"),
        closing_balance=payload.get("closing_balance"),
    )
    return account, None


@app.route("/api/generate", methods=["POST"])
def api_generate():
    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"error": "请求体为空或不是合法 JSON。"}), 400

    account, err = _validate_account(payload)
    if err:
        return jsonify({"error": err}), 400

    raw_txs = payload.get("transactions") or []
    if not isinstance(raw_txs, list) or not raw_txs:
        return jsonify({"error": "没有交易记录，请至少保留一条。"}), 400

    txs: list[QfxTransaction] = []
    for i, t in enumerate(raw_txs):
        date_iso = (t.get("date") or "").strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_iso):
            return jsonify({"error": f"第 {i + 1} 条记录日期格式不对，应为 YYYY-MM-DD。"}), 400
        try:
            amount = float(t.get("amount"))
        except (TypeError, ValueError):
            return jsonify({"error": f"第 {i + 1} 条记录金额不是合法数字。"}), 400
        desc = (t.get("description") or "").strip() or "TRANSACTION"
        ref = (t.get("ref") or None)
        txs.append(QfxTransaction(date_iso=date_iso, description=desc, amount=amount, ref=ref))

    qfx_text = build_qfx(account, txs)
    # 用 utf-8-sig 写入 BOM，帮助部分 Windows 记账软件正确识别 UTF-8 编码。
    buf = io.BytesIO(qfx_text.encode("utf-8-sig"))
    filename = f"statement_{account.account_number or 'account'}.qfx"
    return send_file(
        buf,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.intu.qfx",
    )


if __name__ == "__main__":
    app.run(debug=True, port=8765)
