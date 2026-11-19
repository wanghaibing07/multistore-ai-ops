"use strict";
const $ = (s) => document.querySelector(s),
  $$ = (s) => Array.from(document.querySelectorAll(s));
const state = {
  store: null,
  csrf: sessionStorage.getItem("ms_csrf") || "",
  report: null,
  columns: null,
  batch: null,
  kind: null,
  conflicts: false,
  changesToken: null,
  multiConfirmed: false,
  adjustmentKey: null,
  epoch: 0,
  reportSeq: 0,
  matchSeq: 0,
  importRevision: 0,
  orderLimit: 50,
  selectedOrderIds: new Set(),
  visibleOrderIds: [],
  quickLoginEnabled: true,
  config: null,
  cardMatches: null,
  cardMatchLimit: 12,
  loadedSettingsStore: null,
  captchaSession: null,
  ledgerTab: "purchases",
  ledgerCategory: "all",
  ledgerData: null,
  matchTierFilter: "all",
  confirmedItems: new Set(),
  selectedCandByItem: {},
  kbMatchIndex: 0,
  chatCategory: "all",
  chatTopic: "",
  chatOverview: null,
  cmdIndex: 0,
  theme: localStorage.getItem("ms_theme") || (window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light"),
};

function applyTheme(nextTheme) {
  const clean = nextTheme === "dark" ? "dark" : "light";
  state.theme = clean;
  document.documentElement.dataset.theme = clean;
  const meta = $("#color-scheme-meta");
  if (meta) meta.setAttribute("content", clean === "dark" ? "dark" : "light");
  try { localStorage.setItem("ms_theme", clean); } catch {}
}
applyTheme(state.theme);

const scope = () => ({ store: state.store, epoch: state.epoch });
const current = (x) => x.store === state.store && x.epoch === state.epoch;
let busyCount = 0;
const busyButtons = new WeakSet();
const labels = {
  item_id: "子订单编号",
  order_id: "主订单编号",
  sku: "规格编码",
  product_id: "商品 ID",
  property: "商品属性",
  title: "商品标题",
  quantity: "购买数量",
  paid_at: "付款时间",
  status: "订单状态",
  shipped_at: "发货时间",
  payment: "实付金额",
  refund_status: "退款状态",
  refund_amount: "退款金额",
  refund_id: "退款编号",
  completed_at: "退款完成时间",
  valid_from: "成本生效时间",
  unit_cost: "单位成本",
  supplier: "实际供货渠道",
  bill_type: "账单大类",
  business_flow: "业务流水号",
  settled_at: "打款时间",
  bill_income: "货款收入金额",
  bill_refund: "售后退款金额",
};
const ORDER_CATEGORY_LABELS = {
  all: "全部类目",
  finished: "成品号",
  activation: "激活",
  family: "家庭组",
  other: "其他",
};
function orderCategory(item) {
  const text = [item?.title, item?.sku, item?.property]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  if (/(代充|激活|开通|充值|邀请码|帮填|订阅)/i.test(text)) return "activation";
  if (/(家庭组|拼车|邀请资格|family)/i.test(text)) return "family";
  if (/(成品号|成品|现号|独享)/i.test(text)) return "finished";
  return "other";
}
function fulfillmentLabel(status) {
  return { shipped: "已发货", unshipped: "未发货", unknown: "发货状态待确认" }[status] || "发货状态待确认";
}
function refundLabel(status) {
  return { none: "无退款", success: "已退款", pending: "退款处理中", closed: "退款申请已关闭", unknown: "退款状态待确认" }[status] || "退款状态待确认";
}
function orderIssueLabel(reasons, estimatedProfit) {
  const labels = (reasons || []).map((reason) => ({
    "退款历史是否完整尚未核对": "待核对退款",
    "退款申请已关闭，需核对历史退款": "待核对退款",
    "缺少商品成本": "待补成本",
    "订单信息已变化，请核对原成本和调整后重新设置逐单成本": "成本已变化",
  }[reason] || reason));
  if (!labels.length && estimatedProfit !== null && estimatedProfit !== undefined) {
    return `待处理，暂估 ${money(estimatedProfit)}`;
  }
  return labels.join("、") || "已核算";
}
function orderActionMarkup(item) {
  const reasons = item.reasons || [];
  const actions = [];
  const needsCost = item.cost_needs_review || reasons.some((reason) => reason.includes("商品成本") || reason.includes("成本"));
  const needsBasis = item.basis === "unknown" || item.basis_review_stale || reasons.some((reason) => reason.includes("金额口径"));
  const needsRefund = Boolean(item.refund_review_fingerprint) && reasons.some((reason) => /退款|账单/.test(reason));
  if (needsCost) {
    actions.push(`<button type="button" class="link-button action-primary" data-fix="${esc(item.item_id)}">${item.cost_needs_review ? "重新设置成本" : "补成本"}</button>`);
  }
  if (needsBasis) {
    actions.push(`<button type="button" class="link-button" data-basis-review="${esc(item.item_id)}">${item.basis_review_stale ? "更新金额口径" : "确认金额口径"}</button>`);
  }
  if (needsRefund) {
    actions.push(`<button type="button" class="link-button" data-refund-review="${esc(item.item_id)}">核对退款</button>`);
  }
  if (!actions.length) return "";
  if (actions.length === 1) return actions[0];
  return `<details class="order-more"><summary aria-label="更多处理动作">更多</summary><div class="order-more-menu">${actions.join("")}</div></details>`;
}
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const money = (n) =>
  n === null || n === undefined
    ? "—"
    : `${n < 0 ? "-" : ""}¥${(Math.abs(n) / 100).toFixed(2)}`;
const day = (d) => {
  let y = d.getFullYear(),
    m = String(d.getMonth() + 1).padStart(2, "0"),
    x = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${x}`;
};
const setText = (id, value) => {
  const node = $("#" + id);
  if (node) node.textContent = value;
};
const toast = (msg, type = "success") => {
  let x = $("#toast");
  let message = $("#toast-message");
  if (!x) return;
  const text = String(msg ?? "");
  if (message) message.textContent = text.length > 180 ? `${text.slice(0, 180)}…` : text;
  else x.textContent = text.length > 180 ? `${text.slice(0, 180)}…` : text;
  x.className = `show ${type}`;
  x.classList.add("show");
};
$("#toast-dismiss")?.addEventListener("click", () => $("#toast")?.classList.remove("show"));
async function api(path, options = {}, retried = false) {
  let headers = new Headers(options.headers || {});
  if (options.body && !(options.body instanceof FormData))
    headers.set("Content-Type", "application/json");
  if (options.method && options.method !== "GET" && state.csrf)
    headers.set("X-CSRF-Token", state.csrf);
  let response;
  try {
    response = await fetch(path, {
      ...options,
      headers,
      credentials: "same-origin",
    });
  } catch {
    throw new Error("连接不到本地服务，请确认工作台仍在运行");
  }
  let data;
  try {
    data = await response.json();
  } catch {
    data = { detail: "服务返回无法识别的内容" };
  }
  if (!response.ok) {
    const expired = response.status === 401 || (response.status === 403 && data.detail === "操作已过期，请重新登录");
    if (expired && !retried && path !== "/api/login" && path !== "/api/quick-login" && path !== "/api/auth/quick-login" && !(options.body instanceof FormData)) {
      try {
        await doQuickLogin();
        return api(path, options, true);
      } catch {}
    }
    if (response.status === 401 && path !== "/api/login") showLogin();
    const error = new Error(
      typeof data.detail === "string"
        ? data.detail
        : data.detail?.message || "操作未完成，请检查输入",
    );
    if (Array.isArray(data.detail?.issues)) error.issues = data.detail.issues;
    throw error;
  }
  return data;
}
function applyAuthConfig(config) {
  state.config = config;
  state.quickLoginEnabled = config.quick_login_enabled !== false;
  const quickSection = $(".quick-login-section");
  if (quickSection) quickSection.hidden = !state.quickLoginEnabled;
  const quickButton = $("#quick-login-btn");
  if (quickButton) quickButton.hidden = !state.quickLoginEnabled;
  const loginForm = $("#login-form");
  if (loginForm) loginForm.hidden = state.quickLoginEnabled;
  for (const id of ["open-qianniu", "open-qianniu-top"]) {
    const link = $("#" + id);
    if (link && config.qianniu_url) link.href = config.qianniu_url;
  }
  const cardLink = $("#open-card-site");
  if (cardLink && config.card_site_url) cardLink.href = config.card_site_url;
  const card16688 = $("#open-card-16688");
  if (card16688 && config.card_sites?.["16688"]?.order_url) {
    card16688.href = config.card_sites["16688"].order_url;
  }
  const cardWzyp = $("#open-card-wzyp");
  if (cardWzyp && config.card_sites?.wzyp?.order_url) {
    cardWzyp.href = config.card_sites.wzyp.order_url;
  }
}
async function loadAuthConfig() {
  const config = await api("/api/config");
  applyAuthConfig(config);
  return config;
}
async function doQuickLogin(username) {
  if (!state.quickLoginEnabled) throw Error("远程模式请使用账号密码登录");
  let payload = username ? { username } : {};
  let r = await api("/api/quick-login", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  state.csrf = r.csrf;
  sessionStorage.setItem("ms_csrf", r.csrf);
  showApp(r);
}
async function refreshQuickUsers() {
  try {
    let users = await api("/api/auth/users");
    let grid = $("#quick-users-grid");
    if (grid && Array.isArray(users) && users.length) {
      grid.innerHTML = users
        .map(
          (u) => `
        <button type="button" class="quick-role-card ${u.username === "store_owner" ? "active" : ""}" data-quick-user="${esc(u.username)}">
          <span class="role-icon">${u.username === "store_owner" ? "👑" : "🤝"}</span>
          <span class="role-meta"><strong>${esc(u.username === "store_owner" ? "主店店主" : "合作分店")} (${esc(u.username)})</strong><small>${esc((u.stores || []).map((s) => s.name).join("、") || "店铺")}</small></span>
        </button>
      `,
        )
        .join("");
    }
  } catch {}
}
function showLogin() {
  state.epoch++;
  state.reportSeq++;
  state.matchSeq++;
  state.store = null;
  state.csrf = "";
  state.report = null;
  state.cardMatches = null;
  state.ledgerData = null;
  state.chatOverview = null;
  state.confirmedItems = new Set();
  state.selectedOrderIds.clear();
  state.visibleOrderIds = [];
  state.selectedCandByItem = {};
  state.kbMatchIndex = 0;
  state.refundReview = null;
  state.basisReview = null;
  state.loadedSettingsStore = null;
  state.captchaSession = null;
  state.ledgerData = null;
  resetImport();
  $("#basis-review-panel").hidden = true;
  $("#refund-review-panel").hidden = true;
  if ($("#match-summary-dashboard")) {
    $("#match-summary-dashboard").hidden = true;
    $("#match-summary-dashboard").replaceChildren();
  }
  setText("match-results", "导入卡网采购单后开始核对。");
  setText("orders-list", "请登录后查看订单。");
  setText("batch-list", "请登录后查看导入批次。");
  setText("cost-list", "请登录后查看成本记录。");
  setText("order-total", "请登录后查看订单");
  for (const id of ["profit-value", "sales-value", "cost-value"]) setText(id, "—");
  setText("settled-count", "0");
  setText("as-of", "尚未登录");
  setText("scope-note", "");
  if ($("#card-captcha-panel")) $("#card-captcha-panel").hidden = true;
  sessionStorage.removeItem("ms_csrf");
  $("#app-shell").hidden = true;
  $("#login-screen").hidden = false;
  if ($("#password")) $("#password").value = "";
  applyAuthConfig({ quick_login_enabled: state.quickLoginEnabled });
  if (state.quickLoginEnabled) refreshQuickUsers();
}
function setBusy(button, on, label) {
  if (!button) return;
  if (on) {
    if (!busyButtons.has(button)) {
      busyButtons.add(button);
      busyCount++;
      button.dataset.originalHtml = button.innerHTML;
    }
    button.innerHTML = `<span>${esc(label || "处理中…")}</span>`;
    button.classList.add("is-busy");
    button.setAttribute("aria-busy", "true");
    button.setAttribute("aria-disabled", "true");
    button.disabled = true;
  } else {
    if (busyButtons.has(button)) {
      busyButtons.delete(button);
      busyCount--;
    }
    button.innerHTML = button.dataset.originalHtml || button.textContent;
    button.classList.remove("is-busy");
    button.removeAttribute("aria-busy");
    button.removeAttribute("aria-disabled");
    button.disabled = false;
  }
  const storeSelect = $("#store-select");
  if (storeSelect) storeSelect.disabled = busyCount > 0;
}
function goto(name) {
  location.hash = name;
}
function route() {
  let name = location.hash.slice(1) || "overview";
  if (!$(`[data-panel="${name}"]`)) name = "overview";
  $$("[data-panel]").forEach((x) => (x.hidden = x.dataset.panel !== name));
  $$("[data-view]").forEach((x) =>
    x.classList.toggle("active", x.dataset.view === name),
  );
  let title = {
    overview: "工作台",
    import: "导入数据",
    costs: "AI 补齐采购成本",
    orders: "已入库订单",
    chats: "千牛聊天分析",
    assistant: "经营问答",
  };
  $("#page-title").textContent = title[name] || "工作台";
  if (state.store) {
    if (name === "overview" || name === "orders") loadReport();
    if (name === "import") loadBatches();
    if (name === "costs") loadCosts();
    if (name === "chats") loadChatsOverview();
    if (name === "assistant") loadAskSkus();
  }
}
function showApp(data) {
  state.epoch++;
  state.user = data.username || "免密店主";
  $("#login-screen").hidden = true;
  $("#app-shell").hidden = false;
  let nameEl = $("#user-display-name");
  if (nameEl) nameEl.textContent = "本机模式";
  $("#store-select").innerHTML = data.stores
    .map((s) => `<option value="${s.id}">${esc(s.name)}</option>`)
    .join("");
  state.store = Number($("#store-select").value);
  $("#store-name").textContent =
    $("#store-select").selectedOptions[0]?.textContent || "我的店铺";
  route();
  loadCardLedgerBadgeQuietly();
  loadDateRange();
  loadChatsBadgeQuietly();
}
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  let btn = e.submitter;
  setBusy(btn, true, "登录中…");
  $("#login-error").textContent = "";
  try {
    let r = await api("/api/login", {
      method: "POST",
      body: JSON.stringify({
        username: $("#username").value.trim(),
        password: $("#password").value,
      }),
    });
    state.csrf = r.csrf;
    sessionStorage.setItem("ms_csrf", r.csrf);
    $("#password").value = "";
    showApp(r);
  } catch (err) {
    $("#login-error").textContent = err.message;
  } finally {
    setBusy(btn, false);
  }
});
$("#logout").addEventListener("click", async () => {
  try {
    await api("/api/logout", { method: "POST" });
  } catch {}
  showLogin();
});
document.addEventListener("click", async (e) => {
  let qBtn = e.target.closest("#quick-login-btn");
  if (qBtn) {
    setBusy(qBtn, true, "正在进入…");
    try {
      await doQuickLogin();
    } catch (err) {
      toast(err.message);
    } finally {
      setBusy(qBtn, false);
    }
    return;
  }
  let roleCard = e.target.closest("[data-quick-user]");
  if (roleCard) {
    let username = roleCard.dataset.quickUser;
    setBusy(roleCard, true, "切换中…");
    try {
      await doQuickLogin(username);
    } catch (err) {
      toast(err.message);
    } finally {
      setBusy(roleCard, false);
    }
    return;
  }
  if (e.target.closest("#switch-user-btn")) {
    showLogin();
    return;
  }
  if (e.target.closest("#toggle-legacy-login")) {
    let box = $("#legacy-login-box");
    if (box) box.hidden = !box.hidden;
    return;
  }
});
$("#store-select").addEventListener("change", () => {
  state.epoch++;
  state.store = Number($("#store-select").value);
  state.report = null;
  state.refundReview = null;
  state.basisReview = null;
  state.adjustmentKey = null;
  $("#basis-review-panel").hidden = true;
  $("#store-name").textContent =
    $("#store-select").selectedOptions[0]?.textContent || "我的店铺";
  $("#refund-review-panel").hidden = true;
  setText("refund-review-source", "");
  setText("refund-review-history", "");
  $("#import-form").reset();
  resetImport();
  $("#cost-form").reset();
  $("#cost-from").value = day(new Date());
  $("#sku-options").innerHTML = "";
  $("#adjustment-form").reset();
  $("#adjust-note").required = true;
  $("#adjustment-history").textContent = "";
  $("#ask-form").reset();
  $("#ask-sku").innerHTML = '<option value="">全部商品</option>';
  $("#answer").textContent = "";
  for (const id of ["profit-value", "sales-value", "cost-value"])
    setText(id, "—");
  setText("settled-count", "0");
  for (const id of ["orders-list", "batch-list", "sku-list", "cost-list"])
    setText(id, "正在读取当前店铺…");
  setText("profit-sub", "正在读取当前店铺…");
  setText("scope-note", "");
  setText("gross-basis-result", "");
  setText("as-of", "正在读取当前店铺…");
  setText("sku-count", "—");
  setText("order-total", "正在读取当前店铺…");
  const search = $("#order-search");
  if (search) search.value = "";
  const reviewOnly = $("#review-only");
  if (reviewOnly) reviewOnly.checked = false;
  const orderCategorySelect = $("#order-category-filter");
  if (orderCategorySelect) orderCategorySelect.value = "all";
  state.cardMatches = null;
  state.matchTierFilter = "all";
  state.confirmedItems = new Set();
  state.selectedCandByItem = {};
  state.kbMatchIndex = 0;
  if ($("#match-summary-dashboard")) {
    $("#match-summary-dashboard").hidden = true;
    $("#match-summary-dashboard").innerHTML = "";
  }
  setText("match-results", "导入卡网采购单后开始核对。");
  state.loadedSettingsStore = null;
  state.captchaSession = null;
  state.ledgerTab = "purchases";
  state.ledgerCategory = "all";
  state.ledgerData = null;
  state.chatCategory = "all";
  state.chatTopic = "";
  state.chatOverview = null;
  if ($("#card-contact-email")) $("#card-contact-email").value = "";
  setText("card-settings-status", "正在读取已保存的联系方式…");
  if ($("#card-paste-text")) $("#card-paste-text").value = "";
  if ($("#card-captcha-panel")) $("#card-captcha-panel").hidden = true;
  if ($("#ledger-filter-form")) $("#ledger-filter-form").reset();
  if ($("#chat-filter-form")) $("#chat-filter-form").reset();
  if ($("#chat-answer")) $("#chat-answer").innerHTML = "";
  if ($("#chat-import-result")) $("#chat-import-result").innerHTML = "";
  $$(".cat-pill").forEach((pill) => {
    const active = pill.dataset.ledgerCat === "all";
    pill.classList.toggle("active", active);
    pill.setAttribute("aria-pressed", String(active));
  });
  $$(".chat-cat-pill").forEach((pill) => {
    const active = pill.dataset.chatCat === "all";
    pill.classList.toggle("active", active);
    pill.setAttribute("aria-pressed", String(active));
  });
  activateLedgerTab($("#ledger-tab-purchases"), { focus: false, reload: false });
  state.orderLimit = 50;
  route();
  loadCardLedgerBadgeQuietly();
  loadDateRange();
  loadChatsBadgeQuietly();
});
window.addEventListener("hashchange", route);
document.addEventListener("click", (e) => {
  let target = e.target.closest("[data-goto]");
  if (target) goto(target.dataset.goto);
});
let now = new Date(),
  start = new Date(now);
start.setDate(start.getDate() - 29);
$("#start-date").value = day(start);
$("#end-date").value = day(now);
$("#cost-from").value = day(now);
$("#period-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const button = e.submitter || e.currentTarget.querySelector("button");
  state.orderLimit = 50;
  setBusy(button, true, "正在刷新…");
  try {
    const ok = await loadReport();
    if (ok) toast("数据已刷新", "success");
  } finally {
    setBusy(button, false);
  }
});
$("#apply-gross-basis").addEventListener("click", async (e) => {
  const button = e.currentTarget;
  setBusy(button, true, "正在确认口径…");
  setText("gross-basis-result", "正在为现有订单写入审计记录…");
  try {
    const r = await api(`/api/stores/${state.store}/payment-basis-reviews/apply-gross`, {
      method: "POST",
      body: JSON.stringify({ note: "店主确认：订单导出的买家实付金额为退款前金额" }),
    });
    setText("gross-basis-result", `已确认 ${r.created} 笔，跳过 ${r.skipped} 笔已有记录。`);
    toast("现有订单已按退款前实付口径确认", "success");
    await loadReport();
  } catch (err) {
    setText("gross-basis-result", err.message);
    toast(err.message, "error");
  } finally {
    setBusy(button, false);
  }
});
async function loadDateRange() {
  if (!state.store) return;
  const context = scope();
  try {
    const r = await api(`/api/stores/${context.store}/date-range`);
    if (!current(context)) return;
    if (r.start && r.end) {
      $("#start-date").value = r.start;
      $("#end-date").value = r.end;
      $("#date-range-note").textContent = `已覆盖全部订单：${r.start} 至 ${r.end}`;
    } else {
      $("#date-range-note").textContent = "还没有订单，先导入千牛账单";
    }
    state.orderLimit = 50;
    loadReport();
  } catch (err) {
    if (current(context)) $("#date-range-note").textContent = "日期范围读取失败，请点更新重试";
  }
}
function reportPath() {
  return `/api/stores/${state.store}/report?${new URLSearchParams({ start: $("#start-date").value, end: $("#end-date").value })}`;
}
async function loadReport() {
  if (!state.store) return false;
  let context = scope(),
    seq = ++state.reportSeq;
  const metrics = $("#overview-metrics");
  if (metrics) metrics.setAttribute("aria-busy", "true");
  try {
    let r = await api(reportPath());
    if (!current(context) || seq !== state.reportSeq) return false;
    if (r.cashflow_review_version !== 2) {
      state.report = null;
      state.refundReview = null;
      state.basisReview = null;
      $("#basis-review-panel").hidden = true;
      $("#refund-review-panel").hidden = true;
      for (const id of ["profit-value", "sales-value", "cost-value"])
        setText(id, "—");
      setText("settled-count", "0");
      setText("orders-list", "服务版本尚未更新，请重启服务后查看。");
      setText("as-of", "服务版本尚未更新");
      setText("profit-sub", "请重启本地服务后再刷新");
      setText("scope-note", "");
      return false;
    }
    state.report = r;
    setText("as-of", r.as_of
      ? `最近导入 ${new Date(r.as_of).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false })} 北京时间`
      : "尚未导入数据");
    let k = r.known,
      c = r.counts;
    setText("profit-value", c.settled > 0 ? money(k.profit_fen) : "—");
    setText("sales-value", c.paid_items > 0 ? money(r.source_snapshot.payment_fen) : "—");
    setText("cost-value", c.settled > 0 ? money(k.cost_fen) : "—");
    setText("settled-count", String(c.settled));
    setText("profit-sub", c.settled > 0
      ? `来自 ${c.settled} 笔信息完整的有效订单`
      : `还有 ${c.needs_review} 笔订单待补齐信息`);
    setText("scope-note",
      `统计按订单付款日期归属；退款回冲原订单。${c.missing_paid_time_all_dates ? `另有 ${c.missing_paid_time_all_dates} 条记录缺少付款时间，未纳入统计。` : ""}`);
    const reasonCounts = {};
    r.items.filter((x) => x.reasons.length).forEach((x) =>
      x.reasons.forEach((reason) => { reasonCounts[reason] = (reasonCounts[reason] || 0) + 1; }),
    );
    const reasonText = Object.entries(reasonCounts)
      .sort((a, b) => b[1] - a[1])
      .map(([reason, count]) => `${esc(reason)} ${count} 笔`)
      .join("；");
    const reviewSummary = $("#review-summary");
    if (reviewSummary) reviewSummary.innerHTML = c.needs_review
      ? `<strong>还有 ${c.needs_review} 笔需要处理</strong><span>${reasonText || "请打开待核对列表查看"}</span>`
      : `<strong>这段时间没有待核对订单</strong><span>可以直接查看已核算结果或问账</span>`;
    updateGuidedWorkflow();
    renderOrders();
    renderOverviewCategoryMix(r);
    return true;
  } catch (err) {
    if (current(context) && seq === state.reportSeq) toast(err.message, "error");
    return false;
  } finally {
    if (current(context) && seq === state.reportSeq && metrics)
      metrics.setAttribute("aria-busy", "false");
  }
}
async function loadCashflows(seq = state.reportSeq) {
  let context = scope(),
    store = context.store;
  try {
    let q = new URLSearchParams({
      start: $("#start-date").value,
      end: $("#end-date").value,
    });
    let r = await api(`/api/stores/${store}/cashflows?${q}`);
    if (!current(context) || seq !== state.reportSeq) return;
    let has = r.count > 0;
    $("#cashflow-income").textContent = has ? money(r.income_fen) : "—";
    $("#cashflow-refund").textContent = has ? money(r.refund_fen) : "—";
    $("#cashflow-net").textContent = has ? money(r.net_fen) : "—";
    $("#cashflow-count").textContent = has ? r.count : "—";
    $("#cashflow-list").innerHTML = r.items.length
      ? r.items
          .slice(0, 100)
          .map(
            (x) =>
              `<div class="history-row"><span>${esc(x.settled_at)} · ${x.kind === "income" ? "货款收入" : "售后退款"} ${money(x.amount_fen)} · 子订单 ${esc(x.item_id)}</span><span>批次 ${esc(x.batch_id)}${x.matched_order ? "" : " · 未匹配订单"}</span></div>`,
          )
          .join("") +
        (r.count > 100
          ? "<p>仅展示前 100 条；汇总包含当前范围全部流水。</p>"
          : "")
      : '<div class="empty">当前日期范围没有已导入的货款流水。</div>';
  } catch (err) {
    if (current(context) && seq === state.reportSeq) toast(err.message);
  }
}
function renderOverviewCategoryMix(report) {
  const container = $("#overview-category-mix");
  if (!container) return;
  const counts = { finished: 0, activation: 0, family: 0, other: 0 };
  for (const item of report?.items || []) counts[orderCategory(item)] += 1;
  const total = Object.values(counts).reduce((sum, count) => sum + count, 0);
  if (!total) {
    container.innerHTML = '<div class="empty">导入订单后显示类目。</div>';
    return;
  }
  container.innerHTML = Object.entries(counts)
    .map(([key, count]) => {
      const ratio = Math.round((count / total) * 100);
      return `<button type="button" class="overview-category-item" data-order-category="${key}"><span class="category-swatch category-swatch-${key}" aria-hidden="true"></span><span class="overview-category-copy"><strong>${ORDER_CATEGORY_LABELS[key]}</strong><small>${count} 条订单</small></span><b>${ratio}%</b></button>`;
    })
    .join("");
}
function renderOrders() {
  let r = state.report;
  if (!r) return;
  const query = $("#order-search").value.trim().toLowerCase();
  let items = $("#review-only").checked
    ? r.items.filter((x) => x.reasons.length)
    : r.items;
  const categoryFilter = $("#order-category-filter")?.value || "all";
  if (categoryFilter !== "all") items = items.filter((x) => orderCategory(x) === categoryFilter);
  if (query) {
    items = items.filter((x) =>
      [x.item_id, x.order_id, x.title, x.sku, x.property, x.paid_at]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(query)),
    );
  }
  const visible = items.slice(0, state.orderLimit);
  state.visibleOrderIds = visible.map((x) => x.item_id);
  const availableIds = new Set(r.items.map((x) => x.item_id));
  for (const itemId of state.selectedOrderIds) {
    if (!availableIds.has(itemId)) state.selectedOrderIds.delete(itemId);
  }
  $("#order-total").textContent = `${items.length} 条付款子订单`;
  $("#orders-list").innerHTML = visible.length
    ? visible
        .map(
          (x) =>
            (() => {
              const category = orderCategory(x);
              const profitValue = x.profit_fen !== null && x.profit_fen !== undefined ? x.profit_fen : x.estimated_profit_fen;
              return `<div class="order-row ${state.selectedOrderIds.has(x.item_id) ? "is-selected" : ""}" data-order-row="${esc(x.item_id)}"><div class="order-main"><label class="order-select"><input type="checkbox" data-order-select="${esc(x.item_id)}" aria-label="选择订单 ${esc(x.order_id || x.item_id)}" ${state.selectedOrderIds.has(x.item_id) ? "checked" : ""}></label><div><div class="order-title-line"><strong>${esc(x.title || x.sku)}</strong><span class="order-category-chip order-category-${category}">${ORDER_CATEGORY_LABELS[category]}</span></div><span>${esc(x.sku)}</span><span>主订单 ${esc(x.order_id)} · 子订单 ${esc(x.item_id)} · ${esc(x.paid_at || "")}</span></div></div><div><span>退款后实收</span><span class="amount">${money(x.net_sales_fen)}</span><span>${fulfillmentLabel(x.fulfillment_status)} · ${refundLabel(x.refund_status)} · 成本 ${money(x.cost_fen)}</span></div><div><span>利润</span><span class="amount">${money(profitValue)}</span><span class="issue">${esc(orderIssueLabel(x.reasons, x.estimated_profit_fen))}</span></div><div class="order-actions">${orderActionMarkup(x)}</div></div>`;
            })(),
        )
        .join("")
    : `<div class="empty"><p>${query ? "没有找到匹配的订单。" : "这个时间段没有符合条件的订单。"}</p>${query ? '<button type="button" class="button secondary" id="clear-order-search">清空搜索</button>' : ""}</div>`;
  const more = $("#orders-load-more");
  if (more) more.hidden = visible.length >= items.length;
  syncOrderSelectionUI();
}
$("#review-only").addEventListener("change", () => {
  state.orderLimit = 50;
  renderOrders();
});
$("#order-search").addEventListener("input", () => {
  state.orderLimit = 50;
  renderOrders();
});
$("#order-category-filter")?.addEventListener("change", () => {
  state.orderLimit = 50;
  renderOrders();
});
$("#orders-load-more button")?.addEventListener("click", () => {
  state.orderLimit += 50;
  renderOrders();
});
function syncOrderSelectionUI() {
  const count = state.selectedOrderIds.size;
  setText("selected-order-count", `已选 ${count} 笔`);
  const deleteButton = $("#delete-selected-orders");
  if (deleteButton) deleteButton.disabled = count === 0;
  const selectAll = $("#order-select-all");
  const visibleIds = state.visibleOrderIds;
  const selectedVisible = visibleIds.filter((itemId) => state.selectedOrderIds.has(itemId)).length;
  if (selectAll) {
    selectAll.checked = visibleIds.length > 0 && selectedVisible === visibleIds.length;
    selectAll.indeterminate = selectedVisible > 0 && selectedVisible < visibleIds.length;
  }
}
$("#order-select-all")?.addEventListener("change", (event) => {
  for (const itemId of state.visibleOrderIds) {
    if (event.target.checked) state.selectedOrderIds.add(itemId);
    else state.selectedOrderIds.delete(itemId);
  }
  renderOrders();
});
document.addEventListener("change", (event) => {
  const checkbox = event.target.closest("[data-order-select]");
  if (!checkbox) return;
  if (checkbox.checked) state.selectedOrderIds.add(checkbox.dataset.orderSelect);
  else state.selectedOrderIds.delete(checkbox.dataset.orderSelect);
  const row = checkbox.closest("[data-order-row]");
  row?.classList.toggle("is-selected", checkbox.checked);
  syncOrderSelectionUI();
});
$("#delete-selected-orders")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const itemIds = [...state.selectedOrderIds];
  if (!itemIds.length) return;
  if (!window.confirm(`确定永久删除选中的 ${itemIds.length} 笔订单吗？对应的退款、流水和核对记录也会一并移除。`)) return;
  setBusy(button, true, "删除中…");
  try {
    const result = await api(`/api/stores/${state.store}/orders`, {
      method: "DELETE",
      body: JSON.stringify({ item_ids: itemIds }),
    });
    state.selectedOrderIds.clear();
    toast(`已删除 ${result.deleted} 笔订单记录${result.missing ? `，${result.missing} 笔已不存在` : ""}`);
    await Promise.all([loadReport(), loadCardLedger()]);
  } catch (err) {
    toast(err.message, "error");
  } finally {
    setBusy(button, false);
  }
});
document.addEventListener("click", (e) => {
  if (e.target.closest("#clear-order-search")) {
    $("#order-search").value = "";
    state.orderLimit = 50;
    renderOrders();
    $("#order-search").focus();
    return;
  }
  const categoryButton = e.target.closest("[data-order-category]");
  if (categoryButton) {
    goto("orders");
    const select = $("#order-category-filter");
    if (select) select.value = categoryButton.dataset.orderCategory;
    state.orderLimit = 50;
    renderOrders();
    return;
  }
  let b = e.target.closest("[data-fix]");
  if (b) {
    $("#adjust-item").value = b.dataset.fix;
    state.adjustmentKey = null;
    loadAdjustmentHistory();
    goto("orders");
    setTimeout(
      () =>
        $("#adjustment-form").scrollIntoView({
          behavior: "smooth",
          block: "center",
        }),
      50,
    );
  }
});
async function loadBasisHistory(selected) {
  const rows = await api(
    `/api/stores/${selected.store}/payment-basis-reviews/${encodeURIComponent(selected.item)}`,
  );
  if (!current(selected.context) || state.basisReview !== selected) return;
  $("#basis-review-history").innerHTML = rows.length
    ? rows
        .map(
          (row) =>
            `<div class="history-row"><span>记录 ${esc(row.id)} · ${row.basis === "gross" ? "退款前实付" : "已扣退款净额"} · ${esc(row.note)}</span><span>${esc(row.created_at.slice(0, 10))} · 操作人 ${esc(row.reviewer_id)}</span></div>`,
        )
        .join("")
    : '<p class="muted">暂无口径核对记录。</p>';
}
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-basis-review]");
  if (!button) return;
  const item = state.report?.items.find(
    (row) => row.item_id === button.dataset.basisReview,
  );
  if (!item?.basis_source_fingerprint) return;
  const selected = {
    item: item.item_id,
    fingerprint: item.basis_source_fingerprint,
    store: state.store,
    context: scope(),
    key: null,
  };
  state.basisReview = selected;
  $("#basis-review-item").value = item.item_id;
  $("#basis-review-choice").value = "";
  $("#basis-review-note").value = "";
  $("#basis-review-source").textContent =
    `订单实付快照 ${money(item.payment_fen)}；退款快照 ${money(item.refund_fen)}；${item.refund_review_id ? "已核对累计退款 " + money(item.reviewed_refund_fen) : "退款历史尚未确认或不适用"}。${item.basis_review_stale ? "之前的口径核对已失效。" : ""}`;
  $("#basis-review-panel").hidden = false;
  try {
    await loadBasisHistory(selected);
  } catch (err) {
    if (current(selected.context)) toast(err.message);
  }
  if (current(selected.context) && state.basisReview === selected)
    $("#basis-review-panel").scrollIntoView({
      behavior: "smooth",
      block: "center",
    });
});
$("#basis-review-form").addEventListener("input", () => {
  if (state.basisReview) state.basisReview.key = null;
});
$("#basis-review-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const selected = state.basisReview;
  if (!selected || !current(selected.context)) return;
  const button = event.submitter;
  setBusy(button, true);
  selected.key = selected.key || crypto.randomUUID();
  try {
    await api(`/api/stores/${selected.store}/payment-basis-reviews`, {
      method: "POST",
      body: JSON.stringify({
        request_id: selected.key,
        item_id: selected.item,
        source_fingerprint: selected.fingerprint,
        basis: $("#basis-review-choice").value,
        note: $("#basis-review-note").value,
      }),
    });
    if (!current(selected.context) || state.basisReview !== selected) return;
    toast("金额口径核对已保存");
    await loadBasisHistory(selected);
    await loadReport();
    $("#basis-review-panel").hidden = true;
    state.basisReview = null;
  } catch (err) {
    if (current(selected.context)) toast(err.message);
  } finally {
    setBusy(button, false);
  }
});
$("#import-kind")?.addEventListener("change", () => {
  resetImport();
});
$("#import-file")?.addEventListener("change", resetImport);
function invalidateImportPreview() {
  state.importRevision++;
  state.batch = null;
  state.conflicts = false;
  state.changesToken = null;
  state.multiConfirmed = false;
  $("#commit-import").hidden = true;
  if (state.columns)
    $("#import-result").textContent = "选择已变化，请重新预览后再导入。";
}
$("#payment-basis")?.addEventListener("change", invalidateImportPreview);
$("#mapping-fields")?.addEventListener("change", (e) => {
  if (e.target.matches("[data-map]")) invalidateImportPreview();
});
function resetImport() {
  invalidateImportPreview();
  state.columns = null;
  $("#preview-import").hidden = true;
  $("#commit-import").hidden = true;
  $("#file-count").textContent = "等待文件";
  const mappings = $("#mapping-fields");
  if (mappings) mappings.innerHTML = '<div class="empty">字段将自动识别。</div>';
  $("#import-result").innerHTML = "";
}
const diffLabels = {
  item_external_id: "子订单编号",
  order_external_id: "主订单编号",
  external_id: "退款编号",
  sku: "规格",
  product_title: "商品标题",
  quantity: "数量",
  paid_at: "付款时间",
  status: "状态",
  payment_fen: "实付金额",
  payment_basis: "金额口径",
  refund_status: "退款状态",
  refund_snapshot_fen: "退款金额",
  amount_fen: "金额",
  completed_at: "退款完成时间",
  valid_from: "成本生效时间",
  unit_cost_fen: "单位成本",
  supplier: "供应商",
  settled_at: "打款时间",
};
function differenceDetails(diffs) {
  if (!diffs?.length) return "<p>没有发现对现有记录的修改。</p>";
  return `<details open><summary>${diffs.length} 行已有记录发生变化，查看新旧值</summary><ul>${diffs
    .map(
      (d) =>
        `<li>第 ${esc(d.row)} 行<ul>${Object.entries(d.fields)
          .map(([key, v]) => {
            const fmt = (x) =>
              x === null || x === undefined || x === ""
                ? "未提供"
                : key.endsWith("_fen")
                  ? money(x)
                  : String(x);
            return `<li>${esc(diffLabels[key] || key)}：${esc(fmt(v.old))} → ${esc(fmt(v.new))}</li>`;
          })
          .join("")}</ul></li>`,
    )
    .join("")}</ul></details>`;
}
function formFile() {
  let f = new FormData();
  f.append("kind", $("#import-kind").value);
  f.append("file", $("#import-file").files[0]);
  return f;
}
function renderImportMappings(r) {
  $("#file-count").textContent = `${r.row_count} 行`;
  const mappings = $("#mapping-fields");
  if (!mappings) return;
  mappings.innerHTML = r.fields
    .map(
      (k) =>
        `<label class="mapping-line"><span>${esc(labels[k] || k)} ${r.required.includes(k) ? '<span class="required">*</span>' : ""}</span><select data-map="${esc(k)}"><option value="">不导入</option>${r.headers.map((h) => `<option value="${esc(h)}" ${r.suggested[k] === h ? "selected" : ""}>${esc(h)}</option>`).join("")}</select></label>`,
    )
    .join("");
}

async function commitImportBatch(button, context = scope(), revision = state.importRevision) {
  if (!state.batch) return;
  let locked = $$(
    "#import-form input, #import-form select, #import-form button, #mapping-fields select, #preview-import",
  ).map((el) => [el, el.disabled]);
  locked.forEach(([el]) => (el.disabled = true));
  setBusy(button, true, "导入中…");
  try {
    let r = await api(
      `/api/stores/${state.store}/imports/${state.batch}/commit`,
      {
        method: "POST",
        body: JSON.stringify({
          accept_changes: state.conflicts,
          changes_token: state.changesToken,
          confirm_item_amounts: state.multiConfirmed,
        }),
      },
    );
    if (revision !== state.importRevision || !current(context)) return;
    if (r.requires_item_amounts_confirmation) {
      state.multiConfirmed = true;
      $("#import-result").insertAdjacentHTML(
        "afterbegin",
        `<div class="import-message bad">有 ${esc(r.multi_parent_count)} 个主订单包含多个子订单。请先核对每行实付及退款金额均为子订单金额、没有重复分摊；确认后再点击按钮。</div>`,
      );
      button.dataset.originalHtml = "确认金额已拆分";
      button.textContent = "确认金额已拆分";
      return;
    }
    if (r.conflicts) {
      state.conflicts = true;
      state.changesToken = r.changes_token;
      $("#import-result").innerHTML =
        `<div class="import-message bad">请核对变更前后的数据，并确认新文件较新；如核对期间记录再次变化，系统会要求重新确认。${differenceDetails(r.differences)}</div>`;
      button.dataset.originalHtml = "确认更新";
      button.textContent = "确认更新";
      return;
    }
    $("#import-result").innerHTML =
      `<div class="import-message ok">导入完成：新增 ${esc(r.inserted || 0)} 条，更新 ${esc(r.updated || 0)} 条。</div>`;
    button.hidden = true;
    toast("数据已导入", "success");
    await loadDateRange();
    loadBatches();
  } catch (err) {
    $("#import-result").innerHTML =
      `<div class="import-message bad">${esc(err.message)}</div>`;
    toast(err.message, "error");
  } finally {
    locked.forEach(([el, disabled]) => (el.disabled = disabled));
    setBusy(button, false);
  }
}

async function showImportPreview(r, name, button, autoCommit, context, revision) {
  state.batch = r.batch_id;
  state.conflicts = false;
  state.changesToken = null;
  state.multiConfirmed = false;
  const errors = r.errors || [];
  $("#file-count").textContent = r.row_count ? `${r.row_count} 行` : "等待文件";
  const differences = Array.isArray(r.differences) ? r.differences : [];
  const needsConfirmation = Boolean(r.multi_parent_count || differences.length);
  $("#commit-import").hidden = !r.batch_id || r.duplicate || errors.length || !needsConfirmation;
  if (errors.length) {
    state.batch = null;
    $("#import-result").innerHTML =
      `<div class="import-message bad">请修正文件：${errors.map((x) => `第 ${esc(x.row)} 行 ${esc(x.message)}`).join("；")}</div>`;
    return;
  }
  if (r.duplicate) {
    $("#import-result").innerHTML =
      `<div class="import-message">${esc(name || "这个文件")} 已经导入过，无需重复导入。</div>`;
    return;
  }
  const time = r.time_start
    ? `${esc(labels[r.time_field] || r.time_field)}：${esc(r.time_start)} 至 ${esc(r.time_end)}；${esc(r.time_missing)} 行缺少该时间`
    : "文件中没有可用的时间";
  $("#import-result").innerHTML =
    `<div class="import-message ${needsConfirmation ? "bad" : "ok"}">${esc(name || "文件")} 已识别 ${esc(r.row_count)} 行。${time}${r.multi_parent_count ? `<p>其中 ${esc(r.multi_parent_count)} 个主订单有多个子订单，需要确认金额已拆分。</p>` : ""}${differenceDetails(differences)}</div>`;
  if (autoCommit && !needsConfirmation && r.batch_id) {
    await commitImportBatch(button, context, revision);
  } else if (needsConfirmation) {
    $("#import-result").insertAdjacentHTML(
      "beforeend",
      '<p class="muted">请检查上面的变化，确认后点“确认导入”。</p>',
    );
  }
}

async function previewSelectedImport(button, autoCommit = false) {
  if (!state.columns || !$("#import-file").files.length) return;
  let revision = state.importRevision,
    context = scope(),
    f = formFile(),
    mapping = {};
  $$("[data-map]").forEach((x) => {
    if (x.value) mapping[x.dataset.map] = x.value;
  });
  f.append("mapping", JSON.stringify(mapping));
  f.append("basis", $("#payment-basis").value);
  let r = await api(`/api/stores/${state.store}/imports/preview`, {
    method: "POST",
    body: f,
  });
  if (revision !== state.importRevision || !current(context)) return;
  await showImportPreview(r, $("#import-file").files[0]?.name, button, autoCommit, context, revision);
}

$("#import-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  let btn = e.submitter;
  if (!$("#import-file").files.length) {
    toast("请先选择千牛导出文件", "error");
    return;
  }
  invalidateImportPreview();
  let revision = state.importRevision,
    context = scope();
  setBusy(btn, true, "自动识别中…");
  try {
    let r = await api(`/api/stores/${state.store}/imports/columns`, {
      method: "POST",
      body: formFile(),
    });
    if (revision !== state.importRevision || !current(context)) return;
    state.columns = r;
    state.kind = $("#import-kind").value;
    renderImportMappings(r);
    $("#preview-import").hidden = false;
    $("#commit-import").hidden = true;
    $("#import-result").innerHTML =
      '<div class="import-message">字段已自动识别，正在检查重复和金额冲突…</div>';
    await previewSelectedImport(btn, true);
  } catch (err) {
    if (revision === state.importRevision && current(context)) {
      $("#import-result").innerHTML =
        `<div class="import-message bad">${esc(err.message)}</div>`;
      toast(err.message, "error");
    }
  } finally {
    setBusy(btn, false);
  }
});

$("#preview-import").addEventListener("click", async (e) => {
  let btn = e.currentTarget;
  setBusy(btn, true, "重新检查中…");
  try {
    await previewSelectedImport(btn, false);
  } catch (err) {
    $("#import-result").innerHTML =
      `<div class="import-message bad">${esc(err.message)}</div>`;
    toast(err.message, "error");
  } finally {
    setBusy(btn, false);
  }
});

$("#commit-import").addEventListener("click", async (e) => {
  await commitImportBatch(e.currentTarget);
});

$("#latest-download-btn").addEventListener("click", async (e) => {
  const button = e.currentTarget;
  state.columns = null;
  state.kind = $("#import-kind").value;
  invalidateImportPreview();
  const context = scope();
  const revision = state.importRevision;
  setBusy(button, true, "正在读取下载…");
  try {
    const r = await api(`/api/stores/${context.store}/imports/latest-download`, {
      method: "POST",
      body: JSON.stringify({
        kind: $("#import-kind").value,
        basis: $("#import-kind").value === "orders" ? $("#payment-basis").value : null,
      }),
    });
    if (!current(context) || revision !== state.importRevision) return;
    await showImportPreview(r, r.file_name, $("#commit-import"), true, context, revision);
  } catch (err) {
    $("#import-result").innerHTML =
      `<div class="import-message bad">${esc(err.message)}</div>`;
    toast(err.message, "error");
  } finally {
    setBusy(button, false);
  }
});
async function loadBatches() {
  if (!state.store || !$("#batch-list")) return;
  let context = scope();
  try {
    let r = await api(`/api/stores/${context.store}/imports`);
    if (!current(context)) return;
    let names = {
      orders: "订单",
      refunds: "退款",
      costs: "成本",
      cashflows: "货款收入账单",
      card_purchases: "卡网采购单",
    };
    $("#batch-list").innerHTML = r.length
      ? r
          .map(
            (x) =>
              `<div class="history-row"><span>${esc(names[x.kind] || x.kind)} · ${x.row_count} 行</span><span>${esc(x.committed_at ? new Date(x.committed_at).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : "")} 北京时间 · 批次 ${x.id}</span></div>`,
          )
          .join("")
      : '<div class="empty">暂无导入记录</div>';
  } catch (err) {
    if (current(context)) toast(err.message);
  }
}
async function loadCosts({ preserveCardResult = false } = {}) {
  if (!state.store) return;
  let context = scope();
  try {
    let [items, costs, purchases, matches, cardSettings] = await Promise.all([
      api(`/api/stores/${context.store}/skus`),
      api(`/api/stores/${context.store}/costs`),
      api(`/api/stores/${context.store}/card-purchases`),
      api(`/api/stores/${context.store}/card-matches`),
      api(`/api/stores/${context.store}/card-settings`),
    ]);
    if (!current(context)) return;
    if (state.loadedSettingsStore !== context.store) {
      const emailInput = $("#card-contact-email");
      if (emailInput) emailInput.value = cardSettings.contact_email || "";
      state.loadedSettingsStore = context.store;
    }
    setText(
      "card-settings-status",
      cardSettings.contact_email
        ? `已保存联系方式：${cardSettings.contact_email}${cardSettings.updated_at ? "（更新于 " + formatCardSettingTime(cardSettings.updated_at) + "）" : ""}`
        : "尚未保存联系方式；填写邮箱后可直接 AI 查询卡网订单。",
    );
    $("#sku-count").textContent = `${items.length} 个规格`;
    $("#sku-options").innerHTML = items
      .map((x) => `<option value="${esc(x.sku)}">${esc(x.title)}</option>`)
      .join("");
    $("#sku-list").innerHTML = items.length
      ? items
          .map(
            (x) =>
              `<div class="sku-row"><div><button data-sku="${esc(x.sku)}">${esc(x.sku)}</button><small>${esc(x.title)}</small></div><span>${x.missing_cost} 笔子订单缺成本</span></div>`,
          )
          .join("")
      : '<div class="empty"><p>还没有商品。先导入千牛订单账单。</p><button type="button" class="button secondary" data-goto="import">先导入订单</button></div>';
    $("#cost-list").innerHTML = costs.length || matches.length
      ? costs
          .map(
            (x) =>
              `<div class="cost-row"><div><strong>${esc(x.sku)}</strong><small>版本 ${esc(x.id)} · 生效：${esc(x.valid_from)} ${x.supplier ? " · " + esc(x.supplier) : ""}</small></div><strong>${money(x.unit_cost_fen)}</strong></div>`,
          )
          .join("") +
        matches
          .map(
            (x) =>
              `<div class="cost-row"><div><strong>${esc(x.sale_title || x.item_external_id)}</strong><small>订单 ${esc(x.order_external_id || x.item_external_id)} · 卡网单 ${esc(x.purchase_external_id)}${x.quantity > 1 ? `（分配 ${x.allocated_quantity}/${x.quantity} 件）` : ""} · ${esc(x.purchase_paid_at || "采购时间未知")} · 匹配度 ${x.score}%</small></div><strong>${money(x.cost_fen)}</strong></div>`,
          )
          .join("")
      : '<div class="empty"><p>还没有成本记录。补上成本后，利润才会开始核算。</p><button type="button" class="button secondary" data-goto="orders">去处理待核对</button></div>';
    if (!preserveCardResult) {
      $("#card-import-result").innerHTML = purchases.length
        ? `<div class="import-message ok">已保存 ${purchases.length} 张卡网采购单，最近一张：${esc(purchases[0].purchase_external_id)} · ${esc(purchases[0].product_title)}</div>`
        : '<div class="import-message">还没有卡网采购单。先保存联系方式并使用 AI 查询或粘贴导入。</div>';
    }
    await loadCardLedger(context);
    updateGuidedWorkflow();
    if (purchases.length > 0 && !state.cardMatches && current(context)) {
      runCostMatches(null, { silent: true });
    }
  } catch (err) {
    if (current(context)) toast(err.message);
  }
}

function formatCardSettingTime(iso) {
  if (!iso) return "";
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return String(iso).slice(0, 19).replace("T", " ");
  return dt.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
}

function formatCardImportSummary(r, prefix = "采购单已保存") {
  const unchanged = !r.duplicate && !r.inserted && !r.updated;
  if (r.duplicate || unchanged) {
    return `这些采购单已经导入过，已按订单号去重（跳过 ${r.skipped ?? r.row_count} 条），未重复入账。`;
  }
  return `${prefix}：新增 ${r.inserted} 条，更新 ${r.updated} 条，按订单号去重跳过 ${r.skipped ?? 0} 条（共读取 ${r.row_count} 条）。`;
}

function shortCardSource(source) {
  const raw = String(source || "").trim();
  if (!raw) return "";
  if (raw.startsWith("16688") && raw.length > 14) return "16688 AI查询";
  if ((raw.startsWith("链动") || raw.toLowerCase().includes("wzyp")) && raw.length > 14) return "链动小铺 AI查询";
  return raw;
}

async function importCardPurchaseText(text, button, filename = "card-paste.txt") {
  const clean = String(text || "").trim();
  if (!clean) {
    toast("请先粘贴卡网订单内容", "error");
    return false;
  }
  setBusy(button, true, "正在识别粘贴内容…");
  try {
    const r = await api(`/api/stores/${state.store}/card-purchases/import-text`, {
      method: "POST",
      body: JSON.stringify({ text: clean, filename }),
    });
    const unchanged = !r.duplicate && !r.inserted && !r.updated;
    $("#card-import-result").innerHTML =
      `<div class="import-message ok">${esc(formatCardImportSummary(r))}</div>`;
    toast(r.duplicate || unchanged ? "采购单已去重，未重复入账" : "卡网采购单已导入", "success");
    await loadCosts({ preserveCardResult: true });
    await loadBatches();
    return true;
  } catch (err) {
    const issues = Array.isArray(err.issues) ? err.issues : [];
    $("#card-import-result").innerHTML =
      `<div class="import-message bad"><strong>${esc(err.message)}</strong>${
        issues.length
          ? `<details><summary>查看 ${issues.length} 条明细</summary><ul>${issues.map((issue) => `<li>${esc(issue)}</li>`).join("")}</ul></details>`
          : ""
      }</div>`;
    toast(err.message, "error");
    return false;
  } finally {
    setBusy(button, false);
  }
}

async function uploadCardPurchaseFile(file, button) {
  if (!file) {
    toast("请先选择卡网采购单文件", "error");
    return false;
  }
  const data = new FormData();
  data.append("file", file);
  setBusy(button, true, "正在识别卡网单…");
  try {
    const r = await api(`/api/stores/${state.store}/card-purchases/import`, {
      method: "POST",
      body: data,
    });
    const unchanged = !r.duplicate && !r.inserted && !r.updated;
    $("#card-import-result").innerHTML =
      `<div class="import-message ok">${esc(formatCardImportSummary(r))}</div>`;
    toast(r.duplicate || unchanged ? "采购单已存在，未重复入账" : "卡网采购单已导入", "success");
    await loadCosts({ preserveCardResult: true });
    await loadBatches();
    return true;
  } catch (err) {
    const issues = Array.isArray(err.issues) ? err.issues : [];
    $("#card-import-result").innerHTML =
      `<div class="import-message bad"><strong>${esc(err.message)}</strong>${
        issues.length
          ? `<details><summary>查看 ${issues.length} 条明细</summary><ul>${issues.map((issue) => `<li>${esc(issue)}</li>`).join("")}</ul></details>`
          : ""
      }</div>`;
    toast(err.message, "error");
    return false;
  } finally {
    setBusy(button, false);
  }
}

function filteredSuggestions(r) {
  if (!r || !Array.isArray(r.suggestions)) return [];
  const filter = state.matchTierFilter || "all";
  if (filter === "all") return r.suggestions;
  return r.suggestions.filter((sale) => {
    const best = sale.candidates?.[0];
    if (!best) return false;
    if (filter === "auto") return Boolean(best.auto_confirmable);
    const lvl = best.confidence_level || (best.score >= 82 ? "high" : best.score >= 68 ? "medium" : "review");
    return lvl === filter;
  });
}

function renderMatchSummaryDashboard(r) {
  const dash = $("#match-summary-dashboard");
  if (!dash) return;
  if (!r || !r.purchase_count || !r.suggestions?.length) {
    dash.hidden = true;
    dash.innerHTML = "";
    return;
  }
  const sm = r.smart_summary || {};
  const tb = sm.tier_breakdown || {};
  const confirmedNow = state.confirmedItems.size;
  const allAutoItems = r.suggestions.filter(
    (s) => s.candidates?.[0]?.auto_confirmable && !state.confirmedItems.has(s.item_id),
  );
  const autoCount = allAutoItems.length;
  const alreadyReconciled = (sm.already_reconciled_count ?? 0) + confirmedNow;
  const totalActive = sm.total_active_orders ?? ((sm.already_reconciled_count ?? 0) + (sm.actionable_missing_count ?? r.missing_cost_count ?? 0));
  const progressPct = totalActive > 0 ? Math.min(100, Math.round((alreadyReconciled * 100) / totalActive)) : 100;
  const projectedPct = totalActive > 0 ? Math.min(100, Math.round(((alreadyReconciled + autoCount) * 100) / totalActive)) : 100;
  const effWinPct = sm.effective_window_coverage_pct ?? sm.window_coverage_pct ?? (sm.overall_coverage_pct ?? 0);
  const effWinMatched = sm.window_matched_count ?? r.suggestions.length;
  const effWinMissing = sm.effective_window_missing_count ?? sm.window_missing_count ?? r.missing_cost_count ?? 0;
  const effWinTotal = effWinMatched + effWinMissing;
  const overallPct = sm.overall_coverage_pct ?? 0;
  const matchedSalesPct = sm.matched_sales_pct ?? 0;
  const autoSalesPct = sm.auto_confirmable_sales_pct ?? 0;
  const aiStats = r.ai_stats || {};
  const highCnt = tb.high ?? 0;
  const medCnt = tb.medium ?? 0;
  const revCnt = tb.review ?? 0;
  const totalSug = Math.max(1, r.suggestions.length);
  const wHigh = Math.round((highCnt * 100) / totalSug);
  const wMed = Math.round((medCnt * 100) / totalSug);
  const wRev = Math.max(0, 100 - wHigh - wMed);
  const skippedRef = r.skipped_refunded_count ?? sm.unmatched_reasons?.skipped_refunded ?? 0;
  const beforeWin = sm.unmatched_reasons?.before_purchase_window ?? 0;
  const tips = Array.isArray(sm.diagnostic_tips) ? sm.diagnostic_tips : [];
  const activeFilter = state.matchTierFilter || "all";

  dash.hidden = false;
  dash.innerHTML = `
    <div class="ratio-top-row">
      <div class="ratio-metrics">
        <div class="ratio-metric">
          <span>全店成本核清进度</span>
          <strong class="accent">${autoCount > 0 && projectedPct > progressPct ? `${progressPct}% → ${projectedPct}%` : `${progressPct}%`}</strong>
          <progress class="metric-progress" value="${progressPct}" max="100" aria-label="当前成本核清进度">${progressPct}%</progress>
          <small>已核 ${alreadyReconciled} / 共 ${totalActive} 笔${autoCount > 0 ? `（一键再核 ${autoCount} 笔）` : ""}</small>
        </div>
        <div class="ratio-metric">
          <span>可对账周期命中率</span>
          <strong>${effWinPct}%</strong>
          <progress class="metric-progress" value="${effWinPct}" max="100" aria-label="可对账周期命中率">${effWinPct}%</progress>
          <small>命中 ${effWinMatched} / ${effWinTotal} 笔（覆盖待核销售额 ${matchedSalesPct}%${beforeWin ? ` · 另有 ${beforeWin} 笔早于采购单` : ""}）</small>
        </div>
        <div class="ratio-metric">
          <span>高置信度建议占比</span>
          <strong>${tb.high_pct ?? wHigh}%</strong>
          <small>高 ${highCnt} · 较 ${medCnt} · 复核 ${revCnt} · 全店待补覆盖 ${overallPct}%</small>
        </div>
        <div class="ratio-metric">
          <span>${aiStats.enabled ? "AI 语义复核与防串单" : "高匹配销售额与防串单"}</span>
          <strong>${skippedRef} 笔隔离</strong>
          <small>${aiStats.enabled ? `AI 已核 ${aiStats.unique_pairs_checked ?? 0} 组规格（调优 ${aiStats.adjusted_candidates ?? 0} 项）` : `一键可核占待核销售额 ${autoSalesPct}%`}</small>
        </div>
      </div>
      ${autoCount > 0
        ? `<button id="batch-confirm-high-btn" type="button" class="button primary">一键确认全部高匹配（${autoCount} 笔）</button>`
        : `<span class="badge">高匹配单已全部处理完毕</span>`}
    </div>
    <div class="segmented-bar" role="img" aria-label="匹配质量分布：高匹配 ${highCnt} 笔，较匹配 ${medCnt} 笔，待复核 ${revCnt} 笔">
      ${highCnt ? `<div class="seg-high" style="width:${Math.max(4, wHigh)}%"></div>` : ""}
      ${medCnt ? `<div class="seg-medium" style="width:${Math.max(4, wMed)}%"></div>` : ""}
      ${revCnt ? `<div class="seg-review" style="width:${Math.max(3, wRev)}%"></div>` : ""}
    </div>
    <div class="match-filter-pills" role="group" aria-label="按匹配置信度筛选建议">
      <button type="button" class="match-tier-pill ${activeFilter === "all" ? "active" : ""}" data-match-tier="all">全部建议 (${r.suggestions.length})</button>
      <button type="button" class="match-tier-pill ${activeFilter === "auto" ? "active" : ""}" data-match-tier="auto">可一键确认 (${autoCount})</button>
      <button type="button" class="match-tier-pill ${activeFilter === "high" ? "active" : ""}" data-match-tier="high">高匹配 (${highCnt})</button>
      <button type="button" class="match-tier-pill ${activeFilter === "medium" ? "active" : ""}" data-match-tier="medium">较匹配 (${medCnt})</button>
      <button type="button" class="match-tier-pill ${activeFilter === "review" ? "active" : ""}" data-match-tier="review">待复核 (${revCnt})</button>
    </div>
    ${tips.length ? `<ul class="diag-tips-list">${tips.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>` : ""}
  `;
}

function buildMatchButtonAttrs(itemId, cand) {
  const allocJson = cand.is_bundle && Array.isArray(cand.allocations) && cand.allocations.length > 1
    ? JSON.stringify(cand.allocations.map((a) => ({
        purchase_id: a.purchase_id,
        allocated_quantity: a.allocated_quantity,
        purchase_quantity: a.purchase_quantity || null,
      })))
    : "";
  const inferredAttr = cand.inferred_quantity && cand.inferred_quantity > (cand.purchase_quantity || 1)
    ? cand.inferred_quantity
    : "";
  return `data-match-item="${esc(itemId)}" data-match-purchase="${cand.purchase_id}" data-match-score="${cand.score}" data-match-inferred-qty="${inferredAttr}" data-match-cost-fen="${cand.effective_cost_fen ?? (cand.amount_fen - cand.refund_fen)}" data-match-ext-id="${esc(cand.purchase_external_id || "")}"${allocJson ? ` data-match-allocations="${esc(allocJson)}"` : ""}`;
}

function renderMatchResults(r) {
  state.cardMatches = r;
  renderMatchSummaryDashboard(r);
  if (!r.purchase_count) {
    $("#match-results").innerHTML =
      '<div class="empty">还没有卡网采购单，先在上方保存邮箱后用 AI 查询或直接粘贴导入。</div>';
    return;
  }
  if (!r.suggestions.length) {
    $("#match-results").innerHTML =
      `<div class="empty"><p>已经找到 ${r.purchase_count} 张采购单，当前没有待核对的缺成本订单。</p><button class="button secondary" type="button" data-goto="orders">查看订单明细</button></div>`;
    return;
  }
  const list = filteredSuggestions(r);
  if (!list.length) {
    $("#match-results").innerHTML =
      '<div class="empty">当前置信度筛选下没有订单，点上方「全部建议」查看其余候选。</div>';
    return;
  }
  const slice = list.slice(0, state.cardMatchLimit);
  if (state.kbMatchIndex >= slice.length) state.kbMatchIndex = Math.max(0, slice.length - 1);

  $("#match-results").innerHTML = slice
    .map((sale, idx) => {
      if (state.confirmedItems.has(sale.item_id)) {
        return `<article class="match-card is-confirmed" data-card-item="${esc(sale.item_id)}">
          <div class="confirmed-inline-banner">✓ 订单 ${esc(sale.order_id)}（${esc(sale.title || sale.sku)}）已确认核对入账</div>
          <span class="badge">已计入纯利润</span>
        </article>`;
      }
      const selIdx = Math.min(state.selectedCandByItem[sale.item_id] || 0, sale.candidates.length - 1);
      const best = sale.candidates[selIdx] || sale.candidates[0];
      const level = best.confidence_level || (best.score >= 82 ? "high" : best.score >= 68 ? "medium" : "review");
      const levelText = level === "high" ? "高匹配" : level === "medium" ? "较匹配" : "建议复核";
      const scoreClass = level === "high" ? "" : level === "medium" ? "medium" : "review";
      const effectiveCost = best.effective_cost_fen ?? (best.amount_fen - best.refund_fen);
      const unitCost = best.unit_cost_fen ?? effectiveCost;
      const gap = best.signed_gap_minutes === null || best.signed_gap_minutes === undefined
        ? (best.time_gap_minutes === null ? "时间未知" : `${best.time_gap_minutes} 分钟`)
        : best.signed_gap_minutes >= 0
          ? `下单后 ${best.signed_gap_minutes} 分钟采购`
          : `采购早于下单 ${Math.abs(best.signed_gap_minutes)} 分钟`;
      const reasonChips = (best.reasons || []).map((x) => `<span class="reason-chip">${esc(x)}</span>`).join("");
      const warnChips = (best.warnings || []).map((x) => `<span class="warn-chip">⚠ ${esc(x)}</span>`).join("");
      const isBundle = best.is_bundle && Array.isArray(best.allocations) && best.allocations.length > 1;
      const bundleLabel = best.bundle_kind === "composite" ? "组合装配（白号+激活）" : "多笔凑单履约";
      const bundleHtml = isBundle
        ? `<div class="bundle-alloc-list"><span class="bundle-pill">✦ ${esc(bundleLabel)}</span>${best.allocations
            .map((a) => `<div>• <code>${esc(a.purchase_external_id)}</code> × ${a.allocated_quantity} 件（${money(a.cost_fen)}）· ${esc(a.product_title || "")}</div>`)
            .join("")}</div>`
        : "";
      const bulkInfo = isBundle
        ? `组合合计成本 ${money(effectiveCost)}`
        : best.quantity > 1
          ? `${best.inferred_quantity > 1 ? "推算 " : ""}单件 ${money(unitCost)} × 本单 ${best.allocated_quantity || sale.quantity} 件（整单共 ${best.quantity} 件，余 ${best.remaining_quantity ?? best.quantity} 件）`
          : `成本 ${money(effectiveCost)}`;
      const marginText = best.margin_fen !== null && best.margin_fen !== undefined ? ` · 预估毛利 ${money(best.margin_fen)}` : "";
      const altHtml = sale.candidates.map((c, cIdx) => {
        if (cIdx === selIdx) return "";
        const cCost = c.effective_cost_fen ?? (c.amount_fen - c.refund_fen);
        const cGap = c.signed_gap_minutes === null || c.signed_gap_minutes === undefined
          ? ""
          : c.signed_gap_minutes >= 0 ? `下单后 ${c.signed_gap_minutes}m` : `早于下单 ${Math.abs(c.signed_gap_minutes)}m`;
        return `<div class="alt-candidate-row">
          <div><strong>[候选 ${cIdx + 1}] ${esc(c.product_title || c.purchase_external_id)}</strong><small>${esc(c.purchase_external_id)} · ${esc(shortCardSource(c.source || ""))} · ${money(cCost)} · ${c.score}%${cGap ? " · " + esc(cGap) : ""}</small></div>
          <button type="button" class="button secondary mini" ${buildMatchButtonAttrs(sale.item_id, c)}>采用此单 ${money(cCost)}</button>
        </div>`;
      }).filter(Boolean).join("");

      return `<article class="match-card${idx === state.kbMatchIndex ? " is-kb-focused" : ""}" data-card-item="${esc(sale.item_id)}" data-card-index="${idx}">
        <div class="match-sale">
          <span class="section-kicker">千牛订单 · ${esc(sale.category_label || "未分类")}${sale.quantity > 1 ? ` · 拍 ${sale.quantity} 件` : ""}</span>
          <strong>${esc(sale.title || sale.sku)}</strong>
          <small>规格 ${esc(sale.sku)} · 数量 ${sale.quantity}</small>
          <small>实付 <strong>${money(sale.payment_fen)}</strong> · <span class="mono-id">${esc(sale.paid_at || "付款时间未知")}</span></small>
          <small class="mono-id">订单 ${esc(sale.order_id)}</small>
        </div>
        <div class="match-candidate">
          <span class="section-kicker">候选 #${selIdx + 1} · ${esc(shortCardSource(best.source || "卡网"))}${best.auto_confirmable ? " · 推荐直接确认" : ""}</span>
          <strong>${esc(best.product_title || best.purchase_external_id)}</strong>
          <small class="mono-id">采购单 ${esc(best.purchase_external_id)} · ${esc(best.seller || "卡网供货商")}</small>
          <small>${esc(bulkInfo)}${esc(marginText)} · ${esc(gap)}</small>
          ${bundleHtml}
          ${reasonChips || warnChips ? `<div class="chip-row">${reasonChips}${warnChips}</div>` : ""}
          ${sale.candidates.length > 1 ? `<details class="alt-candidates"><summary>查看其余 ${sale.candidates.length - 1} 个候选（按键 1-${Math.min(3, sale.candidates.length)} 快速切换）</summary>${altHtml}</details>` : ""}
        </div>
        <div class="match-actions">
          <span class="match-score ${scoreClass}">${levelText} ${best.score}%</span>
          <button type="button" class="button primary primary-confirm-btn" ${buildMatchButtonAttrs(sale.item_id, best)}>确认采用 ${money(effectiveCost)}</button>
        </div>
      </article>`;
    })
    .join("") +
    (list.length > state.cardMatchLimit
      ? `<div class="load-more"><span class="muted">已显示 ${state.cardMatchLimit} / ${list.length} 笔</span><button class="button secondary" type="button" data-show-more-matches>继续显示更多</button></div>`
      : "");
}

async function runCostMatches(button, { silent = false } = {}) {
  if (!state.store) return;
  const context = scope();
  const seq = ++state.matchSeq;
  if (button) setBusy(button, true, "AI 核对中…");
  try {
    const matchScope = $("#match-scope")?.value || "all";
    const useAi = Boolean($("#match-use-ai")?.checked);
    const q = new URLSearchParams({ limit: "300" });
    if (matchScope === "all") {
      q.set("scope", "all");
    } else {
      q.set("start", $("#start-date").value);
      q.set("end", $("#end-date").value);
    }
    if (useAi) q.set("use_ai", "true");
    const r = await api(`/api/stores/${context.store}/cost-match-suggestions?${q}`);
    if (!current(context) || seq !== state.matchSeq) return;
    state.cardMatchLimit = 20;
    state.confirmedItems = new Set();
    state.selectedCandByItem = {};
    state.kbMatchIndex = 0;
    renderMatchResults(r);
    updateGuidedWorkflow();
    if (!silent) {
      const winPct = r.smart_summary?.window_coverage_pct ?? 0;
      toast(
        r.suggestions.length
          ? `智能配对 ${r.suggestions.length} 笔订单（采购期匹配率 ${winPct}%）`
          : "当前没有待核对的缺成本订单",
        "success",
      );
    }
  } catch (err) {
    if (current(context) && seq === state.matchSeq) {
      $("#match-results").innerHTML =
        `<div class="import-message bad">${esc(err.message)}</div>`;
      if (!silent) toast(err.message, "error");
    }
  } finally {
    if (button) setBusy(button, false);
  }
}

$("#match-scope")?.addEventListener("change", () => runCostMatches($("#run-cost-matches")));
$("#match-use-ai")?.addEventListener("change", () => runCostMatches($("#run-cost-matches")));

$("#run-cost-matches").addEventListener("click", (e) => runCostMatches(e.currentTarget));
document.addEventListener("click", (e) => {
  const button = e.target.closest("[data-show-more-matches]");
  if (!button || !state.cardMatches) return;
  state.cardMatchLimit = Math.min(
    state.cardMatchLimit + 12,
    state.cardMatches.suggestions.length,
  );
  renderMatchResults(state.cardMatches);
});

$("#card-import-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const button = e.submitter;
  const file = $("#card-import-file").files[0];
  const ok = await uploadCardPurchaseFile(file, button);
  if (ok) await runCostMatches($("#run-cost-matches"));
});

$("#read-card-clipboard").addEventListener("click", async (e) => {
  const button = e.currentTarget;
  if (!navigator.clipboard?.readText) {
    toast("当前浏览器不允许读取剪贴板，请改用文件导入", "error");
    return;
  }
  setBusy(button, true, "读取剪贴板…");
  try {
    const text = (await navigator.clipboard.readText()).trim();
    if (!text) throw Error("剪贴板是空的，请先在卡网订单页全选复制");
    if ($("#card-paste-text")) $("#card-paste-text").value = text;
    setBusy(button, false);
    const ok = await importCardPurchaseText(text, button, "clipboard-paste.txt");
    if (ok) await runCostMatches($("#run-cost-matches"));
  } catch (err) {
    toast(err.message, "error");
  } finally {
    if (busyButtons.has(button)) setBusy(button, false);
  }
});

$("#card-settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const button = e.submitter;
  const email = $("#card-contact-email").value.trim();
  if (!email) {
    toast("请先填写联系方式（邮箱）", "error");
    return;
  }
  setBusy(button, true, "保存中…");
  try {
    const r = await api(`/api/stores/${state.store}/card-settings`, {
      method: "PUT",
      body: JSON.stringify({ contact_email: email }),
    });
    state.loadedSettingsStore = state.store;
    setText(
      "card-settings-status",
      `已保存联系方式：${r.contact_email}（更新于 ${formatCardSettingTime(r.updated_at)}）`,
    );
    toast("联系方式已保存", "success");
  } catch (err) {
    toast(err.message, "error");
  } finally {
    setBusy(button, false);
  }
});

$("#card-paste-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const ok = await importCardPurchaseText($("#card-paste-text").value, e.submitter, "card-paste.txt");
  if (ok) {
    $("#card-paste-text").value = "";
    await runCostMatches($("#run-cost-matches"));
  }
});

function renderAiQueryOutcome(r) {
  const siteLabels = { "16688": "16688", wzyp: "链动小铺" };
  const summaryParts = Object.entries(r.site_results || {}).map(([key, info]) => {
    const label = siteLabels[key] || key;
    if (info.status === "ok") return `${label} 查到 ${info.count} 条`;
    if (info.status === "captcha_required") return `${label} 等待输入验证码`;
    return `${label} 失败（${info.error || "未知原因"}）`;
  });
  const impText = r.import_result ? formatCardImportSummary(r.import_result, "已导入") : "未新增记录";
  const hasErrors = Array.isArray(r.errors) && r.errors.length > 0;
  $("#card-import-result").innerHTML =
    `<div class="import-message ${hasErrors && !r.queried_count ? "bad" : "ok"}"><strong>AI 查询完成：</strong>${esc(summaryParts.join("；") || `读取 ${r.queried_count || 0} 条`)}。${r.import_result ? esc(impText) : ""}</div>`;
  if (r.captcha_session_id) {
    state.captchaSession = r.captcha_session_id;
    if ($("#card-captcha-img") && r.captcha_image_data_url) {
      $("#card-captcha-img").src = r.captcha_image_data_url;
    }
    if ($("#card-captcha-code")) $("#card-captcha-code").value = "";
    if ($("#card-captcha-panel")) $("#card-captcha-panel").hidden = false;
    toast(r.message || "请输入链动小铺图中验证码", "success");
    $("#card-captcha-code")?.focus();
  } else {
    state.captchaSession = null;
    if ($("#card-captcha-panel")) $("#card-captcha-panel").hidden = true;
    toast(summaryParts.join("；") || "AI 查询完成", "success");
  }
}

$("#card-ai-query-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const button = e.submitter;
  const email = $("#card-contact-email").value.trim();
  if (!email) {
    toast("请先填写并保存联系方式（邮箱）", "error");
    $("#card-contact-email")?.focus();
    return;
  }
  setBusy(button, true, "AI 查询中…");
  try {
    await api(`/api/stores/${state.store}/card-settings`, {
      method: "PUT",
      body: JSON.stringify({ contact_email: email }),
    });
    const r = await api(`/api/stores/${state.store}/card-purchases/ai-query`, {
      method: "POST",
      body: JSON.stringify({
        site: $("#card-ai-site").value,
        contact_email: email,
      }),
    });
    renderAiQueryOutcome(r);
    await loadCosts({ preserveCardResult: true });
    await loadBatches();
    if (r.queried_count > 0) await runCostMatches($("#run-cost-matches"));
  } catch (err) {
    $("#card-import-result").innerHTML =
      `<div class="import-message bad">${esc(err.message)}</div>`;
    toast(err.message, "error");
  } finally {
    setBusy(button, false);
  }
});

$("#card-captcha-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!state.captchaSession) {
    toast("验证码会话已失效，请重新点 AI 查询订单", "error");
    $("#card-captcha-panel").hidden = true;
    return;
  }
  const button = e.submitter;
  setBusy(button, true, "验证中…");
  try {
    const r = await api(`/api/stores/${state.store}/card-purchases/ai-query`, {
      method: "POST",
      body: JSON.stringify({
        site: "wzyp",
        contact_email: $("#card-contact-email").value.trim() || null,
        captcha_session_id: state.captchaSession,
        captcha_code: $("#card-captcha-code").value.trim(),
      }),
    });
    renderAiQueryOutcome(r);
    await loadCosts({ preserveCardResult: true });
    await loadBatches();
    if (r.queried_count > 0) await runCostMatches($("#run-cost-matches"));
  } catch (err) {
    toast(err.message, "error");
  } finally {
    setBusy(button, false);
  }
});

$("#cancel-card-captcha").addEventListener("click", () => {
  state.captchaSession = null;
  $("#card-captcha-panel").hidden = true;
});

function updateLedgerCountsUI() {
  const counts = state.ledgerData?.counts;
  if (!counts) return;
  setText("ledger-tab-purchases-count", String(counts.purchases_filtered ?? counts.purchases_total ?? 0));
  setText("ledger-tab-orders-count", String(counts.orders_filtered ?? counts.orders_total ?? 0));
  const isPurchases = state.ledgerTab === "purchases";
  const catMap = isPurchases
    ? (counts.category_group_counts || counts.category_counts)
    : (counts.orders_category_group_counts || counts.orders_category_counts);
  const allTotal = isPurchases ? (counts.purchases_total ?? 0) : (counts.orders_total ?? 0);
  $$(".cat-count").forEach((node) => {
    const key = node.dataset.catCount;
    node.textContent = String(key === "all" ? allTotal : (catMap?.[key] ?? 0));
  });
  setText(
    "ledger-summary-badge",
    `采购单 待核对 ${counts.purchases_pending ?? 0} / 已核对 ${counts.purchases_matched ?? 0} · 订单 待核对 ${counts.orders_pending ?? 0} / 已核对 ${counts.orders_matched ?? 0}`,
  );
}

function renderCardLedger(data) {
  state.ledgerData = data;
  updateLedgerCountsUI();
  const purchasesBody = $("#ledger-purchases-body");
  const ordersBody = $("#ledger-orders-body");
  if (purchasesBody) {
    const rows = data.purchases || [];
    purchasesBody.innerHTML = rows.length
      ? rows
          .map((p) => {
            const effQty = p.effective_quantity || p.quantity || 1;
            const qtyLine = effQty > 1
              ? `${p.inferred_quantity && p.inferred_quantity > p.quantity ? "推算 " : ""}${effQty} 件（已核 ${p.allocated_quantity}，余 ${p.remaining_quantity}）`
              : "1 件";
            const matchedHtml = p.matched_order_ids?.length
              ? `<div><strong>${esc(p.matched_at || "已记录")}</strong></div><div class="mono-id">${p.matched_order_ids.map((id) => esc(id)).join("<br>")}</div>`
              : `<span class="muted">${p.reconcile_status === "excluded" ? "已退款或关闭" : "待与千牛订单核对"}</span>`;
            return `<tr>
              <th scope="row"><div class="mono-id">${esc(p.purchase_external_id)}</div><small title="${esc(p.source || "")}">${esc(shortCardSource(p.source || ""))}${p.seller ? " · " + esc(p.seller) : ""}</small></th>
              <td class="cell-title"><div>${esc(p.product_title || "—")}</div><span class="cat-tag">${esc(p.category_group_label || p.category_label || "其他")}</span></td>
              <td><div>${esc(qtyLine)}</div><small>单价 ${money(p.unit_cost_fen)}</small></td>
              <td><div><strong>${money(p.net_fen)}</strong></div><small>${esc(p.status || "—")}</small></td>
              <td><span class="mono-id">${esc(p.paid_at || "未知")}</span></td>
              <td><span class="status-pill ${esc(p.reconcile_status)}">${esc(p.reconcile_label)}</span></td>
              <td>${matchedHtml}</td>
            </tr>`;
          })
          .join("")
      : '<tr><td colspan="7" class="table-empty">当前筛选条件下没有卡网采购单</td></tr>';
  }
  if (ordersBody) {
    const rows = data.orders || [];
    ordersBody.innerHTML = rows.length
      ? rows
          .map((o) => {
            const matchedHtml = o.matched_purchase_ids?.length
              ? `<div><strong>${esc(o.matched_at || "已记录")}</strong></div><div class="mono-id">${o.matched_purchase_ids.map((id) => esc(id)).join("<br>")}</div>`
              : `<span class="muted">${o.reconcile_status === "excluded" ? "补差价/无需核对" : "待匹配卡网采购单"}</span>`;
            return `<tr>
              <th scope="row"><div class="mono-id">${esc(o.order_id)}</div><small class="mono-id">子单 ${esc(o.item_id)}</small></th>
              <td class="cell-title"><div>${esc(o.product_title || o.sku)}</div><small>${esc(o.sku)}</small><span class="cat-tag">${esc(o.category_group_label || o.category_label || "其他")}</span></td>
              <td><div>实付 ${money(o.payment_fen)}</div><small>成本 ${money(o.cost_fen)}</small></td>
              <td><span class="mono-id">${esc(o.paid_at || "未知")}</span></td>
              <td><span class="status-pill ${esc(o.reconcile_status)}">${esc(o.reconcile_label)}</span></td>
              <td>${matchedHtml}</td>
            </tr>`;
          })
          .join("")
      : '<tr><td colspan="6" class="table-empty">当前筛选条件下没有千牛订单</td></tr>';
  }
}

async function loadCardLedger(context = scope()) {
  if (!context.store) return;
  const params = new URLSearchParams({
    category: state.ledgerCategory || "all",
    status: $("#ledger-status")?.value || "all",
    source: $("#ledger-source")?.value || "all",
    start: $("#ledger-start")?.value || "",
    end: $("#ledger-end")?.value || "",
    q: $("#ledger-search")?.value.trim() || "",
    limit: "1000",
  });
  try {
    const data = await api(`/api/stores/${context.store}/card-ledger?${params}`);
    if (!current(context)) return;
    renderCardLedger(data);
  } catch (err) {
    if (current(context)) toast(err.message, "error");
  }
}

function activateLedgerTab(tabEl, { focus = false, reload = true } = {}) {
  if (!tabEl) return;
  const tabs = $$('.ledger-tabs [role="tab"]');
  const nextKey = tabEl.id === "ledger-tab-orders" ? "orders" : "purchases";
  state.ledgerTab = nextKey;
  for (const t of tabs) {
    const selected = t === tabEl;
    t.setAttribute("aria-selected", String(selected));
    t.tabIndex = selected ? 0 : -1;
    const panelId = t.getAttribute("aria-controls");
    const panel = panelId ? $("#" + panelId) : null;
    if (panel) panel.hidden = !selected;
  }
  const sourceWrap = $("#ledger-source-wrap");
  if (sourceWrap) sourceWrap.hidden = nextKey !== "purchases";
  updateLedgerCountsUI();
  if (focus) tabEl.focus();
  if (reload && state.store) loadCardLedger();
}

$$('.ledger-tabs [role="tab"]').forEach((tab) => {
  tab.addEventListener("click", () => activateLedgerTab(tab, { focus: false, reload: false }));
  tab.addEventListener("keydown", (e) => {
    const tabs = $$('.ledger-tabs [role="tab"]');
    const idx = tabs.indexOf(tab);
    if (idx < 0) return;
    let next = null;
    if (e.key === "ArrowRight") next = tabs[(idx + 1) % tabs.length];
    else if (e.key === "ArrowLeft") next = tabs[(idx - 1 + tabs.length) % tabs.length];
    else if (e.key === "Home") next = tabs[0];
    else if (e.key === "End") next = tabs[tabs.length - 1];
    if (next) {
      e.preventDefault();
      activateLedgerTab(next, { focus: true, reload: false });
    }
  });
});

$$(".cat-pill").forEach((pill) => {
  pill.addEventListener("click", () => {
    state.ledgerCategory = pill.dataset.ledgerCat || "all";
    $$(".cat-pill").forEach((node) => {
      const active = node === pill;
      node.classList.toggle("active", active);
      node.setAttribute("aria-pressed", String(active));
    });
    loadCardLedger();
  });
});

$("#ledger-filter-form")?.addEventListener("submit", (e) => {
  e.preventDefault();
  loadCardLedger();
});

$("#ledger-reset")?.addEventListener("click", () => {
  $("#ledger-filter-form")?.reset();
  state.ledgerCategory = "all";
  $$(".cat-pill").forEach((node) => {
    const active = node.dataset.ledgerCat === "all";
    node.classList.toggle("active", active);
    node.setAttribute("aria-pressed", String(active));
  });
  loadCardLedger();
});

async function confirmSingleMatchButton(button) {
  if (!button || !state.store) return;
  const itemId = button.dataset.matchItem;
  const allocRaw = button.dataset.matchAllocations;
  let allocations = null;
  if (allocRaw) {
    try { allocations = JSON.parse(allocRaw); } catch {}
  }
  setBusy(button, true, "确认中…");
  try {
    const r = await api(`/api/stores/${state.store}/card-matches`, {
      method: "POST",
      body: JSON.stringify({
        request_id: crypto.randomUUID(),
        item_id: itemId,
        purchase_id: Number(button.dataset.matchPurchase),
        purchase_quantity: button.dataset.matchInferredQty ? Number(button.dataset.matchInferredQty) : null,
        score: Number(button.dataset.matchScore),
        cost: "",
        note: allocations ? "AI组合凑单建议，店主确认成本" : "AI给出候选，店主确认成本",
        allocations,
      }),
    });
    state.confirmedItems.add(itemId);
    const cardEl = button.closest(".match-card");
    const extId = button.dataset.matchExtId || "";
    const costFen = Number(button.dataset.matchCostFen || r.amount_fen || 0);
    if (cardEl) {
      cardEl.classList.remove("is-kb-focused");
      cardEl.classList.add("is-confirmed");
      cardEl.innerHTML = `
        <div class="confirmed-inline-banner">✓ 已核对入账 · 采购单 ${esc(extId)} · 成本 ${money(costFen)}</div>
        <span class="badge">已计入纯利润</span>
      `;
    }
    if (state.cardMatches) renderMatchSummaryDashboard(state.cardMatches);
    advanceKbFocusAfterConfirm();
    toast(r.duplicate ? "这笔成本已经确认过" : `已确认成本 ${money(costFen)}，纯利润已同步更新`, "success");
    Promise.all([loadCardLedger(), loadReport()]).catch(() => {});
  } catch (err) {
    toast(err.message, "error");
    setBusy(button, false);
  }
}

async function batchConfirmHighMatches(button) {
  if (!state.store || !state.cardMatches?.suggestions?.length) return;
  const targets = state.cardMatches.suggestions.filter(
    (s) => s.candidates?.[0]?.auto_confirmable && !state.confirmedItems.has(s.item_id),
  );
  if (!targets.length) {
    toast("当前没有可一键批量确认的高匹配订单", "error");
    return;
  }
  setBusy(button, true, `正在批量入账 ${targets.length} 笔…`);
  try {
    const items = targets.map((s) => {
      const c = s.candidates[0];
      const allocs = c.is_bundle && Array.isArray(c.allocations) && c.allocations.length > 1
        ? c.allocations.map((a) => ({
            purchase_id: a.purchase_id,
            allocated_quantity: a.allocated_quantity,
            purchase_quantity: a.purchase_quantity || null,
          }))
        : null;
      return {
        request_id: crypto.randomUUID(),
        item_id: s.item_id,
        purchase_id: c.purchase_id,
        purchase_quantity: c.inferred_quantity && c.inferred_quantity > (c.purchase_quantity || 1)
          ? c.inferred_quantity
          : null,
        score: c.score,
        cost: "",
        note: allocs ? "AI一键批量组合核对" : "AI一键批量高匹配核对",
        allocations: allocs,
      };
    });
    const res = await api(`/api/stores/${state.store}/card-matches/batch`, {
      method: "POST",
      body: JSON.stringify({ items }),
    });
    if (button) button.innerHTML = "<span>正在刷新利润与剩余建议…</span>";
    await Promise.all([
      loadCosts({ preserveCardResult: true }),
      loadReport(),
      runCostMatches(null, { silent: true }),
    ]);
    toast(
      `已一键确认 ${res.confirmed_count} 笔高匹配成本（合计 ${money(res.total_cost_fen)}）${res.skipped_count ? `，跳过库存冲突 ${res.skipped_count} 笔` : ""}`,
      "success",
    );
  } catch (err) {
    toast(err.message, "error");
  } finally {
    setBusy(button, false);
  }
}

document.addEventListener("click", async (e) => {
  const matchBtn = e.target.closest("[data-match-item]");
  if (matchBtn) {
    await confirmSingleMatchButton(matchBtn);
    return;
  }
  const batchBtn = e.target.closest("#batch-confirm-high-btn");
  if (batchBtn) {
    await batchConfirmHighMatches(batchBtn);
    return;
  }
  const tierPill = e.target.closest("[data-match-tier]");
  if (tierPill && state.cardMatches) {
    state.matchTierFilter = tierPill.dataset.matchTier || "all";
    state.kbMatchIndex = 0;
    renderMatchResults(state.cardMatches);
    return;
  }
  const copyBtn = e.target.closest("[data-copy-text]");
  if (copyBtn) {
    const txt = copyBtn.dataset.copyText || "";
    try {
      await navigator.clipboard.writeText(txt);
      toast("已复制到剪贴板", "success");
    } catch {
      toast("浏览器未授权剪贴板，请手动复制", "error");
    }
  }
});
document.addEventListener("click", (e) => {
  let b = e.target.closest("[data-sku]");
  if (b) {
    $("#cost-sku").value = b.dataset.sku;
    $("#unit-cost").focus();
  }
});
$("#cost-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  let btn = e.submitter;
  setBusy(btn, true);
  try {
    let r = await api(`/api/stores/${state.store}/costs`, {
      method: "POST",
      body: JSON.stringify({
        sku: $("#cost-sku").value,
        unit_cost: $("#unit-cost").value,
        valid_from: $("#cost-from").value,
        supplier: $("#supplier").value,
      }),
    });
    toast(`成本已保存，补齐 ${r.matched_orders} 笔订单`);
    $("#unit-cost").value = "";
    await loadCosts();
    await loadReport();
  } catch (err) {
    toast(err.message);
  } finally {
    setBusy(btn, false);
  }
});
$("#adjustment-form").addEventListener("input", () => {
  state.adjustmentKey = null;
});
$("#adjust-kind").addEventListener("change", () => {
  $("#adjust-note").required = $("#adjust-kind").value === "cost_set";
});
$("#adjust-item").addEventListener("change", loadAdjustmentHistory);
async function loadAdjustmentHistory() {
  let item = $("#adjust-item").value.trim(),
    context = scope();
  if (!item) return;
  try {
    let rows = await api(
      `/api/stores/${context.store}/adjustments/${encodeURIComponent(item)}`,
    );
    if (!current(context) || item !== $("#adjust-item").value.trim()) return;
    let names = {
      cost_set: "逐单采购总成本",
      cost_delta: "成本差额",
      expense: "额外费用",
      supplier_refund: "供应商返款",
      cost_invalidate: "旧成本待重新核对",
    };
    $("#adjustment-history").innerHTML = rows.length
      ? "<h4>该订单的调整记录</h4>" +
        rows
          .map(
            (x) =>
              `<div class="history-row"><span>记录 ${esc(x.id)} · ${esc(names[x.kind] || x.kind)} ${x.kind === "cost_invalidate" ? "" : money(x.amount_fen)} ${esc(x.note || "")}</span><span>${esc(x.created_at.slice(0, 10))}</span></div>`,
          )
          .join("")
      : '<p class="muted">该订单暂无调整记录。</p>';
  } catch (err) {
    if (current(context)) toast(err.message);
  }
}
$("#adjustment-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  let btn = e.submitter;
  setBusy(btn, true);
  state.adjustmentKey = state.adjustmentKey || crypto.randomUUID();
  try {
    await api(`/api/stores/${state.store}/adjustments`, {
      method: "POST",
      body: JSON.stringify({
        request_id: state.adjustmentKey,
        item_id: $("#adjust-item").value.trim(),
        kind: $("#adjust-kind").value,
        amount: $("#adjust-amount").value,
        note: $("#adjust-note").value,
      }),
    });
    toast("逐单调整已保存");
    state.adjustmentKey = null;
    $("#adjust-amount").value = "";
    $("#adjust-note").value = "";
    loadAdjustmentHistory();
    loadReport();
  } catch (err) {
    toast(err.message);
  } finally {
    setBusy(btn, false);
  }
});
async function loadRefundReviewHistory(item) {
  let context = scope(),
    rows = await api(
      `/api/stores/${context.store}/refund-reviews/${encodeURIComponent(item)}`,
    );
  if (!current(context) || state.refundReview?.item !== item) return;
  $("#refund-review-history").innerHTML = rows.length
    ? "<h4>历史核对记录</h4>" +
      rows
        .map(
          (x) =>
            `<div class="history-row"><span>记录 ${esc(x.id)} · 累计 ${money(x.refund_fen)} · ${esc(x.note)}</span><span>${esc(x.created_at.slice(0, 10))} · 操作人 ${esc(x.reviewer_id)}</span></div>`,
        )
        .join("")
    : '<p class="muted">暂无人工核对记录。</p>';
}
document.addEventListener("click", async (e) => {
  let context = scope(),
    b = e.target.closest("[data-refund-review]");
  if (!b) return;
  let x = state.report?.items.find((i) => i.item_id === b.dataset.refundReview);
  if (!x?.refund_review_fingerprint) return;
  state.refundReview = {
    item: x.item_id,
    fingerprint: x.refund_review_fingerprint,
    key: null,
  };
  $("#refund-review-item").value = x.item_id;
  $("#refund-review-amount").value = "";
  $("#refund-review-note").value = "";
  $("#refund-review-source").innerHTML =
    `<p class="muted">订单状态：${esc(x.status)}；退款状态：${esc(x.refund_status)}；退款快照：${money(x.refund_fen)}。${x.refund_review_stale ? "之前的核对已失效。" : ""}</p><p class="muted">已导入退款流水：${x.refund_records.length ? x.refund_records.map((r) => `${esc(r.status)} ${money(r.amount_fen)}（批次 ${esc(r.batch_id)}）`).join("；") : "无。无流水不代表没有退款。"}</p><p class="muted">货款账单售后退款：${(x.cashflow_refund_records || []).length ? x.cashflow_refund_records.map((r) => `${money(r.amount_fen)}（${esc(r.settled_at)}，批次 ${esc(r.batch_id)}）`).join("；") : "未发现已导入记录。未导入不代表没有退款。"}；账单金额为商家实际出账，可能与买家退款不同。</p>`;
  $("#refund-review-panel").hidden = false;
  try {
    await loadRefundReviewHistory(x.item_id);
  } catch (err) {
    if (current(context)) toast(err.message);
  }
  if (!current(context)) return;
  $("#refund-review-panel").scrollIntoView({
    behavior: "smooth",
    block: "center",
  });
});
$("#refund-review-form").addEventListener("input", () => {
  if (state.refundReview) state.refundReview.key = null;
});
$("#refund-review-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  let selected = state.refundReview;
  if (!selected) return;
  let btn = e.submitter;
  setBusy(btn, true);
  selected.key = selected.key || crypto.randomUUID();
  try {
    await api(`/api/stores/${state.store}/refund-reviews`, {
      method: "POST",
      body: JSON.stringify({
        request_id: selected.key,
        item_id: selected.item,
        source_fingerprint: selected.fingerprint,
        refund_amount: $("#refund-review-amount").value,
        note: $("#refund-review-note").value,
      }),
    });
    toast("退款历史核对已保存");
    selected.key = null;
    await loadRefundReviewHistory(selected.item);
    await loadReport();
  } catch (err) {
    toast(err.message);
  } finally {
    setBusy(btn, false);
  }
});
async function loadAskSkus() {
  let context = scope();
  try {
    let rows = await api(`/api/stores/${context.store}/skus`);
    if (!current(context)) return;
    $("#ask-sku").innerHTML =
      '<option value="">全部商品</option>' +
      rows
        .map((x) => `<option value="${esc(x.sku)}">${esc(x.sku)}</option>`)
        .join("");
  } catch (err) {
    if (current(context)) toast(err.message);
  }
}
document.addEventListener("click", (e) => {
  let b = e.target.closest("[data-question]");
  if (b) {
    $("#question").value = b.dataset.question;
    $("#question").focus();
  }
});
$("#ask-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  let btn = e.submitter;
  setBusy(btn, true, "查询中…");
  $("#answer").setAttribute("aria-busy", "true");
  $("#answer").textContent = "正在读取账本并核对结果…";
  try {
    let r = await api(`/api/stores/${state.store}/agent/query`, {
      method: "POST",
      body: JSON.stringify({
        question: $("#question").value,
        start: $("#start-date").value,
        end: $("#end-date").value,
        sku: $("#ask-sku").value || null,
      }),
    });
    if (r.cashflow_review_version !== 2)
      throw Error("服务版本尚未更新，请重启服务后查询。");
    let aliases = Object.entries(r.aliases);
    $("#answer").innerHTML =
      `<div class="answer-box">${esc(r.answer)}</div><div class="answer-meta">范围：${esc(r.start)} 至 ${esc(r.end)}；最近导入：${esc(r.as_of || "无")}；查询依据：${esc(r.trace.map((x) => ({ get_profit_summary: "利润汇总", get_order_details: "订单明细", get_refund_summary: "退款汇总" })[x.tool]).join("、"))}；批次：${esc(r.batch_ids.join("、") || "无")}</div>${aliases.length ? `<details class="answer-aliases"><summary>查看 ${aliases.length} 个商品别名</summary><div class="alias-list">${aliases.map(([a, s]) => `${esc(a)}：${esc(s)}`).join("<br>")}</div></details>` : ""}`;
  } catch (err) {
    $("#answer").innerHTML =
      `<div class="import-message bad">${esc(err.message)}</div>`;
  } finally {
    $("#answer").setAttribute("aria-busy", "false");
    setBusy(btn, false);
  }
});

/* ==========================================================================
   Guided Workflow Sync, Qianniu Chat Insights & Global Keyboard Palette
   ========================================================================== */

function updateGuidedWorkflow() {
  const rep = state.report;
  if (rep?.counts) {
    const paid = rep.counts.paid_items || 0;
    const settled = rep.counts.settled || 0;
    setText("wf-step1-desc", paid > 0 ? `当前区间 ${paid} 笔订单，已核算 ${settled} 笔` : "导入千牛 CSV/XLSX 或读取最近下载");
    const b1 = $("#wf-step1-badge");
    if (b1) {
      b1.textContent = paid > 0 ? `${paid} 笔已导` : "去导入";
      b1.className = `wf-pill ${paid > 0 ? "ok" : ""}`;
    }
  }
  const missingPending = state.ledgerData?.counts?.orders_pending
    ?? state.cardMatches?.smart_summary?.actionable_missing_count
    ?? state.report?.counts?.needs_review
    ?? 0;
  const navCostsBadge = $("#nav-costs-badge");
  if (navCostsBadge) {
    navCostsBadge.hidden = !(missingPending > 0);
    navCostsBadge.textContent = String(missingPending);
  }
  setText(
    "wf-step2-desc",
    missingPending > 0
      ? `还有 ${missingPending} 笔缺成本订单可 AI 配对`
      : "采购成本已全部核清",
  );
  const b2 = $("#wf-step2-badge");
  if (b2) {
    b2.textContent = missingPending > 0 ? `待核 ${missingPending}` : "已核清 ✓";
    b2.className = `wf-pill ${missingPending > 0 ? "warn" : "ok"}`;
  }

  const totalChats = state.chatOverview?.counts?.total_sessions ?? 0;
  const knowCnt = state.chatOverview?.counts?.knowledge_count ?? 0;
  const navChatsBadge = $("#nav-chats-badge");
  if (navChatsBadge) {
    navChatsBadge.hidden = !(totalChats > 0);
    navChatsBadge.textContent = String(totalChats);
  }
  setText(
    "wf-step3-desc",
    totalChats > 0
      ? `已整理 ${totalChats} 段对话 · 沉淀 ${knowCnt} 条标准话术`
      : "自动脱敏整理痛点与标准话术",
  );
  const b3 = $("#wf-step3-badge");
  if (b3) {
    b3.textContent = totalChats > 0 ? `${knowCnt} 条话术` : "看洞察";
    b3.className = `wf-pill ${totalChats > 0 ? "ok" : ""}`;
  }
}

async function loadChatsBadgeQuietly() {
  if (!state.store) return;
  const context = scope();
  try {
    const ov = await api(`/api/stores/${context.store}/chats/overview?limit=1`);
    if (!current(context)) return;
    if (!state.chatOverview) state.chatOverview = ov;
    updateGuidedWorkflow();
  } catch {}
}

async function loadCardLedgerBadgeQuietly() {
  if (!state.store) return;
  const context = scope();
  try {
    const led = await api(`/api/stores/${context.store}/card-ledger?limit=1`);
    if (!current(context)) return;
    if (!state.ledgerData) state.ledgerData = led;
    updateGuidedWorkflow();
  } catch {}
}

function formatRedactedHtml(text) {
  return esc(text || "").replaceAll(
    "[已自动脱敏凭据]",
    '<span class="redacted-shield" title="敏感密码、密钥或邮箱已自动脱敏保护">已自动脱敏凭据</span>',
  );
}

function renderChatsOverview(ov) {
  state.chatOverview = ov;
  updateGuidedWorkflow();
  const c = ov.counts || {};
  setText("chat-total-sessions", String(c.total_sessions ?? 0));
  setText("chat-total-messages-sub", `累计 ${c.total_messages ?? 0} 条对话消息`);
  setText("chat-linked-orders", String(c.linked_order_sessions ?? 0));
  setText("chat-unresolved-count", String(c.unresolved_sessions ?? 0));
  setText("chat-urgent-sub", `紧急投诉 ${c.urgent_sessions ?? 0} 段`);
  setText("chat-knowledge-count", String(c.knowledge_count ?? 0));
  setText("chat-sessions-badge", `筛选出 ${c.filtered_sessions ?? 0} / 共 ${c.total_sessions ?? 0} 段会话`);

  const clearTopicBtn = $("#clear-chat-topic-btn");
  if (clearTopicBtn) clearTopicBtn.hidden = !state.chatTopic;

  const topics = ov.topic_ranking || [];
  const topicBox = $("#chat-topic-bars");
  if (topicBox) {
    topicBox.innerHTML = topics.length
      ? topics
          .map(
            (t) => `<button type="button" class="topic-bar-btn${state.chatTopic === t.topic ? " active" : ""}" data-filter-chat-topic="${esc(t.topic)}">
              <div class="topic-bar-head">
                <span>${esc(t.label)}</span>
                <span>${t.count} 段 (${t.ratio_pct}%)${t.unresolved_count ? ` · 待跟进 ${t.unresolved_count}` : ""}</span>
              </div>
              <div class="topic-bar-track"><div class="topic-bar-fill" style="width:${Math.max(4, Math.min(100, t.ratio_pct))}%"></div></div>
            </button>`,
          )
          .join("")
      : '<div class="empty">粘贴或导入千牛对话后，这里会自动统计买家最常问的痛点排行。</div>';
  }

  const demands = ov.demand_ranking || [];
  const demandBox = $("#chat-demand-list");
  if (demandBox) {
    demandBox.innerHTML = demands.length
      ? demands
          .map(
            (d) => `<div class="demand-card">
              <div class="demand-card-head">
                <strong>✦ ${esc(d.label)}</strong>
                <span class="badge">${d.count} 段提及 (${d.ratio_pct}%)</span>
              </div>
              ${d.sample_summary ? `<small>买家原话摘录：${formatRedactedHtml(d.sample_summary)}</small>` : ""}
            </div>`,
          )
          .join("")
      : '<div class="empty">暂未检测到明确的新品、团购或续费诉求信号。</div>';
  }

  const kItems = ov.knowledge_items || [];
  const kBox = $("#chat-knowledge-list");
  if (kBox) {
    kBox.innerHTML = kItems.length
      ? kItems
          .map(
            (k) => `<article class="knowledge-card${k.is_pinned ? " pinned" : ""}">
              <div class="knowledge-card-top">
                <span class="cat-tag">${esc(k.topic_label || k.topic)} · ${esc(k.product_category_label || "通用")}</span>
                ${k.is_pinned ? '<span class="bundle-pill">置顶常用</span>' : `<small>源自 ${k.source_count || 1} 次接待沉淀</small>`}
              </div>
              <strong>${esc(k.title)}</strong>
              ${k.trigger_question ? `<small>触发提问：${formatRedactedHtml(k.trigger_question)}</small>` : ""}
              <div class="knowledge-reply-preview">${formatRedactedHtml(k.standard_reply)}</div>
              <div class="knowledge-actions">
                <button type="button" class="button primary mini" data-copy-text="${esc(k.standard_reply)}">一键复制话术</button>
                <button type="button" class="button outline mini" data-edit-knowledge="${k.id}">编辑</button>
                <button type="button" class="button outline mini" data-pin-knowledge="${k.id}" data-pinned="${k.is_pinned ? "1" : "0"}">${k.is_pinned ? "取消置顶" : "置顶"}</button>
                <button type="button" class="button outline mini" data-delete-knowledge="${k.id}">删除</button>
              </div>
            </article>`,
          )
          .join("")
      : '<div class="empty">还没有标准客服话术，粘贴接待记录会自动提炼，或点右上角手工新增。</div>';
  }

  const sessions = ov.sessions || [];
  const sBox = $("#chat-sessions-list");
  if (sBox) {
    sBox.innerHTML = sessions.length
      ? sessions
          .map((s) => {
            const lo = s.linked_order;
            const orderPill = lo
              ? `<span class="linked-order-pill">关联订单 ${esc(lo.order_external_id)} · 实付 ${money(lo.payment_fen)} · ${esc(lo.sku || lo.product_title)}</span>`
              : s.order_external_id
                ? `<span class="linked-order-pill">提及单号 ${esc(s.order_external_id)}</span>`
                : "";
            const demandBadges = (s.demand_signals || [])
              .map((d) => `<span class="reason-chip">需求：${esc(d)}</span>`)
              .join("");
            const bubbles = (s.messages || [])
              .map(
                (m) => `<div class="chat-bubble ${m.role === "seller" ? "seller" : "buyer"}">
                  <div class="chat-bubble-meta">${esc(m.sender || (m.role === "seller" ? "客服" : "买家"))} ${esc(m.time || "")}</div>
                  <div>${formatRedactedHtml(m.text)}</div>
                </div>`,
              )
              .join("");
            return `<article class="chat-session-card">
              <div class="chat-session-head">
                <div class="chat-session-meta">
                  <strong>${esc(s.buyer_nick || "匿名买家")}</strong>
                  <span class="cat-tag">${esc(s.product_category_label || "其他")}</span>
                  <span class="cat-tag">${esc(s.stage_label || s.stage)}</span>
                  <span class="status-pill ${s.resolved ? "matched" : "pending"}">${s.resolved ? "已解决" : `待跟进 · ${esc(s.sentiment_label || "")}`}</span>
                  ${orderPill}
                </div>
                <button type="button" class="button ${s.resolved ? "outline" : "secondary"} mini" data-toggle-chat-resolved="${s.id}" data-next-resolved="${s.resolved ? "0" : "1"}">${s.resolved ? "标为待跟进" : "✓ 标记已解决"}</button>
              </div>
              <div>${formatRedactedHtml(s.summary || "客服沟通记录")}</div>
              ${demandBadges ? `<div class="chip-row">${demandBadges}</div>` : ""}
              <details>
                <summary class="muted">展开查看 ${s.message_count || s.messages?.length || 0} 条脱敏对话（${esc(s.last_message_at || s.started_at || "时间未记录")}）</summary>
                <div class="chat-bubbles">${bubbles}</div>
              </details>
            </article>`;
          })
          .join("")
      : '<div class="empty">当前筛选条件下暂无千牛聊天记录。</div>';
  }
}

async function loadChatsOverview(context = scope()) {
  if (!context.store) return;
  const params = new URLSearchParams({
    category: state.chatCategory === "all" ? "" : state.chatCategory,
    stage: $("#chat-stage-filter")?.value || "",
    topic: state.chatTopic || "",
    resolved: $("#chat-resolved-filter")?.value || "",
    q: $("#chat-search")?.value.trim() || "",
    limit: "150",
  });
  try {
    const ov = await api(`/api/stores/${context.store}/chats/overview?${params}`);
    if (!current(context)) return;
    renderChatsOverview(ov);
  } catch (err) {
    if (current(context)) toast(err.message, "error");
  }
}

$("#chat-paste-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#chat-import-submit");
  const text = $("#chat-paste-text")?.value.trim() || "";
  if (!text) return;
  setBusy(btn, true, "智能脱敏与归类中…");
  try {
    const r = await api(`/api/stores/${state.store}/chats/import-text`, {
      method: "POST",
      body: JSON.stringify({ text, filename: "qianniu-paste.txt", source: "paste", use_ai: false }),
    });
    const msg = `成功整理 ${r.imported_count} 段会话（新增 ${r.inserted} 段，合并更新 ${r.updated_count} 段，重复跳过 ${r.duplicate_count} 段），自动更新 ${r.knowledge_updated} 条标准话术。`;
    $("#chat-import-result").innerHTML = `<div class="import-message ok">${esc(msg)}</div>`;
    $("#chat-paste-text").value = "";
    toast(msg, "success");
    await loadChatsOverview();
  } catch (err) {
    $("#chat-import-result").innerHTML = `<div class="import-message bad">${esc(err.message)}</div>`;
    toast(err.message, "error");
  } finally {
    setBusy(btn, false);
  }
});

$("#read-chat-clipboard")?.addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  try {
    const clip = await navigator.clipboard.readText();
    if (!clip || !clip.trim()) {
      toast("剪贴板为空，请先在千牛窗口复制聊天内容", "error");
      return;
    }
    $("#chat-paste-text").value = clip;
    $("#chat-paste-form").requestSubmit(btn);
  } catch {
    toast("请点击下方文本框按 Ctrl+V 粘贴", "error");
    $("#chat-paste-text")?.focus();
  }
});

$("#fill-chat-demo")?.addEventListener("click", () => {
  const demo = `买家_小陈 2026-11-17 20:15:10
