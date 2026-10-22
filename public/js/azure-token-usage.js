const azureTokenStatusEl = document.getElementById("azureTokenStatus");
const azureTokenFormEl = document.getElementById("azureTokenQueryForm");
const azureTokenLoginBtn = document.getElementById("azureTokenLoginBtn");
const azureTokenLogoutBtn = document.getElementById("azureTokenLogoutBtn");
const azureTokenRefreshSubsBtn = document.getElementById("azureTokenRefreshSubsBtn");
const azureTokenSelectionGridEl = document.getElementById("azureTokenSelectionGrid");
const azureTokenSubscriptionEl = document.getElementById("azureTokenSubscription");
const azureTokenStartDateEl = document.getElementById("azureTokenStartDate");
const azureTokenEndDateEl = document.getElementById("azureTokenEndDate");
const azureTokenTimezoneEl = document.getElementById("azureTokenTimezone");
const azureTokenQueryBtn = document.getElementById("azureTokenQueryBtn");
const azureTokenResultEl = document.getElementById("azureTokenResult");
const azureTokenTableWrapEl = document.getElementById("azureTokenTableWrap");
const azureTokenTableEl = document.getElementById("azureTokenTable");
const azureTokenState = { signedIn: false, configured: false, loadingSubscriptions: false, loginUrlApi: "", logoutUrl: "", account: null, subscriptions: [], data: null, view: "detail", sortKey: "", sortAsc: false, dropdownOpen: false, search: "" };
const AZURE_TOKEN_CACHE_KEY = "azure-query-azure-token-cache-v3-model-fallback";
const AZURE_TOKEN_METRIC_LABELS = {
  ProcessedPromptTokens: "提示令牌数",
  GeneratedTokens: "完成令牌数",
  TokenTransaction: "令牌总数",
};
const AZURE_TOKEN_FILTERS = { subscriptionId: { search: "", selected: new Set() }, region: { search: "", selected: new Set() }, account: { search: "", selected: new Set() }, model: { search: "", selected: new Set() }, openColumn: "" };

initAzureTokenUsage();

function initAzureTokenUsage() {
  if (!azureTokenFormEl) return;
  const today = new Date();
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  populateAzureTokenDateOptions(today, yesterday);
  azureTokenLoginBtn.addEventListener("click", () => startAzureTokenLogin().catch((error) => setAzureTokenStatus(error.message, true)));
  azureTokenLogoutBtn.addEventListener("click", () => { window.location.href = azureTokenState.logoutUrl || "/auth/openai/logout"; });
  azureTokenRefreshSubsBtn.addEventListener("click", () => loadAzureTokenSubscriptions(true).catch((error) => setAzureTokenStatus(error.message || "刷新订阅列表失败", true)));
  azureTokenFormEl.addEventListener("submit", (event) => { event.preventDefault(); runAzureTokenQuery(); });
  azureTokenStartDateEl.addEventListener("change", () => { syncAzureTokenDateBounds(); loadAzureTokenCachedResult(); });
  azureTokenEndDateEl.addEventListener("change", () => { syncAzureTokenDateBounds(); loadAzureTokenCachedResult(); });
  azureTokenTimezoneEl.addEventListener("change", () => loadAzureTokenCachedResult());
  azureTokenSelectionGridEl.addEventListener("click", handleAzureTokenSelectionClick);
  azureTokenSelectionGridEl.addEventListener("change", handleAzureTokenSelectionChange);
  azureTokenSelectionGridEl.addEventListener("input", handleAzureTokenSelectionInput);
  document.addEventListener("click", (event) => {
    if (azureTokenState.dropdownOpen && !azureTokenSelectionGridEl.contains(event.target)) {
      azureTokenState.dropdownOpen = false;
      renderAzureTokenSelection();
    }
  });
  document.querySelectorAll("[data-token-view]").forEach((button) => button.addEventListener("click", () => {
    azureTokenState.view = button.dataset.tokenView === "summary" ? "summary" : "detail";
    document.querySelectorAll("[data-token-view]").forEach((item) => item.classList.toggle("active", item === button));
    renderAzureTokenTable();
  }));
  const azureTokenTableEventTarget = azureTokenTableWrapEl || azureTokenTableEl;
  azureTokenTableEventTarget.addEventListener("click", handleAzureTokenTableClick);
  azureTokenTableEventTarget.addEventListener("input", handleAzureTokenTableInput);
  azureTokenTableEventTarget.addEventListener("change", handleAzureTokenTableChange);
  fetchAzureTokenAuthStatus();
}

