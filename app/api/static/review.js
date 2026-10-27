"use strict";
const statusNames = {
  PROCESSING: "待处理",
  APPROVED: "已通过",
  REJECTED: "已驳回",
  WAITING_INFO: "待补材料",
  CANCELLED: "已取消",
};
const actionNames = {
  approve: "批准",
  approve_with_note: "认可差异并通过",
  reject: "驳回",
  request_info: "要求补材料",
  confirm_fields: "确认字段并继续",
};
const taskNames = {
  ap_review: "会计复核",
  level1_approval: "一级审批",
  level2_approval: "二级审批",
  field_verify: "核对字段",
  wait_for_info: "等待补交发票",
};
const fieldNames = {
  invoice_number: "发票号码", invoice_date: "开票日期", seller_name: "销售方名称",
  seller_tax_id: "销售方税号", buyer_name: "购买方名称", buyer_tax_id: "购买方税号",
  po_number: "采购单号", gr_number: "收货单号", total_excl_tax: "不含税合计",
  total_tax: "税额合计", total_incl_tax: "价税合计", line_no: "序号", sku: "商品编码",
  description: "货物或服务名称", quantity: "数量", unit_price_excl_tax: "不含税单价",
  amount_excl_tax: "不含税金额", tax_rate: "税率", tax_amount: "税额", lines: "发票明细",
  po_line_no: "采购行号", quantity_received: "收货数量",
};
function fieldLabel(path) {
  const parts = path.split(".");
  return parts[0] === "lines" && parts.length === 3
    ? `第 ${Number(parts[1]) + 1} 行 · ${fieldNames[parts[2]] || "待确认字段"}`
    : fieldNames[path] || "待确认字段";
}
function findingValue(value) {
  if (value == null) return "—";
  if (typeof value === "object")
    return Object.entries(value)
      .map(([key, item]) => (fieldNames[key] || key) + " " + item)
      .join("，");
  return String(value);
}
let selected = null,
  runs = [],
  busy = false;