你好，我刚拍了 Gemini Pro 18个月成品号，登录密码 demoPass888999 提示要输入两步验证码怎么弄？另外质保多久，到期后还能找你们续费吗？
小猫AI工作室:客服阿猫 2026-11-17 20:16:05
亲，成品号都是独立独享的，首登质保6小时哦！打开2FA在线接码页粘贴卡密最后一段密钥就能拿到6位动态验证码登录，后续到期随时找我们续费或换新号～
---
买家_工作室老周 2026-11-17 21:02:30
老板，店里有 Claude Pro 独享号吗？我们团队想一次采购 10 个 Gemini 18个月激活链接，有批量团购价吗？
小猫AI工作室:客服阿猫 2026-11-17 21:03:18
亲，拍5件以上就有工作室阶梯团购优惠！Claude 新品需求已经帮您登记反馈选品了，先给您改好10个激活链接的团购价哈！`;
  if ($("#chat-paste-text")) {
    $("#chat-paste-text").value = demo;
    $("#chat-paste-text").focus();
    toast("已填入示例格式预览，点「智能整理并入库」即可体验效果", "success");
  }
});

$("#sync-qianniu-browser")?.addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  setBusy(btn, true, "只读检测中…");
  try {
    const r = await api(`/api/stores/${state.store}/chats/sync-browser`, { method: "POST", body: "{}" });
    const note = r.message || `检测完成：读取到 ${r.imported_count || 0} 段会话`;
    $("#chat-import-result").innerHTML = `<div class="import-message ${r.imported_count ? "ok" : ""}">${esc(note)}</div>`;
    toast(note, "success");
    if (r.imported_count) await loadChatsOverview();
  } catch (err) {
    toast(err.message, "error");
  } finally {
    setBusy(btn, false);
  }
});

$("#chat-upload-btn")?.addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  const file = $("#chat-import-file")?.files?.[0];
  if (!file) {
    toast("请先选择要上传的聊天或质检文件", "error");
    return;
  }
  setBusy(btn, true, "上传解析中…");
  try {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("use_ai", "false");
    const r = await api(`/api/stores/${state.store}/chats/import-file`, { method: "POST", body: fd });
    const msg = `文件「${file.name}」已整理 ${r.imported_count} 段会话（新增 ${r.inserted} 段，重复跳过 ${r.duplicate_count} 段）`;
    $("#chat-import-result").innerHTML = `<div class="import-message ok">${esc(msg)}</div>`;
    $("#chat-import-file").value = "";
    toast(msg, "success");
    await loadChatsOverview();
  } catch (err) {
    $("#chat-import-result").innerHTML = `<div class="import-message bad">${esc(err.message)}</div>`;
    toast(err.message, "error");
  } finally {
    setBusy(btn, false);
  }
});

$$(".chat-cat-pill").forEach((pill) => {
  pill.addEventListener("click", () => {
    state.chatCategory = pill.dataset.chatCat || "all";
    $$(".chat-cat-pill").forEach((p) => {
      const active = p === pill;
      p.classList.toggle("active", active);
      p.setAttribute("aria-pressed", String(active));
    });
    loadChatsOverview();
  });
});

$("#chat-filter-form")?.addEventListener("submit", (e) => {
  e.preventDefault();
  loadChatsOverview();
});

$("#chat-filter-reset")?.addEventListener("click", () => {
  $("#chat-filter-form")?.reset();
  state.chatCategory = "all";
  state.chatTopic = "";
  $$(".chat-cat-pill").forEach((p) => {
    const active = p.dataset.chatCat === "all";
    p.classList.toggle("active", active);
    p.setAttribute("aria-pressed", String(active));
  });
  loadChatsOverview();
});

$("#clear-chat-topic-btn")?.addEventListener("click", () => {
  state.chatTopic = "";
  loadChatsOverview();
});

$("#chat-ask-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = e.submitter;
  const question = $("#chat-question")?.value.trim() || "";
  if (!question) return;
  const useAi = Boolean($("#chat-query-use-ai")?.checked);
  setBusy(btn, true, useAi ? "AI 深度归纳中…" : "统计分析中…");
  $("#chat-answer").innerHTML = '<div class="import-message">正在汇总脱敏会话并提炼结论…</div>';
  try {
    const r = await api(`/api/stores/${state.store}/chats/query`, {
      method: "POST",
      body: JSON.stringify({ question, start: "", end: "", use_ai: useAi }),
    });
    const recs = (r.recommended_replies || [])
      .map(
        (k) => `<div class="demand-card">
          <div class="demand-card-head"><strong>推荐话术：${esc(k.title)}</strong><button type="button" class="button secondary mini" data-copy-text="${esc(k.standard_reply)}">复制话术</button></div>
          <small>${formatRedactedHtml(k.standard_reply)}</small>
        </div>`,
      )
      .join("");
    $("#chat-answer").innerHTML = `
      <div class="answer-box">${formatRedactedHtml(r.answer)}</div>
      <div class="answer-meta">分析模式：${r.output_mode === "model_enhanced" ? "本地大模型深度归纳（数字经账本强校验）" : "确定性实时统计"} · 基于 ${r.counts?.total_sessions ?? 0} 段会话</div>
      ${recs ? `<div class="demand-list">${recs}</div>` : ""}
    `;
  } catch (err) {
    $("#chat-answer").innerHTML = `<div class="import-message bad">${esc(err.message)}</div>`;
  } finally {
    setBusy(btn, false);
  }
});

function openKnowledgeDialog(item = null) {
  const dlg = $("#knowledge-dialog");
  if (!dlg) return;
  $("#kn-id").value = item ? String(item.id) : "";
  $("#kn-topic").value = item?.topic || "login_2fa";
  $("#kn-category").value = item?.product_category || "all";
  $("#kn-title").value = item?.title || "";
  $("#kn-trigger").value = item?.trigger_question || "";
  $("#kn-reply").value = item?.standard_reply || "";
  $("#kn-pinned").checked = Boolean(item?.is_pinned);
  dlg.showModal();
}

$("#open-new-knowledge")?.addEventListener("click", () => openKnowledgeDialog(null));

$("#knowledge-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = e.submitter;
  setBusy(btn, true, "保存中…");
  try {
    const rawId = $("#kn-id").value.trim();
    await api(`/api/stores/${state.store}/chats/knowledge`, {
      method: "POST",
      body: JSON.stringify({
        id: rawId ? Number(rawId) : null,
        topic: $("#kn-topic").value,
        product_category: $("#kn-category").value,
        title: $("#kn-title").value.trim(),
        trigger_question: $("#kn-trigger").value.trim(),
        standard_reply: $("#kn-reply").value.trim(),
        is_pinned: $("#kn-pinned").checked,
      }),
    });
    $("#knowledge-dialog")?.close();
    toast("标准客服话术已保存", "success");
    await loadChatsOverview();
  } catch (err) {
    toast(err.message, "error");
  } finally {
    setBusy(btn, false);
  }
});

document.addEventListener("click", async (e) => {
  const chatQ = e.target.closest("[data-chat-question]");
  if (chatQ && $("#chat-question")) {
    $("#chat-question").value = chatQ.dataset.chatQuestion;
    $("#chat-ask-form")?.requestSubmit();
    return;
  }
  const topicBtn = e.target.closest("[data-filter-chat-topic]");
  if (topicBtn) {
    const nextTopic = topicBtn.dataset.filterChatTopic || "";
    state.chatTopic = state.chatTopic === nextTopic ? "" : nextTopic;
    loadChatsOverview();
    return;
  }
  const resolveBtn = e.target.closest("[data-toggle-chat-resolved]");
  if (resolveBtn) {
    const id = Number(resolveBtn.dataset.toggleChatResolved);
    const nextResolved = resolveBtn.dataset.nextResolved === "1";
    setBusy(resolveBtn, true, "更新中…");
    try {
      await api(`/api/stores/${state.store}/chats/sessions/${id}/toggle-resolved`, {
        method: "POST",
        body: JSON.stringify({ resolved: nextResolved }),
      });
      toast(nextResolved ? "已标记为解决" : "已标为待跟进", "success");
      await loadChatsOverview();
    } catch (err) {
      toast(err.message, "error");
      setBusy(resolveBtn, false);
    }
    return;
  }
  const editKn = e.target.closest("[data-edit-knowledge]");
  if (editKn) {
    const id = Number(editKn.dataset.editKnowledge);
    const item = (state.chatOverview?.knowledge_items || []).find((x) => x.id === id);
    if (item) openKnowledgeDialog(item);
    return;
  }
  const pinKn = e.target.closest("[data-pin-knowledge]");
  if (pinKn) {
    const id = Number(pinKn.dataset.pinKnowledge);
    const item = (state.chatOverview?.knowledge_items || []).find((x) => x.id === id);
    if (!item) return;
    try {
      await api(`/api/stores/${state.store}/chats/knowledge`, {
        method: "POST",
        body: JSON.stringify({
          id: item.id,
          topic: item.topic,
          product_category: item.product_category,
          title: item.title,
          trigger_question: item.trigger_question,
          standard_reply: item.standard_reply,
          is_pinned: !item.is_pinned,
        }),
      });
      toast(item.is_pinned ? "已取消置顶" : "已置顶话术", "success");
      await loadChatsOverview();
    } catch (err) {
      toast(err.message, "error");
    }
    return;
  }
  const delKn = e.target.closest("[data-delete-knowledge]");
  if (delKn) {
    const id = Number(delKn.dataset.deleteKnowledge);
    try {
      await api(`/api/stores/${state.store}/chats/knowledge/${id}`, { method: "DELETE" });
      toast("话术卡片已删除", "success");
      await loadChatsOverview();
    } catch (err) {
      toast(err.message, "error");
    }
    return;
  }
  const closeDlg = e.target.closest("[data-close-dialog]");
  if (closeDlg) {
    $("#" + closeDlg.dataset.closeDialog)?.close();
  }
});

/* Theme Toggle, Command Palette & Keyboard Review Engine */
$("#theme-toggle")?.addEventListener("click", () => {
  const next = state.theme === "dark" ? "light" : "dark";
  applyTheme(next);
  toast(next === "dark" ? "已切换为深色护眼模式" : "已切换为浅色明亮模式", "success");
});

$("#open-shortcut-help")?.addEventListener("click", () => {
  $("#shortcut-help-dialog")?.showModal();
});

$$("dialog.modal-dialog").forEach((dlg) => {
  dlg.addEventListener("click", (e) => {
    if (e.target === dlg) dlg.close();
  });
});

function buildPaletteCommands() {
  return [
    { label: "前往：工作台", sub: "查看纯利润、营业额与待处理订单", kbd: "Alt+1", run: () => goto("overview") },
    { label: "前往：导入千牛订单", sub: "导入 CSV/XLSX 或读取最近下载", kbd: "Alt+2", run: () => goto("import") },
    { label: "前往：AI 补齐采购成本", sub: "卡网查询粘贴、智能比例看板与批量核对", kbd: "Alt+3", run: () => goto("costs") },
    { label: "前往：已入库订单", sub: "按类目搜索订单并核对成本", kbd: "Alt+4", run: () => goto("orders") },
    { label: "前往：千牛聊天分析", sub: "对话脱敏归档、高频痛点与标准话术库", kbd: "Alt+5", run: () => goto("chats") },
    { label: "前往：问账", sub: "按时间范围提问利润与退款数据", kbd: "Alt+6", run: () => goto("assistant") },
    {
      label: "动作：一键确认全部高匹配成本",
      sub: "在 AI 补成本页批量确认高置信度建议",
      kbd: "批量入账",
      run: async () => {
        goto("costs");
        if (!state.cardMatches) await runCostMatches(null, { silent: true });
        await batchConfirmHighMatches($("#batch-confirm-high-btn"));
      },
    },
    {
      label: "动作：重新运行 AI 智能核对",
      sub: "全店库存全局最优配对",
      kbd: "AI 核对",
      run: () => {
        goto("costs");
        runCostMatches($("#run-cost-matches"));
      },
    },
    {
      label: `外观：切换为${state.theme === "dark" ? "浅色" : "深色"}主题`,
      sub: "护眼深色 / 清爽浅色随时切换",
      kbd: "Alt+D",
      run: () => $("#theme-toggle")?.click(),
    },
    {
      label: "帮助：查看键盘快捷键说明",
      sub: "J/K 快速核对、/ 搜索等效率热键",
      kbd: "?",
      run: () => $("#shortcut-help-dialog")?.showModal(),
    },
  ];
}

let filteredCmds = [];
function renderCmdPalette() {
  const q = ($("#cmd-palette-input")?.value || "").trim().toLowerCase();
  const all = buildPaletteCommands();
  filteredCmds = q
    ? all.filter((c) => (c.label + " " + c.sub).toLowerCase().includes(q))
    : all;
  if (state.cmdIndex >= filteredCmds.length) state.cmdIndex = 0;
  const listEl = $("#cmd-palette-list");
  if (!listEl) return;
  listEl.innerHTML = filteredCmds.length
    ? filteredCmds
        .map(
          (c, i) => `<button type="button" class="cmd-item${i === state.cmdIndex ? " active" : ""}" data-cmd-idx="${i}" role="option" aria-selected="${i === state.cmdIndex}">
            <div><strong>${esc(c.label)}</strong><small>${esc(c.sub)}</small></div>
            <kbd>${esc(c.kbd)}</kbd>
          </button>`,
        )
        .join("")
    : '<div class="empty">没有匹配的命令</div>';
}

function openCmdPalette() {
  const dlg = $("#cmd-palette-dialog");
  if (!dlg || $("#app-shell")?.hidden) return;
  state.cmdIndex = 0;
  if ($("#cmd-palette-input")) $("#cmd-palette-input").value = "";
  renderCmdPalette();
  if (!dlg.open) dlg.showModal();
  $("#cmd-palette-input")?.focus();
}

$("#open-cmd-palette")?.addEventListener("click", openCmdPalette);
$("#cmd-palette-input")?.addEventListener("input", () => {
  state.cmdIndex = 0;
  renderCmdPalette();
});
$("#cmd-palette-input")?.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    e.preventDefault();
    $("#cmd-palette-dialog")?.close();
    return;
  }
  if (!filteredCmds.length) return;
  if (e.key === "ArrowDown") {
    e.preventDefault();
    state.cmdIndex = (state.cmdIndex + 1) % filteredCmds.length;
    renderCmdPalette();
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    state.cmdIndex = (state.cmdIndex - 1 + filteredCmds.length) % filteredCmds.length;
    renderCmdPalette();
  } else if (e.key === "Enter") {
    e.preventDefault();
    const chosen = filteredCmds[state.cmdIndex];
    $("#cmd-palette-dialog")?.close();
    chosen?.run();
  }
});
$("#cmd-palette-list")?.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-cmd-idx]");
  if (!btn) return;
  const chosen = filteredCmds[Number(btn.dataset.cmdIdx)];
  $("#cmd-palette-dialog")?.close();
  chosen?.run();
});

function advanceKbFocusAfterConfirm() {
  const cards = $$("#match-results .match-card:not(.is-confirmed)");
  if (!cards.length) return;
  const target = cards[0];
  $$("#match-results .match-card").forEach((c) => c.classList.remove("is-kb-focused"));
  target.classList.add("is-kb-focused");
  state.kbMatchIndex = Number(target.dataset.cardIndex || 0);
  target.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function moveKbMatchFocus(delta) {
  const cards = $$("#match-results .match-card:not(.is-confirmed)");
  if (!cards.length) return;
  let curPos = cards.findIndex((c) => c.classList.contains("is-kb-focused"));
  if (curPos < 0) curPos = 0;
  else curPos = Math.max(0, Math.min(cards.length - 1, curPos + delta));
  $$("#match-results .match-card").forEach((c) => c.classList.remove("is-kb-focused"));
  const nextCard = cards[curPos];
  nextCard.classList.add("is-kb-focused");
  state.kbMatchIndex = Number(nextCard.dataset.cardIndex || 0);
  nextCard.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

document.addEventListener("keydown", (e) => {
  if ($("#app-shell")?.hidden) return;
  if ((e.ctrlKey || e.metaKey || e.altKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    const dlg = $("#cmd-palette-dialog");
    if (dlg?.open) dlg.close();
    else openCmdPalette();
    return;
  }
  if (e.altKey && e.key.toLowerCase() === "d") {
    e.preventDefault();
    $("#theme-toggle")?.click();
    return;
  }
  const viewMap = { "1": "overview", "2": "import", "3": "costs", "4": "orders", "5": "chats", "6": "assistant" };
  if (e.altKey && viewMap[e.key]) {
    e.preventDefault();
    goto(viewMap[e.key]);
    return;
  }
  if (document.querySelector("dialog[open]")) return;
  const tag = (e.target?.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select" || e.target?.isContentEditable) return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;

  if (e.key === "?") {
    e.preventDefault();
    $("#shortcut-help-dialog")?.showModal();
    return;
  }
  if (e.key === "/") {
    const activeView = location.hash.slice(1) || "overview";
    const searchTarget =
      activeView === "costs" ? $("#ledger-search")
      : activeView === "orders" ? $("#order-search")
      : activeView === "chats" ? $("#chat-search")
      : activeView === "assistant" ? $("#question")
      : null;
    if (searchTarget) {
      e.preventDefault();
      searchTarget.focus();
    }
    return;
  }

  const activeView = location.hash.slice(1) || "overview";
  if (activeView === "costs" && state.cardMatches?.suggestions?.length) {
    if (e.key === "j" || e.key === "J") {
      e.preventDefault();
      moveKbMatchFocus(1);
    } else if (e.key === "k" || e.key === "K") {
      e.preventDefault();
      moveKbMatchFocus(-1);
    } else if (e.key === "1" || e.key === "2" || e.key === "3") {
      const focused = $("#match-results .match-card.is-kb-focused:not(.is-confirmed)");
      if (!focused) return;
      const itemId = focused.dataset.cardItem;
      const sale = state.cardMatches.suggestions.find((s) => s.item_id === itemId);
      const targetIdx = Number(e.key) - 1;
      if (sale && sale.candidates?.[targetIdx]) {
        e.preventDefault();
        state.selectedCandByItem[itemId] = targetIdx;
        renderMatchResults(state.cardMatches);
        toast(`已切换至第 ${targetIdx + 1} 个候选采购单`, "success");
      }
    } else if (e.key === "Enter") {
      const focused = $("#match-results .match-card.is-kb-focused:not(.is-confirmed)");
      const confirmBtn = focused?.querySelector(".primary-confirm-btn");
      if (confirmBtn) {
        e.preventDefault();
        confirmSingleMatchButton(confirmBtn);
      }
    }
  }
});

(async () => {
  try {
    await loadAuthConfig();
  } catch {
    applyAuthConfig({ quick_login_enabled: true });
  }
  try {
    if (!state.csrf) throw Error();
    showApp(await api("/api/me"));
  } catch {
    if (state.quickLoginEnabled) {
      try {
        await doQuickLogin();
      } catch {
        showLogin();
      }
    } else {
      showLogin();
    }
  }
})();