async function fetchAzureTokenAuthStatus() {
  try {
    const response = await fetch("/api/openai-tier/auth/status");
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.message || "读取登录状态失败");
    azureTokenState.configured = Boolean(data.configured);
    azureTokenState.signedIn = Boolean(data.signedIn);
    azureTokenState.loginUrlApi = data.loginUrlApi || "/api/openai-tier/auth/login-url";
    azureTokenState.logoutUrl = data.logoutUrl || "/auth/openai/logout";
    azureTokenState.account = data.account || null;
    renderAzureTokenSelection();
    updateAzureTokenAuthControls();
    if (azureTokenState.signedIn) {
      await loadAzureTokenSubscriptions();
      loadAzureTokenCachedResult();
      setAzureTokenStatus(`当前已登录：${formatAzureTokenAccount(data.account)}`);
    } else {
      setAzureTokenStatus("请先通过 Entra ID 账号登录，然后再查询 Azure 模型令牌。", true);
    }
  } catch (error) {
    setAzureTokenStatus(error.message || "初始化令牌统计失败", true);
  }
}

function updateAzureTokenAuthControls() {
  azureTokenLoginBtn.disabled = azureTokenState.signedIn || !azureTokenState.configured;
  azureTokenLogoutBtn.disabled = !azureTokenState.signedIn;
  azureTokenRefreshSubsBtn.disabled = azureTokenState.loadingSubscriptions || !azureTokenState.signedIn;
  azureTokenQueryBtn.disabled = azureTokenState.loadingSubscriptions || !azureTokenState.signedIn || azureTokenSubscriptionEl.options.length === 0;
}

async function startAzureTokenLogin() {
  if (!azureTokenState.configured) throw new Error("服务端尚未配置 Microsoft 登录参数");
  const baseUrl = azureTokenState.loginUrlApi || "/api/openai-tier/auth/login-url";
  const separator = baseUrl.includes("?") ? "&" : "?";
  const response = await fetch(`${baseUrl}${separator}source=azure-token-usage`);
  const data = await response.json().catch(() => ({}));
  if (!response.ok || !data.authUrl) throw new Error(data.message || "生成 Microsoft 登录地址失败");
  window.location.href = data.authUrl;
}

async function loadAzureTokenSubscriptions(showLoading = false) {
  if (azureTokenState.loadingSubscriptions || !azureTokenState.signedIn) return;
  azureTokenState.loadingSubscriptions = true;
  updateAzureTokenAuthControls();
  if (showLoading) setAzureTokenStatus("正在加载订阅列表...", false, true);
  try {
    const response = await fetch("/api/azure-token/subscriptions");
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.message || "加载订阅列表失败");
    azureTokenState.subscriptions = Array.isArray(data.subscriptions) ? data.subscriptions : [];
    azureTokenSubscriptionEl.innerHTML = azureTokenState.subscriptions.map((item) => `<option value="${escapeAttr(item.subscriptionId)}">${escapeHtml(item.subscriptionName)} (${escapeHtml(item.subscriptionId)})</option>`).join("");
    Array.from(azureTokenSubscriptionEl.options).forEach((option) => { option.selected = true; });
    azureTokenState.dropdownOpen = false;
    azureTokenState.search = "";
    renderAzureTokenSelection();
    loadAzureTokenCachedResult();
    if (showLoading) setAzureTokenStatus(`当前已登录：${formatAzureTokenAccount(azureTokenState.account)}`);
  } finally {
    azureTokenState.loadingSubscriptions = false;
    updateAzureTokenAuthControls();
  }
}