const $ = (id) => document.getElementById(id);
function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (cls) node.className = cls;
  return node;
}
function notice(message, error = false) {
  const node = $("notice");
  node.hidden = false;
  node.textContent = message;
  node.className = error ? "error" : "";
}
async function api(url, options = {}) {
  const res = await fetch(url, options);
  const data = await res.json();
  if (!res.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : "提交内容不符合要求，请检查字段。",
    );
  return data;
}
function badge(status) {
  return el("span", statusNames[status] || status, "badge " + status);
}
function money(value) {
  return value == null
    ? "—"
    : new Intl.NumberFormat("zh-CN", {
        style: "currency",
        currency: "CNY",
      }).format(value);
}
async function refresh() {
  runs = await api("/runs");
  $("processing-count").textContent = runs.filter(
    (r) => r.status === "PROCESSING",
  ).length;
  $("approved-count").textContent = runs.filter(
    (r) => r.status === "APPROVED",
  ).length;
  $("waiting-count").textContent = runs.filter(
    (r) => r.status === "WAITING_INFO",
  ).length;
  renderList();
  if (selected) {
    const run = await api("/runs/" + selected);
    renderDetail(run);
  }
}
function renderList() {
  const list = $("run-list");
  list.replaceChildren();
  const visible = runs.filter(
    (r) => !$("filter").value || r.status === $("filter").value,
  );
  if (!visible.length) {
    list.append(el("p", "暂无审核记录", "empty"));
    return;
  }
  for (const run of visible) {
    const inv = run.invoice || {};
    const button = el(
      "button",
      null,
      "run-card" + (selected === run.run_id ? " active" : ""),
    );
    button.type = "button";
    button.append(
      el("small", inv.invoice_number || "未识别票号"),
      el("b", inv.seller_name || "发票信息待确认"),
    );
    const meta = el("div", null, "run-meta");
    meta.append(badge(run.status), el("span", money(inv.total_incl_tax)));
    button.append(meta);
    button.onclick = async () => {
      try {
        selected = run.run_id;
        renderList();
        renderDetail(await api("/runs/" + selected));
      } catch (e) {
        notice(e.message, true);
      }
    };
    list.append(button);
  }
}
function valueAt(obj, path) {
  return path.split(".").reduce((value, key) => value?.[key], obj);
}
function labelValue(label, value, cls = "") {
  const box = el("div", null, "kv " + cls);
  box.append(el("span", label), el("b", value ?? "—"));
  return box;
}
function renderDetail(run) {
  const root = $("detail"),
    inv = run.invoice || {};
  root.replaceChildren();
  const head = el("div", null, "detail-head"),
    heading = el("div");
  heading.append(
    el("h3", inv.invoice_number || "发票审核"),
    el("small", inv.invoice_date || "日期待确认"),
  );
  head.append(heading, badge(run.status));
  root.append(head);
  const grid = el("div", null, "kv-grid");
  grid.append(
    labelValue("价税合计", money(inv.total_incl_tax), "amount"),
    labelValue("不含税金额", money(inv.total_excl_tax)),
    labelValue("税额", money(inv.total_tax)),
    labelValue("销售方", inv.seller_name),
    labelValue("销售方税号", inv.seller_tax_id),
    labelValue("购买方", inv.buyer_name),
    labelValue("购买方税号", inv.buyer_tax_id),
    labelValue("采购单", inv.po_number),
    labelValue("收货单", inv.gr_number),
  );
  root.append(grid);
  const wrap = el("div", null, "table-wrap"),
    table = el("table"),
    thead = el("thead"),
    tr = el("tr");
  for (const title of [
    "商品 / SKU",
    "数量",
    "不含税单价",
    "金额",
    "税率",
    "税额",
  ]) {
    tr.append(el("th", title));
  }
  thead.append(tr);
  table.append(thead);
  const tbody = el("tbody");
  for (const line of inv.lines || []) {
    const row = el("tr");
    for (const [index, value] of [
      (line.description || "未填写") + " / " + (line.sku || "未填写"),
      line.quantity,
      line.unit_price_excl_tax,
      line.amount_excl_tax,
      line.tax_rate == null ? "—" : Number(line.tax_rate) * 100 + "%",
      line.tax_amount,
    ].entries()) {
      row.append(el("td", value ?? "—", index ? "number" : ""));
    }
    tbody.append(row);
  }
  table.append(tbody);
  wrap.append(table);
  root.append(wrap);
  if (run.explanation) {
    root.append(
      el("h4", "差异说明", "section-label"),
      el("p", run.explanation, "explanation"),
    );
  }
  for (const finding of run.findings || []) {
    const box = el("div", null, "finding"),
      values = el("div", null, "finding-values");
    values.append(
      el("span", "期望 " + findingValue(finding.expected)),
      el("span", "实际 " + findingValue(finding.actual)),
    );
    box.append(
      el(
        "code",
        finding.code + (finding.field ? " · " + fieldLabel(finding.field) : ""),
      ),
      el("span", finding.message),
      values,
    );
    root.append(box);
  }
  if (run.variance) {
    root.append(
      el("p", "尾差记账：" + money(run.variance.amount), "explanation"),
    );
  }
  if (run.error) {
    root.append(
      el(
        "p",
        run.pending
          ? "上次提交未能完成，审核已回到当前待办，可重试或补交其他文件。"
          : "此发票未能完成识别，请检查票面信息后重新提交。",
        "explanation",
      ),
    );
  }
  if (run.pending) {
    renderTask(root, run);
  }
  if ((run.decisions || []).length) {
    root.append(el("h4", "处理记录", "section-label"));
    for (const decision of run.decisions) {
      const line = el("div", null, "history");
      line.append(
        el(
          "b",
          (decision.role === "finance_manager" ? "财务经理" : "会计") +
            " · " +
            (actionNames[decision.action] || "补交发票"),
        ),
        el("div", decision.note || "已确认"),
        el("div", new Date(decision.created_at).toLocaleString("zh-CN")),
      );
      root.append(line);
    }
  }
}
function renderTask(root, run) {
  const task = run.pending,
    box = el("section", null, "review-box");
  box.append(el("h3", taskNames[task.type] || "审核待办"));
  const required =
    task.required_role === "finance_manager" ? "财务经理" : "应付会计";
  box.append(el("p", "处理角色：" + required));
  if (task.info_rounds_left != null) {
    box.append(
      el(
        "p",
        task.info_rounds_left > 0
          ? "还可要求补材料 " + task.info_rounds_left + " 次。"
          : "补材料次数已用完；再次要求补材料将取消本次审核。",
      ),
    );
  }
  const permitted = $("role").value === task.required_role;
  const fields = {};
  for (const path of task.fields || []) {
    const label = el("label", fieldLabel(path), "field-input"),
      input = el("input");
    input.value = valueAt(run.invoice, path) ?? "";
    input.disabled = !permitted;
    label.append(input);
    fields[path] = input;
    box.append(label);
  }
  if (task.type === "wait_for_info") {
    const file = el("input");
    file.type = "file";
    file.accept = "application/pdf";
    file.disabled = !permitted;
    const button = el("button", "补交发票并重审", "primary");
    button.disabled = !permitted;
    button.onclick = () =>
      perform(async () => {
        if (!file.files.length) throw new Error("请选择需要补交的发票 PDF");
        const form = new FormData();
        form.append("file", file.files[0]);
        await api("/runs/" + run.run_id + "/documents", {
          method: "POST",
          headers: { "X-User-Role": $("role").value },
          body: form,
        });
      });
    box.append(file, button);
  } else {
    const note = el("textarea");
    note.placeholder = "处理理由或需要补充的材料";
    note.rows = 3;
    note.disabled = !permitted;
    box.append(note);
    const actions = el("div", null, "actions");
    for (const action of task.allowed_actions) {
      const button = el(
        "button",
        actionNames[action] || action,
        action === "reject"
          ? "danger"
          : action.startsWith("approve") || action === "confirm_fields"
            ? "primary"
            : "",
      );
      button.disabled = !permitted;
      button.onclick = () =>
        perform(async () => {
          const body = { action, note: note.value, version: run.version };
          if (action === "confirm_fields") {
            body.corrected_fields = Object.fromEntries(
              Object.entries(fields).map(([path, input]) => [
                path,
                input.value,
              ]),
            );
          }
          await api("/runs/" + run.run_id + "/resume", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-User-Role": $("role").value,
            },
            body: JSON.stringify(body),
          });
        });
      actions.append(button);
    }
    box.append(actions);
  }
  if (!permitted) box.append(el("p", "切换为" + required + "后可处理此待办。"));
  root.append(box);
}
async function perform(work) {
  if (busy) return;
  busy = true;
  document
    .querySelectorAll("button")
    .forEach((button) => (button.disabled = true));
  try {
    await work();
    notice("处理已完成。");
  } catch (e) {
    notice(e.message, true);
  } finally {
    busy = false;
    await refresh().catch((e) => notice(e.message, true));
    $("upload-form").querySelector("button").disabled = false;
    $("refresh").disabled = false;
  }
}
$("upload-form").onsubmit = (event) => {
  event.preventDefault();
  perform(async () => {
    const file = $("invoice-file").files[0];
    if (!file) throw new Error("请选择 PDF 发票");
    const form = new FormData();
    form.append("file", file);
    const result = await api("/runs", { method: "POST", body: form });
    selected = result.run_id;
    $("invoice-file").value = "";
  });
};
$("refresh").onclick = () => refresh().catch((e) => notice(e.message, true));
$("filter").onchange = renderList;
$("role").onchange = () => refresh().catch((e) => notice(e.message, true));
refresh().catch((e) => notice(e.message, true));
