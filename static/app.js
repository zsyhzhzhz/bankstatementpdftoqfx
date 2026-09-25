(function () {
  "use strict";

  const el = (id) => document.getElementById(id);

  const parseBtn = el("parseBtn");
  const parseStatus = el("parseStatus");
  const warningsBox = el("warnings");

  const accountSection = el("accountSection");
  const txSection = el("txSection");
  const generateSection = el("generateSection");

  const accountType = el("accountType");
  const bankAcctTypeWrap = el("bankAcctTypeWrap");
  const bankIdWrap = el("bankIdWrap");

  const txBody = el("txBody");
  const txSummary = el("txSummary");
  const addRowBtn = el("addRowBtn");

  const generateBtn = el("generateBtn");
  const generateStatus = el("generateStatus");

  function setStatus(node, text, cls) {
    node.textContent = text || "";
    node.className = "status" + (cls ? " " + cls : "");
  }

  function toggleAccountFields() {
    const isBank = accountType.value === "BANK";
    bankAcctTypeWrap.classList.toggle("hidden", !isBank);
    bankIdWrap.classList.toggle("hidden", !isBank);
  }
  accountType.addEventListener("change", toggleAccountFields);
  toggleAccountFields();

  function fillAccountForm(account) {
    el("bankName").value = account.bank_name || "";
    accountType.value = account.account_type_guess === "CC" ? "CC" : "BANK";
    el("accountNumber").value = account.account_number || "";
    el("bankId").value = account.bank_id || "";
    el("currency").value = account.currency || "CNY";
    el("statementStart").value = account.statement_start || "";
    el("statementEnd").value = account.statement_end || "";
    el("openingBalance").value = account.opening_balance ?? "";
    el("closingBalance").value = account.closing_balance ?? "";
    toggleAccountFields();
  }

  function makeRow(tx) {
    tx = tx || { date: "", description: "", amount: "", balance: "", ref: "" };
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><input type="date" class="f-date" value="${tx.date || ""}"></td>
      <td class="row-desc"><input type="text" class="f-desc" value="${escapeAttr(tx.description || "")}"></td>
      <td><input type="number" step="0.01" class="f-amount" value="${tx.amount ?? ""}"></td>
      <td><input type="number" step="0.01" class="f-balance" value="${tx.balance ?? ""}" tabindex="-1"></td>
      <td><input type="text" class="f-ref" value="${escapeAttr(tx.ref || "")}"></td>
      <td><button type="button" class="remove-btn">删除</button></td>
    `;
    tr.querySelector(".remove-btn").addEventListener("click", () => {
      tr.remove();
      updateSummary();
    });
    tr.querySelectorAll("input").forEach((inp) =>
      inp.addEventListener("input", updateSummary)
    );
    return tr;
  }

  function escapeAttr(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/"/g, "&quot;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function renderTransactions(transactions) {
    txBody.innerHTML = "";
    transactions.forEach((tx) => txBody.appendChild(makeRow(tx)));
    updateSummary();
  }

  function updateSummary() {
    const rows = Array.from(txBody.querySelectorAll("tr"));
    let sum = 0;
    let invalid = 0;
    rows.forEach((tr) => {
      const date = tr.querySelector(".f-date").value;
      const amountStr = tr.querySelector(".f-amount").value;
      const amount = parseFloat(amountStr);
      const ok = !!date && !Number.isNaN(amount);
      tr.classList.toggle("invalid", !ok);
      if (!ok) invalid += 1;
      if (!Number.isNaN(amount)) sum += amount;
    });
    txSummary.textContent = `共 ${rows.length} 条记录，合计金额 ${sum.toFixed(2)}` +
      (invalid ? `，其中 ${invalid} 条日期/金额不完整（已高亮标红）` : "");
    txSummary.className = "status" + (invalid ? " error" : " ok");
  }

  addRowBtn.addEventListener("click", () => {
    txBody.appendChild(makeRow());
    updateSummary();
  });

  parseBtn.addEventListener("click", async () => {
    const fileInput = el("pdfFile");
    const file = fileInput.files[0];
    if (!file) {
      setStatus(parseStatus, "请先选择一个 PDF 文件。", "error");
      return;
    }
    setStatus(parseStatus, "解析中…（表格较多的对账单可能需要几秒）");
    parseBtn.disabled = true;
    warningsBox.classList.add("hidden");
    warningsBox.innerHTML = "";

    try {
      const fd = new FormData();
      fd.append("file", file);
      const resp = await fetch("/api/parse", { method: "POST", body: fd });
      const data = await resp.json();
      if (!resp.ok) {
        setStatus(parseStatus, data.error || "解析失败。", "error");
        return;
      }

      fillAccountForm(data.account);
      renderTransactions(data.transactions);

      if (data.warnings && data.warnings.length) {
        warningsBox.innerHTML =
          "<strong>提示：</strong><ul>" +
          data.warnings.map((w) => `<li>${escapeAttr(w)}</li>`).join("") +
          "</ul>";
        warningsBox.classList.remove("hidden");
      }

      accountSection.classList.remove("hidden");
      txSection.classList.remove("hidden");
      generateSection.classList.remove("hidden");
      setStatus(parseStatus, `解析完成，共提取 ${data.transactions.length} 条交易记录，请核对下方信息。`, "ok");
    } catch (e) {
      setStatus(parseStatus, "请求失败：" + e.message, "error");
    } finally {
      parseBtn.disabled = false;
    }
  });

  generateBtn.addEventListener("click", async () => {
    const rows = Array.from(txBody.querySelectorAll("tr"));
    const transactions = rows.map((tr) => ({
      date: tr.querySelector(".f-date").value,
      description: tr.querySelector(".f-desc").value,
      amount: tr.querySelector(".f-amount").value,
      ref: tr.querySelector(".f-ref").value,
    }));

    const payload = {
      account_type: accountType.value,
      account_number: el("accountNumber").value,
      bank_id: el("bankId").value,
      bank_acct_type: el("bankAcctType").value,
      currency: el("currency").value,
      bank_name: el("bankName").value,
      statement_start: el("statementStart").value,
      statement_end: el("statementEnd").value,
      opening_balance: el("openingBalance").value ? parseFloat(el("openingBalance").value) : null,
      closing_balance: el("closingBalance").value ? parseFloat(el("closingBalance").value) : null,
      transactions,
    };

    setStatus(generateStatus, "生成中…");
    generateBtn.disabled = true;
    try {
      const resp = await fetch("/api/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        setStatus(generateStatus, data.error || "生成失败。", "error");
        return;
      }
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = (payload.account_number || "statement") + ".qfx";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      setStatus(generateStatus, "已生成并下载 QFX 文件。", "ok");
    } catch (e) {
      setStatus(generateStatus, "请求失败：" + e.message, "error");
    } finally {
      generateBtn.disabled = false;
    }
  });
})();