function renderAzureTokenSelection() {
  if (!azureTokenSelectionGridEl) return;
  const selectedIds = new Set(Array.from(azureTokenSubscriptionEl.selectedOptions).map((option) => option.value));
  const visible = azureTokenState.subscriptions.filter((item) => `${item.subscriptionName} ${item.subscriptionId}`.toLowerCase().includes(azureTokenState.search.trim().toLowerCase()));
  const dropdown = azureTokenState.dropdownOpen ? `<div class="tier-summary-menu"><input class="tier-picker-search" data-action="subscription-search" value="${escapeAttr(azureTokenState.search)}" placeholder="搜索订阅..." /><div class="tier-picker-toolbar"><div class="tier-picker-meta">已选择 ${selectedIds.size} / ${azureTokenState.subscriptions.length} 个订阅</div><div class="tier-picker-actions"><button type="button" class="tier-picker-action" data-picker-action="select-all">全选</button><button type="button" class="tier-picker-action" data-picker-action="clear-all">清空</button></div></div><div class="tier-subscription-grid">${visible.map((item) => `<label class="tier-subscription-option"><input type="checkbox" data-subscription-id="${escapeAttr(item.subscriptionId)}" ${selectedIds.has(item.subscriptionId) ? "checked" : ""} /><span class="tier-subscription-option-text">${escapeHtml(item.subscriptionName)} / ${escapeHtml(item.subscriptionId)}</span></label>`).join("") || '<div class="tier-picker-empty">没有匹配的订阅</div>'}</div></div>` : "";
  azureTokenSelectionGridEl.innerHTML = `<div class="summary-card summary-card--range summary-card--login-account"><div class="k">登录账号：</div><div class="v">${escapeHtml(formatAzureTokenAccount(azureTokenState.account))}</div></div><div class="summary-card summary-card--range summary-card--subscription-select" data-token-action="toggle"><div class="k">已选订阅：</div><div class="v tier-summary-inline"><div class="tier-selected-count">${azureTokenState.signedIn ? `${selectedIds.size} / ${azureTokenState.subscriptions.length} 个订阅` : ""}</div><div class="tier-summary-dropdown"><button type="button" class="tier-summary-trigger" aria-expanded="${azureTokenState.dropdownOpen}" ${azureTokenState.subscriptions.length ? "" : "disabled"}></button>${dropdown}</div></div></div>`;
  const subscriptionCard = azureTokenSelectionGridEl.querySelector(".summary-card--subscription-select");
  subscriptionCard?.addEventListener("click", (event) => {
    if (event.target.closest(".tier-summary-menu") || event.target.closest("input")) return;
    event.stopPropagation();
    toggleAzureTokenSubscriptionDropdown();
  });
  subscriptionCard?.querySelector(".tier-summary-trigger")?.addEventListener("click", (event) => {
    event.stopPropagation();
    toggleAzureTokenSubscriptionDropdown();
  });
}

function toggleAzureTokenSubscriptionDropdown() {
  if (azureTokenState.subscriptions.length === 0) return;
  azureTokenState.dropdownOpen = !azureTokenState.dropdownOpen;
  renderAzureTokenSelection();
  if (azureTokenState.dropdownOpen) {
    azureTokenSelectionGridEl.querySelector("[data-action='subscription-search']")?.focus();
  }
}

function handleAzureTokenSelectionClick(event) {
  const action = event.target.closest("[data-picker-action]")?.dataset.pickerAction;
  if (action) {
    const selectedIds = action === "select-all"
      ? new Set(getFilteredAzureTokenSubscriptions().map((item) => item.subscriptionId))
      : new Set();
    if (action === "clear-all") {
      Array.from(azureTokenSubscriptionEl.options).forEach((option) => { option.selected = false; });
    } else {
      Array.from(azureTokenSubscriptionEl.options).forEach((option) => {
        if (selectedIds.has(option.value)) option.selected = true;
      });
    }
    azureTokenState.dropdownOpen = true;
    renderAzureTokenSelection();
    updateAzureTokenAuthControls();
    return;
  }
  if (event.target.closest(".tier-summary-menu")) return;
  const toggle = event.target.closest('[data-token-action="toggle"]');
  if (toggle && azureTokenState.subscriptions.length > 0) { toggleAzureTokenSubscriptionDropdown(); return; }
}

function handleAzureTokenSelectionChange(event) {
  const checkbox = event.target.closest("input[data-subscription-id]");
  if (!checkbox) return;
  const option = Array.from(azureTokenSubscriptionEl.options).find((item) => item.value === checkbox.dataset.subscriptionId);
  if (option) option.selected = checkbox.checked;
  azureTokenState.dropdownOpen = true;
  renderAzureTokenSelection();
  updateAzureTokenAuthControls();
  loadAzureTokenCachedResult();
}

function getFilteredAzureTokenSubscriptions() {
  const keyword = azureTokenState.search.trim().toLowerCase();
  if (!keyword) return azureTokenState.subscriptions;
  return azureTokenState.subscriptions.filter((item) => `${item.subscriptionName} ${item.subscriptionId}`.toLowerCase().includes(keyword));
}

function handleAzureTokenSelectionInput(event) {
  if (!event.target.matches("[data-action='subscription-search']")) return;
  const caretPosition = event.target.selectionStart ?? event.target.value.length;
  azureTokenState.search = event.target.value;
  renderAzureTokenSelection();
  const input = azureTokenSelectionGridEl.querySelector("[data-action='subscription-search']");
  if (input) {
    input.focus();
    input.setSelectionRange(caretPosition, caretPosition);
  }
}

async function runAzureTokenQuery() {
  const startDate = azureTokenStartDateEl.value;
  const endDate = azureTokenEndDateEl.value;
  const subscriptionIds = Array.from(azureTokenSubscriptionEl.selectedOptions).map((option) => option.value).filter(Boolean);
  if (subscriptionIds.length === 0 || !startDate || !endDate) return setAzureTokenStatus("请选择至少一个订阅和完整日期范围。", true);
  if (endDate < startDate) return setAzureTokenStatus("结束日期不能早于开始日期。", true);
  azureTokenQueryBtn.disabled = true;
  azureTokenQueryBtn.textContent = "查询中...";
  setAzureTokenStatus("正在查询 Azure 模型令牌用量...", false, true);
  try {
    const response = await fetch("/api/azure-token/query", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ subscriptionIds, startDate, endDate, tz: azureTokenTimezoneEl.value }) });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.message || data.error || "令牌统计查询失败");
    data.cachedAt = new Date().toISOString();
    const cacheKey = getAzureTokenCacheKey(subscriptionIds, startDate, endDate, azureTokenTimezoneEl.value);
    localStorage.setItem(cacheKey, JSON.stringify(data));
    azureTokenState.data = data;
    renderAzureTokenResult();
    setAzureTokenStatus(`查询完成，共 ${formatAzureTokenCount(data.grandTotal)} 个令牌。缓存时间：${formatAzureTokenDateTime(data.cachedAt)}`);
  } catch (error) {
    setAzureTokenStatus(error.message || "令牌统计查询失败", true);
  } finally {
    azureTokenQueryBtn.disabled = false;
    azureTokenQueryBtn.textContent = "查询";
  }
}

function getAzureTokenCacheKey(subscriptionIds, startDate, endDate, timezone) {
  return `${AZURE_TOKEN_CACHE_KEY}-${subscriptionIds.slice().sort().join(",")}-${startDate}-${endDate}-${timezone}`;
}

function loadAzureTokenCachedResult() {
  if (!azureTokenState.signedIn || azureTokenSubscriptionEl.selectedOptions.length === 0) return false;
  const subscriptionIds = Array.from(azureTokenSubscriptionEl.selectedOptions).map((option) => option.value).filter(Boolean);
  const cacheKey = getAzureTokenCacheKey(subscriptionIds, azureTokenStartDateEl.value, azureTokenEndDateEl.value, azureTokenTimezoneEl.value);
  const cached = localStorage.getItem(cacheKey);
  if (!cached) return false;
  try {
    azureTokenState.data = JSON.parse(cached);
    renderAzureTokenResult();
    setAzureTokenStatus(`已加载本地缓存结果，缓存时间：${formatAzureTokenDateTime(azureTokenState.data.cachedAt)}`);
    return true;
  } catch (error) {
    localStorage.removeItem(cacheKey);
    return false;
  }
}

function renderAzureTokenResult() {
  const data = azureTokenState.data;
  if (!data) return;
  azureTokenResultEl.hidden = false;
  renderAzureTokenTable();
}

function renderAzureTokenTable() {
  const data = azureTokenState.data;
  if (!data || !azureTokenTableEl) return;
  const filterPanelState = azureTokenTableWrapEl ? window.FilterPopupPortal?.capture(azureTokenTableWrapEl) : null;
  const metrics = Array.isArray(data.metrics) ? data.metrics : [];
  const detail = azureTokenState.view === "detail";
  const rows = detail ? buildAzureTokenDetailRows(data.rows || [], metrics) : (data.summary || []).slice();
  azureTokenTableEl.className = detail ? "azure-token-detail-table" : "azure-token-summary-table";
  const columns = detail ? [["subscriptionName", "订阅", "subscriptionId"], ["region", "区域", "region"], ["account", "账号", "account"], ["model", "模型", "model"], ...metrics.map((metric) => [metric, AZURE_TOKEN_METRIC_LABELS[metric] || metric, "", true])] : [["subscriptionName", "订阅", "subscriptionId"], ["model", "模型", "model"], ...metrics.map((metric) => [metric, AZURE_TOKEN_METRIC_LABELS[metric] || metric, "", true])];
  const filterColumns = detail ? ["subscriptionId", "region", "account", "model"] : ["subscriptionId", "model"];
  const filtered = rows.filter((row) => filterColumns.every((key) => {
    const state = AZURE_TOKEN_FILTERS[key];
    return !state || state.selected.size === 0 || state.selected.has(String(row[key] || "-"));
  }));
  if (azureTokenState.sortKey) filtered.sort((a, b) => { const x = a[azureTokenState.sortKey] || 0; const y = b[azureTokenState.sortKey] || 0; const result = typeof x === "number" ? x - y : String(x).localeCompare(String(y), "zh"); return azureTokenState.sortAsc ? result : -result; });
  azureTokenTableEl.tHead.innerHTML = `<tr>${columns.map(([key, label, filterKey, numeric]) => `<th class="${numeric ? "amount-column" : ""}" data-token-sort="${key}">${filterKey ? renderAzureTokenFilterHeader(filterKey, label, rows) : escapeHtml(label)}${numeric ? ' <button type="button" class="token-sort-button">↕</button>' : ""}</th>`).join("")}</tr>`;
  azureTokenTableEl.tBodies[0].innerHTML = filtered.map((row) => `<tr>${columns.map(([key, , , numeric]) => `<td class="${numeric ? "amount-cell" : ""}">${numeric ? formatAzureTokenCount(row[key]) : escapeHtml(row[key] || "-")}</td>`).join("")}</tr>`).join("");
  const totals = metrics.map((metric) => `<td class="amount-cell">${formatAzureTokenCount(filtered.reduce((sum, row) => sum + Number(row[metric] || 0), 0))}</td>`).join("");
  azureTokenTableEl.tFoot.innerHTML = `<tr><td colspan="${columns.length - metrics.length}">合计</td>${totals}</tr>`;
  if (window.FilterPopupPortal && azureTokenTableWrapEl) {
    window.FilterPopupPortal.clear(azureTokenTableWrapEl);
    if (azureTokenTableWrapEl.querySelector(".filter-popup[data-filter-popup]")) {
      window.FilterPopupPortal.mount(azureTokenTableWrapEl, filterPanelState || {});
    }
  }
}

function renderAzureTokenFilterHeader(filterKey, label, rows) {
  const state = AZURE_TOKEN_FILTERS[filterKey];
  const options = getAzureTokenFilterOptions(filterKey, rows);
  const isOpen = AZURE_TOKEN_FILTERS.openColumn === filterKey;
  const keyword = state.search.trim().toLowerCase();
  const visible = options.filter((option) => option.label.toLowerCase().includes(keyword));
  const popup = isOpen ? `<div class="filter-popup azure-token-filter-popup" data-filter-popup="${filterKey}"><input class="filter-search" data-token-filter-search="${filterKey}" value="${escapeAttr(state.search)}" placeholder="搜索..." /><div class="filter-actions"><button type="button" class="filter-link" data-token-filter-action="select-all" data-token-filter-key="${filterKey}">全选</button><button type="button" class="filter-link filter-link-danger" data-token-filter-action="clear-all" data-token-filter-key="${filterKey}">清除</button></div><div class="filter-options">${visible.map((option) => `<label class="filter-option-item"><input type="checkbox" data-token-filter-option="${filterKey}" data-value="${encodeURIComponent(option.value)}" ${state.selected.has(option.value) ? "checked" : ""} />${escapeHtml(option.label)}</label>`).join("") || '<div class="filter-empty">无匹配项</div>'}</div></div>` : "";
  const active = state.selected.size > 0 && state.selected.size < options.length;
  return `<div class="filter-head-row"><span>${escapeHtml(label)}</span><button type="button" class="filter-toggle ${active ? "filtered" : ""}" data-token-filter-toggle="${filterKey}" data-column="${filterKey}">▼</button></div>${popup}`;
}

function getAzureTokenFilterOptions(filterKey, rows) {
  const optionsByValue = new Map();
  rows.forEach((row) => {
    const value = String(row[filterKey] || "-").trim() || "-";
    const label = filterKey === "subscriptionId"
      ? String(row.subscriptionName || value).trim() || value
      : value;
    optionsByValue.set(value, { value, label });
  });
  return [...optionsByValue.values()].sort((left, right) => left.label.localeCompare(right.label, "zh"));
}

function buildAzureTokenDetailRows(rows, metrics) {
  const grouped = new Map();
  rows.forEach((row) => {
    const key = `${row.subscriptionId}|${row.region}|${row.account}|${row.model || row.deployment}`;
    const item = grouped.get(key) || { subscriptionId: row.subscriptionId, subscriptionName: row.subscriptionName, region: row.region, account: row.account, deployment: row.deployment, model: row.model || row.deployment };
    metrics.forEach((metric) => { item[metric] = Number(item[metric] || 0) + Number(row[metric] || 0); });
    grouped.set(key, item);
  });
  return [...grouped.values()];
}

function handleAzureTokenTableClick(event) {
  const filterToggle = event.target.closest("[data-token-filter-toggle]");
  if (filterToggle) {
    event.stopPropagation();
    const key = filterToggle.dataset.tokenFilterToggle;
    AZURE_TOKEN_FILTERS.openColumn = AZURE_TOKEN_FILTERS.openColumn === key ? "" : key;
    renderAzureTokenTable();
    return;
  }
  const filterAction = event.target.closest("[data-token-filter-action]");
  if (filterAction) {
    const key = filterAction.dataset.tokenFilterKey;
    const state = AZURE_TOKEN_FILTERS[key];
    const metrics = azureTokenState.data?.metrics || [];
    const rows = azureTokenState.view === "detail"
      ? buildAzureTokenDetailRows(azureTokenState.data?.rows || [], metrics)
      : (azureTokenState.data?.summary || []);
    const options = getAzureTokenFilterOptions(key, rows);
    state.selected = filterAction.dataset.tokenFilterAction === "select-all" ? new Set(options.map((option) => option.value)) : new Set();
    state.search = filterAction.dataset.tokenFilterAction === "clear-all" ? "" : state.search;
    AZURE_TOKEN_FILTERS.openColumn = "";
    renderAzureTokenTable();
    return;
  }
  if (event.target.closest(".filter-popup")) return;
  const header = event.target.closest("th[data-token-sort]");
  if (!header) return;
  const key = header.dataset.tokenSort;
  azureTokenState.sortAsc = azureTokenState.sortKey === key ? !azureTokenState.sortAsc : false;
  azureTokenState.sortKey = key;
  renderAzureTokenTable();
}

function handleAzureTokenTableInput(event) {
  const input = event.target.closest("[data-token-filter-search]");
  if (!input) return;
  const key = input.dataset.tokenFilterSearch;
  AZURE_TOKEN_FILTERS[key].search = input.value;
  const caret = input.selectionStart ?? input.value.length;
  renderAzureTokenTable();
  const ownerId = azureTokenTableWrapEl?.dataset.filterPopupOwner;
  const next = ownerId
    ? document.querySelector(`.filter-popup[data-filter-popup-portal-owner="${ownerId}"] [data-token-filter-search="${key}"]`)
    : azureTokenTableEl.querySelector(`[data-token-filter-search="${key}"]`);
  next?.focus();
  next?.setSelectionRange(caret, caret);
}

function handleAzureTokenTableChange(event) {
  const checkbox = event.target.closest("[data-token-filter-option]");
  if (!checkbox) return;
  const state = AZURE_TOKEN_FILTERS[checkbox.dataset.tokenFilterOption];
  const value = decodeURIComponent(checkbox.dataset.value || "");
  checkbox.checked ? state.selected.add(value) : state.selected.delete(value);
  AZURE_TOKEN_FILTERS.openColumn = "";
  renderAzureTokenTable();
}

function syncAzureTokenDateBounds() {
  if (azureTokenEndDateEl.value < azureTokenStartDateEl.value) {
    azureTokenEndDateEl.value = azureTokenStartDateEl.value;
  }
}

function populateAzureTokenDateOptions(today, defaultDate) {
  [azureTokenStartDateEl, azureTokenEndDateEl].forEach((select) => {
    select.replaceChildren();
    for (let offset = 0; offset <= 365; offset += 1) {
      const date = new Date(today);
      date.setDate(today.getDate() - offset);
      const value = formatAzureTokenDate(date);
      select.append(new Option(value, value));
    }
    select.value = formatAzureTokenDate(defaultDate);
  });
}

function setAzureTokenStatus(text, isError = false, isLoading = false) {
  azureTokenStatusEl.classList.toggle("error", isError);
  azureTokenStatusEl.classList.toggle("loading", isLoading);
  azureTokenStatusEl.textContent = text;
}

function formatAzureTokenAccount(account) { return account?.preferredUsername || account?.name || "-"; }
function formatAzureTokenDate(date) { return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`; }
function formatAzureTokenDateTime(value) { const date = new Date(value); return Number.isNaN(date.getTime()) ? "未知" : date.toLocaleString("zh-CN", { hour12: false }); }
function formatAzureTokenCount(value) {
  const amount = Number(value || 0);
  const absolute = Math.abs(amount);
  if (absolute >= 100000000) return `${formatAzureTokenUnit(amount / 100000000)}亿`;
  if (absolute >= 10000) return `${formatAzureTokenUnit(amount / 10000)}万`;
  return Math.round(amount).toLocaleString("zh-CN");
}
function formatAzureTokenUnit(value) {
  return value.toFixed(2).replace(/\.00$/, "").replace(/(\.\d)0$/, "$1");
}
