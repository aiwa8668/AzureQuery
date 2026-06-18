const startDateEl = document.getElementById("startDate");
const endDateEl = document.getElementById("endDate");
const customRangeRowEl = document.getElementById("customRangeRow");
const presetBtns = Array.from(document.querySelectorAll(".query-preset-btn"));
const queryBtn = document.getElementById("queryBtn");
const downloadSubscriptionBtn = document.getElementById("downloadSubscriptionBtn");
const downloadDailyBtn = document.getElementById("downloadDailyBtn");
const downloadResourceTypeBtn = document.getElementById("downloadResourceTypeBtn");
const downloadDateBtn = document.getElementById("downloadDateBtn");
const cacheInfoEl = document.getElementById("cacheInfo");
const dimensionTabsEl = document.getElementById("dimensionTabs");
const themeToggleBtn = document.getElementById("themeToggleBtn");
const themeToggleLightIcon = document.getElementById("themeToggleLightIcon");
const themeToggleDarkIcon = document.getElementById("themeToggleDarkIcon");
const openSettingsBtn = document.getElementById("openSettingsBtn");
const closeSettingsBtn = document.getElementById("closeSettingsBtn");
const saveSettingsBtn = document.getElementById("saveSettingsBtn");
const deleteAuthParamsBtn = document.getElementById("deleteAuthParamsBtn");
const exportAuthParamsBtn = document.getElementById("exportAuthParamsBtn");
const importAuthParamsBtn = document.getElementById("importAuthParamsBtn");
const importAuthParamsInput = document.getElementById("importAuthParamsInput");
const addAuthRowBtn = document.getElementById("addAuthRowBtn");
const authTableBody = document.getElementById("authTableBody");
const settingsModalEl = document.getElementById("settingsModal");
const settingsModalMessageEl = document.getElementById("settingsModalMessage");

let authTableRows = [];

const summaryGridEl = document.getElementById("summaryGrid");
const subscriptionTableWrapEl = document.getElementById("subscriptionTableWrap");
const dailyTableWrapEl = document.getElementById("dailyTableWrap");
const resourceTypeTableWrapEl = document.getElementById("resourceTypeTableWrap");
const dateTableWrapEl = document.getElementById("dateTableWrap");
const dimensionPanels = Array.from(document.querySelectorAll("[data-dimension-panel]"));

let currentResult = null;
const QUERY_CACHE_KEY = "azure-cost-analyzer-query-cache-v1";
const DEFAULT_COST_FILTER_VALUES = [">0"];
let activeDimension = "subscription";
let currentRangeText = "-";
let subscriptionFilters = {
  openColumn: "",
  tenantName: {
    search: "",
    selectedValues: [],
    initialized: false
  },
  subscriptionName: {
    search: "",
    selectedValues: [],
    initialized: false
  },
  subscriptionId: {
    search: "",
    selectedValues: [],
    initialized: false
  },
  cost: {
    search: "",
    selectedValues: [],
    initialized: false
  }
};
let latestSubscriptionRows = [];
let subscriptionFilterEventsBound = false;
const dimensionTableFilters = {
  daily: { openColumn: "", columns: {} },
  resourceType: { openColumn: "", columns: {} },
  date: { openColumn: "", columns: {} }
};
const dimensionFilterEventsBound = {
  daily: false,
  resourceType: false,
  date: false
};

init();

function init() {
  initThemeToggle();
  initPresetButtons();

  renderSummary(null);
  renderSubscriptionTable(subscriptionTableWrapEl, []);
  renderTable(dailyTableWrapEl, getDailyColumns(), [], { dimensionKey: "daily", enableFilters: true });
  renderTable(resourceTypeTableWrapEl, getResourceColumns(), [], { dimensionKey: "resourceType", enableFilters: true });
  renderTable(dateTableWrapEl, getDateColumns(), [], { dimensionKey: "date", enableFilters: true });
  initDimensionTabs();
  switchDimensionPanel(activeDimension);
  initSettingsModal();

  loadCachedResult();
}

function initThemeToggle() {
  if (!themeToggleBtn) {
    return;
  }

  applyTheme(getStoredTheme());
  themeToggleBtn.addEventListener("click", () => {
    const nextTheme = document.documentElement.classList.contains("dark") ? "light" : "dark";
    applyTheme(nextTheme);
  });
}

function getStoredTheme() {
  return localStorage.getItem("color-theme") === "dark" ? "dark" : "light";
}

function applyTheme(theme) {
  const isDark = theme === "dark";
  document.documentElement.classList.toggle("dark", isDark);
  localStorage.setItem("color-theme", isDark ? "dark" : "light");

  if (themeToggleLightIcon) {
    themeToggleLightIcon.hidden = !isDark;
  }
  if (themeToggleDarkIcon) {
    themeToggleDarkIcon.hidden = isDark;
  }
}

async function runQuery() {
  const originalText = queryBtn.textContent;
  try {
    queryBtn.disabled = true;
    queryBtn.textContent = "查询中...";

    const startDate = startDateEl.value;
    const endDate = endDateEl.value;
    validateDates(startDate, endDate);

    const authConfigs = loadAuthConfigs();
    if (!hasRequiredCredentials(authConfigs)) {
      await openSettingsModalWithMessage("未检测到访问凭据，请填写并保存访问凭据", true);
      throw new Error("未检测到访问凭据，请填写并保存访问凭据");
    }

    const validAuthConfigs = authConfigs.filter(
      (cfg) => cfg
        && String(cfg.azureClientId || "").trim()
        && String(cfg.azureClientSecret || "").trim()
        && String(cfg.azureTenantId || "").trim()
        && String(cfg.tenantName || "").trim()
    );
    const hasMaskedSecret = validAuthConfigs.some((cfg) => isMaskedSecret(cfg.azureClientSecret || ""));
    if (hasMaskedSecret) {
      await openSettingsModalWithMessage("检测到部分 App_Client_Secret 为掩码值，请重新输入真实的 Secret Value 后保存", true);
      throw new Error("检测到部分 App_Client_Secret 为掩码值，请在设置中重新输入真实的 Secret Value 后保存");
    }

    setCacheInfo("正在查询数据，请耐心等待！", false, true);

    const response = await fetch("/api/costs/query", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        startDate,
        endDate,
        authList: validAuthConfigs.map((cfg) => ({
          azureClientId: cfg.azureClientId,
          azureClientSecret: cfg.azureClientSecret,
          azureTenantId: cfg.azureTenantId,
          tenantName: cfg.tenantName,
          subscriptionId: cfg.subscriptionId || ""
        }))
      })
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.message || "查询失败");
    }

    currentResult = await response.json();
    currentRangeText = `${startDate} ~ ${endDate}`;

    renderSummary(currentResult);
    renderSubscriptionTable(subscriptionTableWrapEl, currentResult.bySubscription || []);
    renderTable(dailyTableWrapEl, getDailyColumns(), getDailyRows(currentResult), { dimensionKey: "daily", enableFilters: true });
    renderTable(resourceTypeTableWrapEl, getResourceColumns(), currentResult.byResourceType || [], { dimensionKey: "resourceType", enableFilters: true });
    renderTable(dateTableWrapEl, getDateColumns(), currentResult.byDate || [], { dimensionKey: "date", enableFilters: true });

    const warnings = Array.isArray(currentResult.warnings) ? currentResult.warnings : [];
    const authWarnings = Array.isArray(currentResult.authWarnings) ? currentResult.authWarnings : [];
    const allWarnings = [...authWarnings, ...warnings];
    if (allWarnings.length > 0) {
      const hasThrottleWarning = allWarnings.some((message) => String(message || "").includes("限流"));
      if (hasThrottleWarning) {
        setCacheInfo(`查询完成，但部分时间段触发限流，结果可能不完整：${allWarnings.join("；")}`, true);
      } else {
        setCacheInfo(`查询完成（部分认证/订阅异常）：${allWarnings.join("；")}`, true);
      }
    }

    saveQueryCache({
      startDate,
      endDate,
      result: currentResult,
      cachedAt: new Date().toISOString()
    });
    setCacheInfo(`当前查询结果已缓存，缓存时间: ${formatLocalDateTime(new Date().toISOString())}`);
  } catch (error) {
    setCacheInfo(error.message || "查询失败", true);
  } finally {
    queryBtn.disabled = false;
    queryBtn.textContent = originalText;
  }
}

queryBtn.addEventListener("click", () => {
  runQuery();
});

downloadSubscriptionBtn.addEventListener("click", () => {
  exportCsv("subscription");
});

downloadDailyBtn.addEventListener("click", () => {
  exportCsv("daily");
});

downloadResourceTypeBtn.addEventListener("click", () => {
  exportCsv("resourceType");
});

downloadDateBtn.addEventListener("click", () => {
  exportCsv("date");
});

async function exportCsv(dimension) {
  try {
    if (!currentResult) {
      throw new Error("请先查询，再下载 CSV");
    }

    if (dimension === "daily") {
      exportDailyCsv();
      return;
    }

    const response = await fetch("/api/costs/export", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        dimension,
        data: currentResult
      })
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.message || "导出失败");
    }

    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = buildExportFileName(dimension);
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  } catch (error) {
    setCacheInfo(error.message || "导出失败", true);
  }
}

function exportDailyCsv() {
  const rows = getDimensionRowsByKey("daily");
  if (!Array.isArray(rows) || rows.length === 0) {
    throw new Error("按天维度暂无可导出数据");
  }

  const columns = [
    { key: "date", label: "Date" },
    { key: "subscriptionName", label: "Subscription Name" },
    { key: "tenantName", label: "Tenant Name" },
    { key: "subscriptionId", label: "Subscription ID" },
    { key: "cost", label: "Cost" }
  ];

  const header = columns.map((column) => csvEscape(column.label)).join(",");
  const body = rows
    .map((row) => columns.map((column) => csvEscape(formatCellValue(row[column.key], column.key))).join(","))
    .join("\n");
  const csvContent = `${header}\n${body}`;

  const blob = new Blob([`\uFEFF${csvContent}`], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = buildExportFileName("daily");
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function csvEscape(value) {
  const text = value === undefined || value === null ? "" : String(value);
  return `"${text.replace(/"/g, '""')}"`;
}

function buildExportFileName(dimension) {
  const nameMap = {
    subscription: "azure-cost-subscription",
    daily: "azure-cost-date",
    resourceType: "azure-cost-resource",
    date: "azure-cost-details"
  };
  const baseName = nameMap[dimension] || `azure-cost-${dimension}`;
  return `${baseName}-${formatFileTimestamp()}.csv`;
}

function formatFileTimestamp(date = new Date()) {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  const hh = String(date.getHours()).padStart(2, "0");
  const mm = String(date.getMinutes()).padStart(2, "0");
  return `${y}${m}${d}${hh}${mm}`;
}

function renderSummary(result) {
  if (!result) {
    summaryGridEl.innerHTML = [
      summaryCard("时间范围：", currentRangeText || "-", "summary-card--range"),
      summaryCard("总费用：", "-", "summary-card--range"),
      summaryCard("订阅数：", "0", "summary-card--range"),
    ].join("");
    return;
  }

  const currency = result.summary && result.summary.currency ? result.summary.currency : "USD";
  const totalCost = result.summary && Number(result.summary.totalCost || 0);

  summaryGridEl.innerHTML = [
    summaryCard("时间范围：", currentRangeText || "-", "summary-card--range"),
    summaryCard("总费用：", `${currency} ${toMoney(totalCost)}`, "summary-card--range"),
    summaryCard("订阅数：", String((result.bySubscription || []).length), "summary-card--range"),
  ].join("");
}

function summaryCard(k, v, extraClass = "") {
  const className = extraClass ? `summary-card ${extraClass}` : "summary-card";
  return `<div class="${className}"><div class="k">${escapeHtml(k)}</div><div class="v">${escapeHtml(v)}</div></div>`;
}

function renderTable(container, columns, rows, options = {}) {
  const { enableFilters = false, dimensionKey = "" } = options;

  if (!rows || rows.length === 0) {
    container.innerHTML = '<div class="empty">暂无数据</div>';
    return;
  }

  const normalizedColumns = columns.map((column) => {
    if (typeof column === "string") {
      return { key: column, label: column };
    }
    return column;
  });

  let activeRows = rows;
  let header = "";

  if (enableFilters && dimensionKey) {
    const columnOptions = {};
    normalizedColumns.forEach((column) => {
      columnOptions[column.key] = syncDimensionFilterColumnOptions(dimensionKey, column.key, rows);
    });

    activeRows = applyDimensionFilters(dimensionKey, rows, normalizedColumns);
    header = ["<th>No.</th>"]
      .concat(normalizedColumns.map((column) => renderDimensionFilterHeader(dimensionKey, column.key, column.label, columnOptions[column.key])))
      .join("");
  } else {
    header = ["<th>No.</th>"]
      .concat(normalizedColumns.map((column) => `<th>${escapeHtml(column.label)}</th>`))
      .join("");
  }

  const body = activeRows
    .map((row, index) => {
      const cells = normalizedColumns
        .map((column) => `<td>${escapeHtml(formatCellValue(row[column.key], column.key))}</td>`)
        .join("");
      return `<tr class="selectable-row" data-row-index="${index}" tabindex="0"><td>${index + 1}</td>${cells}</tr>`;
    })
    .join("");

  const totalCost = activeRows.reduce((sum, row) => sum + Number(row.cost || 0), 0);
  const totalRow = `<tr class="total-row"><td></td><td colspan="${Math.max(1, normalizedColumns.length - 1)}">合计</td><td>${escapeHtml(toMoney(totalCost))}</td></tr>`;

  const emptyRow = activeRows.length === 0
    ? `<tr><td colspan="${normalizedColumns.length + 1}" class="empty-cell">暂无数据</td></tr>`
    : "";

  container.innerHTML = `<table class="zebra-table"><thead><tr>${header}</tr></thead><tbody>${body}${emptyRow}${totalRow}</tbody></table>`;

  bindRowSelection(container);

  if (enableFilters && dimensionKey) {
    bindDimensionFilterEvents(container, dimensionKey);
  }
}

function renderSubscriptionTable(container, rows) {
  latestSubscriptionRows = Array.isArray(rows) ? rows : [];

  if (!latestSubscriptionRows || latestSubscriptionRows.length === 0) {
    container.innerHTML = '<div class="empty">暂无数据</div>';
    return;
  }

  const tenantOptions = syncSubscriptionFilterColumnOptions("tenantName", latestSubscriptionRows);
  const nameOptions = syncSubscriptionFilterColumnOptions("subscriptionName", latestSubscriptionRows);
  const idOptions = syncSubscriptionFilterColumnOptions("subscriptionId", latestSubscriptionRows);
  const costOptions = syncSubscriptionFilterColumnOptions("cost", latestSubscriptionRows);
  const filteredRows = applySubscriptionFilters(latestSubscriptionRows);

  if (!filteredRows || filteredRows.length === 0) {
    const headerOnly = [
      "<th>No.</th>",
      renderSubscriptionFilterHeader("subscriptionName", "Subscription Name", nameOptions),
      renderSubscriptionFilterHeader("subscriptionId", "Subscription ID", idOptions),
      renderSubscriptionFilterHeader("tenantName", "Tenant Name", tenantOptions),
      renderSubscriptionFilterHeader("cost", "Cost", costOptions)
    ].join("");

    container.innerHTML = `<table class="zebra-table subscription-table"><thead><tr>${headerOnly}</tr></thead><tbody><tr class="total-row"><td></td><td colspan="3">合计</td><td>0.00</td></tr></tbody></table>`;
    bindSubscriptionFilterEvents(container);
    return;
  }

  const columns = [
    { key: "subscriptionName", label: "Subscription Name" },
    { key: "subscriptionId", label: "Subscription ID" },
    { key: "tenantName", label: "Tenant Name" },
    { key: "cost", label: "Cost" }
  ];

  const header = [
    "<th>No.</th>",
    renderSubscriptionFilterHeader("subscriptionName", "Subscription Name", nameOptions),
    renderSubscriptionFilterHeader("subscriptionId", "Subscription ID", idOptions),
    renderSubscriptionFilterHeader("tenantName", "Tenant Name", tenantOptions),
    renderSubscriptionFilterHeader("cost", `${escapeHtml(columns[3].label)}`, costOptions)
  ].join("");
  const body = filteredRows
    .map((row, index) => {
      const cells = columns
        .map((column) => `<td>${escapeHtml(formatSubscriptionCell(row, column.key))}</td>`)
        .join("");
      return `<tr class="selectable-row" data-row-index="${index}" tabindex="0"><td>${index + 1}</td>${cells}</tr>`;
    })
    .join("");

  const totalCost = filteredRows.reduce((sum, row) => sum + Number(row.cost || 0), 0);
  const totalRow = `<tr class="total-row"><td></td><td colspan="3">合计</td><td>${escapeHtml(toMoney(totalCost))}</td></tr>`;

  container.innerHTML = `<table class="zebra-table subscription-table"><thead><tr>${header}</tr></thead><tbody>${body}${totalRow}</tbody></table>`;
  bindSubscriptionFilterEvents(container);
  bindRowSelection(container);
}

function syncSubscriptionFilterColumnOptions(columnKey, rows) {
  const uniqueValues = getSubscriptionFilterOptions(columnKey, rows);
  const columnState = subscriptionFilters[columnKey];

  if (!columnState.initialized) {
    columnState.selectedValues = columnKey === "cost"
      ? DEFAULT_COST_FILTER_VALUES.filter((value) => uniqueValues.includes(value))
      : [];
    columnState.initialized = true;
    return uniqueValues;
  }

  columnState.selectedValues = columnState.selectedValues.filter((value) => uniqueValues.includes(value));
  return uniqueValues;
}

function getSubscriptionFilterOptions(columnKey, rows) {
  if (columnKey === "cost") {
    return ["=0", ">0"];
  }

  return Array.from(new Set(rows.map((row) => String(row[columnKey] || "-").trim() || "-"))).sort((a, b) => a.localeCompare(b));
}

function getDimensionFilterOptions(columnKey, rows) {
  if (columnKey === "cost") {
    return ["=0", ">0"];
  }

  return Array.from(new Set(rows.map((row) => String(row[columnKey] || "-").trim() || "-"))).sort((a, b) => a.localeCompare(b));
}

function getDimensionFilterState(dimensionKey) {
  return dimensionTableFilters[dimensionKey] || { openColumn: "", columns: {} };
}

function ensureDimensionColumnState(dimensionKey, columnKey) {
  const state = getDimensionFilterState(dimensionKey);
  if (!state.columns[columnKey]) {
    state.columns[columnKey] = {
      search: "",
      selectedValues: [],
      initialized: false
    };
  }
  return state.columns[columnKey];
}

function syncDimensionFilterColumnOptions(dimensionKey, columnKey, rows) {
  const options = getDimensionFilterOptions(columnKey, rows);
  const columnState = ensureDimensionColumnState(dimensionKey, columnKey);

  if (!columnState.initialized) {
    columnState.selectedValues = columnKey === "cost"
      ? DEFAULT_COST_FILTER_VALUES.filter((value) => options.includes(value))
      : [];
    columnState.initialized = true;
    return options;
  }

  columnState.selectedValues = columnState.selectedValues.filter((value) => options.includes(value));
  return options;
}

function applyDimensionFilters(dimensionKey, rows, columns) {
  const state = getDimensionFilterState(dimensionKey);

  return rows.filter((row) => {
    return columns.every((column) => {
      const columnState = ensureDimensionColumnState(dimensionKey, column.key);
      const selectedSet = new Set(columnState.selectedValues);
      const hasFilter = selectedSet.size > 0;
      if (!hasFilter) {
        return true;
      }

      if (column.key === "cost") {
        const costValue = Number(row.cost || 0);
        return (selectedSet.has("=0") && costValue === 0)
          || (selectedSet.has(">0") && costValue > 0);
      }

      const value = String(row[column.key] || "-").trim() || "-";
      return selectedSet.has(value);
    });
  });
}

function renderDimensionFilterHeader(dimensionKey, columnKey, label, options) {
  const state = getDimensionFilterState(dimensionKey);
  const columnState = ensureDimensionColumnState(dimensionKey, columnKey);
  const selectedSet = new Set(columnState.selectedValues);
  const isOpen = state.openColumn === columnKey;
  const isFiltered = columnState.selectedValues.length > 0 && columnState.selectedValues.length !== options.length;
  const searchKeyword = (columnState.search || "").trim().toLowerCase();
  const visibleOptions = options.filter((option) => option.toLowerCase().includes(searchKeyword));

  const optionList = visibleOptions.length > 0
    ? visibleOptions.map((option) => {
      const encodedValue = encodeURIComponent(option);
      const checked = selectedSet.has(option) ? "checked" : "";
      return `<label class="filter-option-item"><input type="checkbox" data-action="toggle-option" data-dimension="${dimensionKey}" data-column="${columnKey}" data-value="${encodedValue}" ${checked} />${escapeHtml(option)}</label>`;
    }).join("")
    : '<div class="filter-empty">无匹配项</div>';

  const panel = isOpen
    ? `
      <div class="filter-popup" data-filter-popup="${columnKey}">
        <input type="text" class="filter-search" data-action="search" data-dimension="${dimensionKey}" data-column="${columnKey}" value="${escapeAttr(columnState.search)}" placeholder="搜索..." />
        <div class="filter-actions">
          <button type="button" class="filter-link" data-action="select-all" data-dimension="${dimensionKey}" data-column="${columnKey}">全选</button>
          <button type="button" class="filter-link filter-link-danger" data-action="clear-all" data-dimension="${dimensionKey}" data-column="${columnKey}">清除</button>
        </div>
        <div class="filter-options">${optionList}</div>
      </div>
    `
    : "";

  return `
    <th class="filterable-th">
      <div class="filter-head-row">
        <span>${escapeHtml(label)}</span>
        <button type="button" class="filter-toggle ${isFiltered ? "filtered" : ""}" data-action="toggle-filter" data-dimension="${dimensionKey}" data-column="${columnKey}">▼</button>
      </div>
      ${panel}
    </th>
  `;
}

function bindDimensionFilterEvents(container, dimensionKey) {
  if (dimensionFilterEventsBound[dimensionKey]) {
    return;
  }

  container.addEventListener("click", (event) => {
    const toggleBtn = event.target.closest('[data-action="toggle-filter"]');
    if (toggleBtn && toggleBtn.getAttribute("data-dimension") === dimensionKey) {
      event.stopPropagation();
      const columnKey = toggleBtn.getAttribute("data-column") || "";
      const state = getDimensionFilterState(dimensionKey);
      state.openColumn = state.openColumn === columnKey ? "" : columnKey;
      renderDimensionTableByKey(dimensionKey);
      return;
    }

    const actionBtn = event.target.closest('[data-action="select-all"], [data-action="clear-all"]');
    if (actionBtn && actionBtn.getAttribute("data-dimension") === dimensionKey) {
      event.stopPropagation();
      const columnKey = actionBtn.getAttribute("data-column") || "";
      const state = ensureDimensionColumnState(dimensionKey, columnKey);
      const rows = getDimensionRowsByKey(dimensionKey);
      const options = getDimensionFilterOptions(columnKey, rows);

      if (actionBtn.getAttribute("data-action") === "select-all") {
        state.selectedValues = [...options];
      } else {
        state.selectedValues = [];
        state.search = "";
      }
      renderDimensionTableByKey(dimensionKey);
      return;
    }
  });

  container.addEventListener("input", (event) => {
    const searchInput = event.target.closest('input[data-action="search"]');
    if (!searchInput || searchInput.getAttribute("data-dimension") !== dimensionKey) {
      return;
    }

    const columnKey = searchInput.getAttribute("data-column") || "";
    const state = ensureDimensionColumnState(dimensionKey, columnKey);
    state.search = searchInput.value || "";
    const caretPos = searchInput.selectionStart || (searchInput.value || "").length;
    renderDimensionTableByKey(dimensionKey);

    const nextInput = container.querySelector(`input[data-action="search"][data-dimension="${dimensionKey}"][data-column="${columnKey}"]`);
    if (nextInput) {
      nextInput.focus();
      nextInput.setSelectionRange(caretPos, caretPos);
    }
  });

  container.addEventListener("change", (event) => {
    const checkbox = event.target.closest('input[data-action="toggle-option"]');
    if (!checkbox || checkbox.getAttribute("data-dimension") !== dimensionKey) {
      return;
    }

    const columnKey = checkbox.getAttribute("data-column") || "";
    const encodedValue = checkbox.getAttribute("data-value") || "";
    const optionValue = decodeURIComponent(encodedValue);
    const state = ensureDimensionColumnState(dimensionKey, columnKey);
    const selectedSet = new Set(state.selectedValues);
    if (checkbox.checked) {
      selectedSet.add(optionValue);
    } else {
      selectedSet.delete(optionValue);
    }
    state.selectedValues = Array.from(selectedSet);
    renderDimensionTableByKey(dimensionKey);
  });

  document.addEventListener("click", (event) => {
    const state = getDimensionFilterState(dimensionKey);
    if (!state.openColumn) {
      return;
    }
    if (container.contains(event.target)) {
      return;
    }
    state.openColumn = "";
    renderDimensionTableByKey(dimensionKey);
  });

  dimensionFilterEventsBound[dimensionKey] = true;
}

function getDimensionRowsByKey(dimensionKey) {
  if (!currentResult) {
    return [];
  }
  if (dimensionKey === "daily") {
    return getDailyRows(currentResult);
  }
  if (dimensionKey === "resourceType") {
    return currentResult.byResourceType || [];
  }
  if (dimensionKey === "date") {
    return currentResult.byDate || [];
  }
  return [];
}

function renderDimensionTableByKey(dimensionKey) {
  if (dimensionKey === "daily") {
    renderTable(dailyTableWrapEl, getDailyColumns(), getDimensionRowsByKey("daily"), { dimensionKey: "daily", enableFilters: true });
    return;
  }
  if (dimensionKey === "resourceType") {
    renderTable(resourceTypeTableWrapEl, getResourceColumns(), getDimensionRowsByKey("resourceType"), { dimensionKey: "resourceType", enableFilters: true });
    return;
  }
  if (dimensionKey === "date") {
    renderTable(dateTableWrapEl, getDateColumns(), getDimensionRowsByKey("date"), { dimensionKey: "date", enableFilters: true });
  }
}

function applySubscriptionFilters(rows) {
  const tenantSelected = new Set(subscriptionFilters.tenantName.selectedValues);
  const nameSelected = new Set(subscriptionFilters.subscriptionName.selectedValues);
  const idSelected = new Set(subscriptionFilters.subscriptionId.selectedValues);
  const costSelected = new Set(subscriptionFilters.cost.selectedValues);
  const hasTenantFilter = tenantSelected.size > 0;
  const hasNameFilter = nameSelected.size > 0;
  const hasIdFilter = idSelected.size > 0;
  const hasCostFilter = costSelected.size > 0;

  return rows.filter((row) => {
    const tenantName = String(row.tenantName || "-").trim() || "-";
    const name = String(row.subscriptionName || "-").trim() || "-";
    const id = String(row.subscriptionId || "-").trim() || "-";
    const costValue = Number(row.cost || 0);
    const tenantMatched = !hasTenantFilter || tenantSelected.has(tenantName);
    const nameMatched = !hasNameFilter || nameSelected.has(name);
    const idMatched = !hasIdFilter || idSelected.has(id);
    const costMatched = !hasCostFilter
      || (costSelected.has("=0") && costValue === 0)
      || (costSelected.has(">0") && costValue > 0);
    return tenantMatched && nameMatched && idMatched && costMatched;
  });
}

function renderSubscriptionFilterHeader(columnKey, label, options) {
  const columnState = subscriptionFilters[columnKey];
  const selectedSet = new Set(columnState.selectedValues);
  const isOpen = subscriptionFilters.openColumn === columnKey;
  const isFiltered = columnState.selectedValues.length > 0 && columnState.selectedValues.length !== options.length;
  const searchKeyword = (columnState.search || "").trim().toLowerCase();
  const visibleOptions = options.filter((option) => option.toLowerCase().includes(searchKeyword));

  const optionList = visibleOptions.length > 0
    ? visibleOptions.map((option) => {
      const encodedValue = encodeURIComponent(option);
      const checked = selectedSet.has(option) ? "checked" : "";
      return `<label class="filter-option-item"><input type="checkbox" data-action="toggle-option" data-column="${columnKey}" data-value="${encodedValue}" ${checked} />${escapeHtml(option)}</label>`;
    }).join("")
    : '<div class="filter-empty">无匹配项</div>';

  const panel = isOpen
    ? `
      <div class="filter-popup" data-filter-popup="${columnKey}">
        <input type="text" class="filter-search" data-action="search" data-column="${columnKey}" value="${escapeAttr(columnState.search)}" placeholder="搜索..." />
        <div class="filter-actions">
          <button type="button" class="filter-link" data-action="select-all" data-column="${columnKey}">全选</button>
          <button type="button" class="filter-link filter-link-danger" data-action="clear-all" data-column="${columnKey}">清除</button>
        </div>
        <div class="filter-options">${optionList}</div>
      </div>
    `
    : "";

  return `
    <th class="filterable-th ${columnKey === "subscriptionId" ? "subscription-id-header" : ""}">
      <div class="filter-head-row">
        <span>${escapeHtml(label)}</span>
        <button type="button" class="filter-toggle ${isFiltered ? "filtered" : ""}" data-action="toggle-filter" data-column="${columnKey}">▼</button>
      </div>
      ${panel}
    </th>
  `;
}

function bindSubscriptionFilterEvents(container) {
  if (subscriptionFilterEventsBound) {
    return;
  }

  container.addEventListener("click", (event) => {
    const toggleBtn = event.target.closest('[data-action="toggle-filter"]');
    if (toggleBtn) {
      event.stopPropagation();
      const columnKey = toggleBtn.getAttribute("data-column");
      subscriptionFilters.openColumn = subscriptionFilters.openColumn === columnKey ? "" : (columnKey || "");
      renderSubscriptionTable(container, latestSubscriptionRows);
      return;
    }

    const actionBtn = event.target.closest('[data-action="select-all"], [data-action="clear-all"]');
    if (actionBtn) {
      event.stopPropagation();
      const columnKey = actionBtn.getAttribute("data-column");
      if (!columnKey || !subscriptionFilters[columnKey]) {
        return;
      }

      const options = getSubscriptionFilterOptions(columnKey, latestSubscriptionRows);
      if (actionBtn.getAttribute("data-action") === "select-all") {
        subscriptionFilters[columnKey].selectedValues = [...options];
      } else {
        subscriptionFilters[columnKey].selectedValues = [];
        subscriptionFilters[columnKey].search = "";
      }
      renderSubscriptionTable(container, latestSubscriptionRows);
    }
  });

  container.addEventListener("input", (event) => {
    const searchInput = event.target.closest('input[data-action="search"]');
    if (!searchInput) {
      return;
    }

    const columnKey = searchInput.getAttribute("data-column");
    if (!columnKey || !subscriptionFilters[columnKey]) {
      return;
    }

    subscriptionFilters[columnKey].search = searchInput.value || "";
    const caretPos = searchInput.selectionStart || (searchInput.value || "").length;
    renderSubscriptionTable(container, latestSubscriptionRows);

    const nextInput = container.querySelector(`input[data-action="search"][data-column="${columnKey}"]`);
    if (nextInput) {
      nextInput.focus();
      nextInput.setSelectionRange(caretPos, caretPos);
    }
  });

  container.addEventListener("change", (event) => {
    const checkbox = event.target.closest('input[data-action="toggle-option"]');
    if (!checkbox) {
      return;
    }

    const columnKey = checkbox.getAttribute("data-column");
    const encodedValue = checkbox.getAttribute("data-value") || "";
    const optionValue = decodeURIComponent(encodedValue);
    if (!columnKey || !subscriptionFilters[columnKey]) {
      return;
    }

    const selectedSet = new Set(subscriptionFilters[columnKey].selectedValues);
    if (checkbox.checked) {
      selectedSet.add(optionValue);
    } else {
      selectedSet.delete(optionValue);
    }
    subscriptionFilters[columnKey].selectedValues = Array.from(selectedSet);
    renderSubscriptionTable(container, latestSubscriptionRows);
  });

  document.addEventListener("click", (event) => {
    if (!subscriptionFilters.openColumn) {
      return;
    }
    if (container.contains(event.target)) {
      return;
    }
    subscriptionFilters.openColumn = "";
    renderSubscriptionTable(container, latestSubscriptionRows);
  });

  subscriptionFilterEventsBound = true;
}

function formatSubscriptionCell(row, key) {
  if (key === "cost") {
    return toMoney(Number(row[key] || 0));
  }

  const value = row[key];
  return value === undefined || value === null || value === "" ? "-" : String(value);
}

function getResourceColumns() {
  return [
    { key: "resourceName", label: "Resource Name" },
    { key: "tenantName", label: "Tenant Name" },
    { key: "subscription", label: "Subscription" },
    { key: "location", label: "Location" },
    { key: "serviceName", label: "Service Name" },
    { key: "cost", label: "Cost" },
  ];
}

function getDateColumns() {
  return [
    { key: "date", label: "Date" },
    { key: "tenantName", label: "Tenant Name" },
    { key: "subscription", label: "Subscription" },
    { key: "resourceName", label: "Resource Name" },
    { key: "product", label: "Product" },
    { key: "cost", label: "Cost" },
  ];
}

function getDailyColumns() {
  return [
    { key: "date", label: "Date" },
    { key: "tenantName", label: "Tenant Name" },
    { key: "subscriptionName", label: "Subscription Name" },
    { key: "subscriptionId", label: "Subscription ID" },
    { key: "cost", label: "Cost" },
  ];
}

function getDailyRows(result) {
  if (!result || !Array.isArray(result.byDate) || result.byDate.length === 0) {
    return [];
  }

  const subNameToId = new Map(
    (result.bySubscription || []).map((item) => [
      `${String(item.tenantName || "-").trim()}__${String(item.subscriptionName || "").trim()}`,
      String(item.subscriptionId || "-").trim() || "-"
    ])
  );

  const grouped = new Map();
  result.byDate.forEach((row) => {
    const tenantName = String(row.tenantName || "-").trim() || "-";
    const date = String(row.date || "-").trim() || "-";
    const subscriptionName = String(row.subscription || "-").trim() || "-";
    const subscriptionId = subNameToId.get(`${tenantName}__${subscriptionName}`) || "-";
    const key = `${tenantName}__${date}__${subscriptionName}`;
    const prev = grouped.get(key);

    if (prev) {
      prev.cost += Number(row.cost || 0);
      return;
    }

    grouped.set(key, {
      tenantName,
      date,
      subscriptionName,
      subscriptionId,
      cost: Number(row.cost || 0)
    });
  });

  return Array.from(grouped.values()).sort((a, b) => {
    if (a.tenantName !== b.tenantName) {
      return a.tenantName.localeCompare(b.tenantName);
    }
    if (a.date !== b.date) {
      return a.date.localeCompare(b.date);
    }
    return a.subscriptionName.localeCompare(b.subscriptionName);
  });
}

function bindRowSelection(container) {
  const rows = Array.from(container.querySelectorAll("tbody tr.selectable-row"));
  if (rows.length === 0) {
    return;
  }

  const selectRow = (targetRow) => {
    rows.forEach((row) => {
      const isSelected = row === targetRow;
      row.classList.toggle("selected", isSelected);
      if (isSelected) {
        row.setAttribute("aria-selected", "true");
      } else {
        row.removeAttribute("aria-selected");
      }
    });
  };

  rows.forEach((row) => {
    row.addEventListener("click", () => {
      selectRow(row);
    });

    row.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") {
        return;
      }
      event.preventDefault();
      selectRow(row);
    });
  });
}

function formatCellValue(value, column) {
  if (column === "cost") {
    return toMoney(Number(value || 0));
  }
  return value === undefined || value === null || value === "" ? "-" : String(value);
}

function toMoney(value) {
  return Number(value || 0).toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  });
}

function initPresetButtons() {
  presetBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      presetBtns.forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");

      if (btn.dataset.preset === "custom") {
        if (customRangeRowEl) customRangeRowEl.hidden = false;
        syncDateInputConstraints();
      } else {
        if (customRangeRowEl) customRangeRowEl.hidden = true;
        applyPreset(btn.dataset.preset);
      }
    });
  });

  startDateEl.addEventListener("change", syncDateInputConstraints);
  endDateEl.addEventListener("change", syncDateInputConstraints);

  // 默认选择"上月"
  const lastMonthBtn = presetBtns.find((btn) => btn.dataset.preset === "last-month");
  if (lastMonthBtn) {
    lastMonthBtn.classList.add("active");
    applyPreset("last-month");
  } else {
    presetBtns.forEach((btn) => btn.classList.remove("active"));
    if (customRangeRowEl) customRangeRowEl.hidden = true;
    startDateEl.value = "";
    endDateEl.value = "";
  }
  syncDateInputConstraints();
}

function applyPreset(preset) {
  const today = new Date();
  let start, end;

  if (preset === "last-month") {
    start = new Date(today.getFullYear(), today.getMonth() - 1, 1);
    end = new Date(today.getFullYear(), today.getMonth(), 0);
  } else if (preset === "this-month") {
    start = new Date(today.getFullYear(), today.getMonth(), 1);
    end = today;
  } else if (preset === "this-quarter") {
    const quarter = Math.floor(today.getMonth() / 3);
    start = new Date(today.getFullYear(), quarter * 3, 1);
    end = today;
  } else if (preset === "this-year") {
    start = new Date(today.getFullYear(), 0, 1);
    end = today;
  } else if (preset === "yesterday") {
    const yesterday = new Date(today);
    yesterday.setDate(today.getDate() - 1);
    start = yesterday;
    end = yesterday;
  }

  if (start && end) {
    startDateEl.value = formatDate(start);
    endDateEl.value = formatDate(end);
  }
}

function validateDates(startDate, endDate) {
  if (!startDate || !endDate) {
    throw new Error("请填写开始时间和结束时间");
  }
  if (new Date(startDate) > new Date(endDate)) {
    throw new Error("开始时间不能晚于结束时间");
  }
}

function syncDateInputConstraints() {
  const startDate = startDateEl.value;
  const endDate = endDateEl.value;

  if (startDate) {
    endDateEl.min = startDate;
  } else {
    endDateEl.removeAttribute("min");
  }

  if (endDate) {
    startDateEl.max = endDate;
  } else {
    startDateEl.removeAttribute("max");
  }

  if (startDate && endDate && new Date(endDate) < new Date(startDate)) {
    endDateEl.value = startDate;
  }
}

function setCacheInfo(text, isError = false, isLoading = false) {
  if (!cacheInfoEl) {
    return;
  }
  cacheInfoEl.classList.toggle("error", isError);
  cacheInfoEl.classList.toggle("loading", isLoading);
  if (isLoading) {
    cacheInfoEl.innerHTML = `<span class="loading-dots" aria-hidden="true"><span></span><span></span><span></span></span><span>${escapeHtml(text)}</span>`;
  } else {
    cacheInfoEl.textContent = text;
  }
}

function initSettingsModal() {
  if (!openSettingsBtn || !settingsModalEl) {
    return;
  }

  openSettingsBtn.addEventListener("click", async () => {
    await loadSettingsIntoModal();
    openSettingsModal();
  });

  closeSettingsBtn.addEventListener("click", () => {
    closeSettingsModal();
  });

  settingsModalEl.addEventListener("click", (event) => {
    if (event.target === settingsModalEl) {
      closeSettingsModal();
    }
  });

  saveSettingsBtn.addEventListener("click", async () => {
    await saveSettingsFromModal();
  });

  if (deleteAuthParamsBtn) {
    deleteAuthParamsBtn.addEventListener("click", () => {
      clearAuthParameters();
    });
  }

  if (addAuthRowBtn) {
    addAuthRowBtn.addEventListener("click", () => {
      addAuthRow();
    });
  }

  if (exportAuthParamsBtn) {
    exportAuthParamsBtn.addEventListener("click", () => {
      exportAuthParameters();
    });
  }

  if (importAuthParamsBtn && importAuthParamsInput) {
    importAuthParamsBtn.addEventListener("click", () => {
      importAuthParamsInput.click();
    });

    importAuthParamsInput.addEventListener("change", async () => {
      await importAuthParametersFromFile();
    });
  }
}

function openSettingsModal() {
  settingsModalEl.classList.add("open");
  settingsModalEl.setAttribute("aria-hidden", "false");
}

function closeSettingsModal() {
  settingsModalEl.classList.remove("open");
  settingsModalEl.setAttribute("aria-hidden", "true");
}

async function openSettingsModalWithMessage(message, isError = false) {
  await loadSettingsIntoModal();
  openSettingsModal();
  setSettingsModalMessage(message, isError);
}

function hasRequiredCredentials(authConfigs) {
  if (!Array.isArray(authConfigs) || authConfigs.length === 0) {
    return false;
  }
  return authConfigs.some(
    (cfg) => Boolean(
      cfg &&
      String(cfg.azureClientId || "").trim() &&
      String(cfg.azureClientSecret || "").trim() &&
      String(cfg.azureTenantId || "").trim() &&
      String(cfg.tenantName || "").trim()
    )
  );
}

function clearAuthParameters() {
  const shouldClear = window.confirm("确认删除本地保存的认证参数设置及本地缓存数据吗？该操作不可恢复。");
  if (!shouldClear) {
    return;
  }

  clearAuthConfigs();
  authTableRows = [{ azureClientId: "", azureClientSecret: "", azureTenantId: "", tenantName: "", subscriptionId: "" }];
  renderAuthTable();
  clearConfig();
  clearQueryCache();
  currentResult = null;
  currentRangeText = "-";
  resetAllTableFilters();
  renderSummary(null);
  renderSubscriptionTable(subscriptionTableWrapEl, []);
  renderTable(dailyTableWrapEl, getDailyColumns(), [], { dimensionKey: "daily", enableFilters: true });
  renderTable(resourceTypeTableWrapEl, getResourceColumns(), [], { dimensionKey: "resourceType", enableFilters: true });
  renderTable(dateTableWrapEl, getDateColumns(), [], { dimensionKey: "date", enableFilters: true });
  setCacheInfo("");
  setSettingsModalMessage("本地认证参数设置及缓存数据已删除");
}

function renderAuthTable() {
  if (!authTableBody) {
    return;
  }

  authTableBody.innerHTML = "";

  const tableRows = authTableRows && authTableRows.length > 0 ? authTableRows : [{ azureClientId: "", azureClientSecret: "", azureTenantId: "", tenantName: "", subscriptionId: "" }];

  tableRows.forEach((auth, index) => {
    const tr = document.createElement("tr");
    tr.setAttribute("data-row-index", index);

    const tdIndex = document.createElement("td");
    tdIndex.textContent = index + 1;
    tr.appendChild(tdIndex);

    const tdClientId = document.createElement("td");
    const inputClientId = document.createElement("input");
    inputClientId.type = "text";
    inputClientId.value = auth.azureClientId || "";
    inputClientId.className = "auth-input auth-client-id";
    inputClientId.placeholder = "App_Client_ID";
    tdClientId.appendChild(inputClientId);
    tr.appendChild(tdClientId);

    const tdSecret = document.createElement("td");
    const inputSecret = document.createElement("input");
    inputSecret.type = "password";
    inputSecret.className = "auth-input auth-secret";
    inputSecret.placeholder = "App_Client_Secret";
    const realSecret = auth.azureClientSecret || "";
    inputSecret.value = realSecret ? maskSecretKey(realSecret) : "";
    inputSecret.setAttribute("data-realValue", realSecret);
    inputSecret.addEventListener("focus", () => {
      const rv = inputSecret.getAttribute("data-realValue");
      if (rv) {
        inputSecret.value = rv;
      }
    });
    inputSecret.addEventListener("blur", () => {
      const rv = inputSecret.getAttribute("data-realValue");
      if (rv) {
        inputSecret.value = maskSecretKey(rv);
      }
    });
    inputSecret.addEventListener("input", () => {
      inputSecret.setAttribute("data-realValue", inputSecret.value);
    });
    tdSecret.appendChild(inputSecret);
    tr.appendChild(tdSecret);

    const tdTenantId = document.createElement("td");
    const inputTenantId = document.createElement("input");
    inputTenantId.type = "text";
    inputTenantId.value = auth.azureTenantId || "";
    inputTenantId.className = "auth-input auth-tenant-id";
    inputTenantId.placeholder = "Tenant_ID";
    tdTenantId.appendChild(inputTenantId);
    tr.appendChild(tdTenantId);

    const tdTenantName = document.createElement("td");
    const inputTenantName = document.createElement("input");
    inputTenantName.type = "text";
    inputTenantName.value = auth.tenantName || "";
    inputTenantName.className = "auth-input auth-tenant-name";
    inputTenantName.placeholder = "Tenant_Name";
    tdTenantName.appendChild(inputTenantName);
    tr.appendChild(tdTenantName);

    const tdSubscriptionId = document.createElement("td");
    const inputSubscriptionId = document.createElement("input");
    inputSubscriptionId.type = "text";
    inputSubscriptionId.value = auth.subscriptionId || "";
    inputSubscriptionId.className = "auth-input auth-subscription-id";
    inputSubscriptionId.placeholder = "Subscription_ID (可选)";
    tdSubscriptionId.appendChild(inputSubscriptionId);
    tr.appendChild(tdSubscriptionId);

    const tdDelete = document.createElement("td");
    const btnDelete = document.createElement("button");
    btnDelete.type = "button";
    btnDelete.textContent = "删除";
    btnDelete.className = "auth-delete-btn";
    btnDelete.addEventListener("click", () => {
      deleteAuthRow(index);
    });
    tdDelete.appendChild(btnDelete);
    tr.appendChild(tdDelete);

    authTableBody.appendChild(tr);
  });
}

function collectAuthTableRows() {
  if (!authTableBody) {
    return [];
  }

  const rows = authTableBody.querySelectorAll("tr");
  const collected = [];

  rows.forEach((tr) => {
    const clientIdInput = tr.querySelector(".auth-client-id");
    const secretInput = tr.querySelector(".auth-secret");
    const tenantIdInput = tr.querySelector(".auth-tenant-id");
    const tenantNameInput = tr.querySelector(".auth-tenant-name");
    const subscriptionIdInput = tr.querySelector(".auth-subscription-id");

    if (!clientIdInput || !secretInput || !tenantIdInput || !tenantNameInput) {
      return;
    }

    const clientId = clientIdInput.value.trim();
    const secret = (secretInput.getAttribute("data-realValue") || secretInput.value).trim();
    const tenantId = tenantIdInput.value.trim();
    const tenantName = tenantNameInput.value.trim();
    const subscriptionId = subscriptionIdInput ? subscriptionIdInput.value.trim() : "";

    if (clientId || secret || tenantId || tenantName) {
      collected.push({
        azureClientId: clientId,
        azureClientSecret: secret,
        azureTenantId: tenantId,
        tenantName,
        subscriptionId: subscriptionId
      });
    }
  });

  return collected;
}

function getAuthFieldLabelByKey(key) {
  const labelMap = {
    azureClientId: "APP_Client_ID",
    azureClientSecret: "APP_Client_Secret",
    azureTenantId: "Tenant_ID",
    tenantName: "Tenant_Name",
    subscriptionId: "Subscription_ID"
  };
  return labelMap[key] || key;
}

function validateAuthConfigsUniqueValues(authConfigs) {
  if (!Array.isArray(authConfigs) || authConfigs.length === 0) {
    return;
  }

  const valueMap = new Map();
  const fieldOrder = ["azureClientId", "azureClientSecret", "azureTenantId", "tenantName", "subscriptionId"];

  for (let rowIndex = 0; rowIndex < authConfigs.length; rowIndex++) {
    const cfg = authConfigs[rowIndex] || {};

    for (const fieldKey of fieldOrder) {
      const rawValue = String(cfg[fieldKey] || "").trim();
      if (!rawValue) {
        continue;
      }

      if (valueMap.has(rawValue)) {
        const first = valueMap.get(rawValue);
        throw new Error(
          `检测到重复值：第 ${first.row + 1} 行 ${getAuthFieldLabelByKey(first.field)} 与第 ${rowIndex + 1} 行 ${getAuthFieldLabelByKey(fieldKey)} 相同`
        );
      }

      valueMap.set(rawValue, { row: rowIndex, field: fieldKey });
    }
  }
}

function syncAuthTableRowsFromDom() {
  if (!authTableBody) {
    return;
  }

  authTableRows = collectAuthTableRows();
}

function addAuthRow() {
  if (!authTableBody) {
    return;
  }

  syncAuthTableRowsFromDom();

  authTableRows.push({
    azureClientId: "",
    azureClientSecret: "",
    azureTenantId: "",
    tenantName: "",
    subscriptionId: ""
  });

  renderAuthTable();
}

function deleteAuthRow(index) {
  syncAuthTableRowsFromDom();

  if (authTableRows && authTableRows.length > 0 && index >= 0 && index < authTableRows.length) {
    authTableRows.splice(index, 1);
  }

  if (authTableRows.length === 0) {
    authTableRows = [{ azureClientId: "", azureClientSecret: "", azureTenantId: "", tenantName: "", subscriptionId: "" }];
  }

  renderAuthTable();
}

function exportAuthParameters() {
  try {
    const authConfigs = collectAuthTableRows();
    if (authConfigs.length === 0 || !hasRequiredCredentials(authConfigs)) {
      throw new Error("请先完整填写至少一行认证参数后再导出");
    }

    const invalidRowIndex = authConfigs.findIndex(
      (cfg) => !String(cfg.azureClientId || "").trim()
        || !String(cfg.azureClientSecret || "").trim()
        || !String(cfg.azureTenantId || "").trim()
        || !String(cfg.tenantName || "").trim()
    );
    if (invalidRowIndex >= 0) {
      throw new Error(`第 ${invalidRowIndex + 1} 行认证信息不完整，请补全必填项`);
    }

    const hasMaskedSecret = authConfigs.some((cfg) => isMaskedSecret(cfg.azureClientSecret));
    if (hasMaskedSecret) {
      throw new Error("部分 App_Client_Secret 为掩码值，请输入真实 Secret Value 后再导出");
    }

    validateAuthConfigsUniqueValues(authConfigs);

    const header = ["App_Client_ID", "App_Client_Secret", "Tenant_ID", "Tenant_Name", "Subscription_ID"];
    const rows = authConfigs.map((cfg) => [
      cfg.azureClientId,
      cfg.azureClientSecret,
      cfg.azureTenantId,
      cfg.tenantName,
      cfg.subscriptionId || ""
    ]);
    const csvText = `${header.join(",")}\n${rows.map((row) => row.join(",")).join("\n")}`;
    const blob = new Blob([`\uFEFF${csvText}`], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `azure-auth-config-${formatFileTimestamp()}.csv`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);

    setSettingsModalMessage("认证参数已导出");
  } catch (error) {
    setSettingsModalMessage(error.message || "认证参数导出失败", true);
  }
}

async function importAuthParametersFromFile() {
  if (!importAuthParamsInput || !importAuthParamsInput.files || importAuthParamsInput.files.length === 0) {
    return;
  }

  try {
    const file = importAuthParamsInput.files[0];
    const fileName = String(file.name || "").toLowerCase();
    const fileType = String(file.type || "").toLowerCase();
    if (!fileName.endsWith(".csv")) {
      throw new Error("仅支持导入 .csv 格式文件");
    }
    if (fileType && !fileType.startsWith("text/")) {
      throw new Error("仅支持导入 .csv 后缀的文本文件");
    }

    const content = await file.text();
    const importedConfigs = parseAuthConfigsFromCsv(content);

    if (importedConfigs.length === 0) {
      throw new Error("CSV 文件中没有有效的认证数据");
    }

    authTableRows = importedConfigs;
    renderAuthTable();
    saveAuthConfigs(importedConfigs);

    setSettingsModalMessage(`认证参数导入成功，共导入 ${importedConfigs.length} 行`);
  } catch (error) {
    setSettingsModalMessage(error.message || "认证参数导入失败", true);
  } finally {
    importAuthParamsInput.value = "";
  }
}

function parseAuthConfigsFromCsv(content) {
  const lines = String(content || "")
    .replace(/^\uFEFF/, "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0);

  if (lines.length < 2) {
    throw new Error("CSV 文件内容无效，至少需要表头和一行数据");
  }

  const normalizeCsvHeader = (header) => String(header || "")
    .trim()
    .replace(/^\"|\"$/g, "")
    .replace(/^\'|\'$/g, "")
    .toUpperCase();

  const headerValues = lines[0].split(",").map((item) => normalizeCsvHeader(item));

  const indexClientId = headerValues.indexOf("APP_CLIENT_ID");
  const indexClientSecret = headerValues.indexOf("APP_CLIENT_SECRET");
  const indexTenantId = headerValues.indexOf("TENANT_ID");
  const indexTenantName = headerValues.indexOf("TENANT_NAME");
  const indexSubscriptionId = headerValues.indexOf("SUBSCRIPTION_ID");

  if (indexClientId < 0 || indexClientSecret < 0 || indexTenantId < 0 || indexTenantName < 0) {
    throw new Error("CSV 表头缺少必填列：App_Client_ID、App_Client_Secret、Tenant_ID、Tenant_Name");
  }

  const configs = [];
  for (let i = 1; i < lines.length; i++) {
    const dataValues = lines[i].split(",").map((item) => item.trim());

    const azureClientId = String(dataValues[indexClientId] || "").trim();
    const azureClientSecret = String(dataValues[indexClientSecret] || "").trim();
    const azureTenantId = String(dataValues[indexTenantId] || "").trim();
    const tenantName = String(dataValues[indexTenantName] || "").trim();
    const subscriptionId = indexSubscriptionId >= 0 ? String(dataValues[indexSubscriptionId] || "").trim() : "";

    if (!azureClientId || !azureClientSecret || !azureTenantId || !tenantName) {
      continue;
    }
    if (isMaskedSecret(azureClientSecret)) {
      throw new Error(`第 ${i + 1} 行的 App_Client_Secret 为掩码值，无法导入`);
    }

    configs.push({
      azureClientId,
      azureClientSecret,
      azureTenantId,
      tenantName,
      subscriptionId
    });
  }

  validateAuthConfigsUniqueValues(configs);

  return configs;
}

function setSettingsModalMessage(text, isError = false) {
  if (!settingsModalMessageEl) {
    return;
  }
  settingsModalMessageEl.textContent = text;
  settingsModalMessageEl.classList.toggle("error", isError);
}

function resetAllTableFilters() {
  subscriptionFilters = {
    openColumn: "",
    tenantName: { search: "", selectedValues: [], initialized: false },
    subscriptionName: { search: "", selectedValues: [], initialized: false },
    subscriptionId: { search: "", selectedValues: [], initialized: false },
    cost: { search: "", selectedValues: [], initialized: false }
  };

  Object.keys(dimensionTableFilters).forEach((dimensionKey) => {
    dimensionTableFilters[dimensionKey].openColumn = "";
    dimensionTableFilters[dimensionKey].columns = {};
  });
}

async function loadSettingsIntoModal() {
  const authConfigs = loadAuthConfigs();
  authTableRows = authConfigs.length > 0 ? [...authConfigs] : [{ azureClientId: "", azureClientSecret: "", azureTenantId: "", tenantName: "", subscriptionId: "" }];
  renderAuthTable();
  setSettingsModalMessage("已加载当前设置");
}

async function saveSettingsFromModal() {
  try {
    const authConfigs = collectAuthTableRows();
    if (authConfigs.length === 0 || !hasRequiredCredentials(authConfigs)) {
      throw new Error("请至少填写一行完整的认证信息（App_Client_ID、App_Client_Secret、Tenant_ID、Tenant_Name）");
    }

    const invalidRowIndex = authConfigs.findIndex(
      (cfg) => !String(cfg.azureClientId || "").trim()
        || !String(cfg.azureClientSecret || "").trim()
        || !String(cfg.azureTenantId || "").trim()
        || !String(cfg.tenantName || "").trim()
    );
    if (invalidRowIndex >= 0) {
      throw new Error(`第 ${invalidRowIndex + 1} 行认证信息不完整，请补全必填项`);
    }

    const hasMaskedSecret = authConfigs.some((cfg) => isMaskedSecret(cfg.azureClientSecret));
    if (hasMaskedSecret) {
      throw new Error("App_Client_Secret 不能保存掩码值，请重新输入 Azure 应用的 Secret Value");
    }

    validateAuthConfigsUniqueValues(authConfigs);

    saveAuthConfigs(authConfigs);

    setSettingsModalMessage("");
  } catch (error) {
    setSettingsModalMessage(error.message || "设置保存失败", true);
  }
}

function formatDate(date) {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

function maskSecretKey(secret) {
  if (!secret || secret.length === 0) {
    return "";
  }
  if (secret.length <= 4) {
    return secret;
  }
  const first4 = secret.substring(0, 4);
  const restMasked = "*".repeat(secret.length - 4);
  return first4 + restMasked;
}

function isMaskedSecret(secret) {
  return typeof secret === "string" && /\*{3,}/.test(secret);
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function escapeAttr(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/\"/g, "&quot;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function initDimensionTabs() {
  if (!dimensionTabsEl) {
    return;
  }

  const tabs = Array.from(dimensionTabsEl.querySelectorAll(".dimension-tab"));
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      const dimension = tab.getAttribute("data-dimension");
      if (!dimension) {
        return;
      }
      switchDimensionPanel(dimension);
    });
  });
}

function switchDimensionPanel(dimension) {
  activeDimension = dimension;

  dimensionPanels.forEach((panel) => {
    const panelDimension = panel.getAttribute("data-dimension-panel");
    const isActive = panelDimension === dimension;
    panel.classList.toggle("dimension-panel-hidden", !isActive);
  });

  if (!dimensionTabsEl) {
    return;
  }

  const tabs = Array.from(dimensionTabsEl.querySelectorAll(".dimension-tab"));
  tabs.forEach((tab) => {
    const isActive = tab.getAttribute("data-dimension") === dimension;
    tab.classList.toggle("active", isActive);
    tab.setAttribute("aria-selected", isActive ? "true" : "false");
  });

  downloadSubscriptionBtn.hidden = dimension !== "subscription";
  downloadDailyBtn.hidden = dimension !== "daily";
  downloadResourceTypeBtn.hidden = dimension !== "resourceType";
  downloadDateBtn.hidden = dimension !== "date";
}

function loadCachedResult() {
  try {
    const raw = localStorage.getItem(QUERY_CACHE_KEY);
    if (!raw) {
      return;
    }

    const cached = JSON.parse(raw);
    if (!cached || typeof cached !== "object" || !cached.result || typeof cached.result !== "object") {
      return;
    }

    if (cached.startDate) {
      startDateEl.value = String(cached.startDate);
    }
    if (cached.endDate) {
      endDateEl.value = String(cached.endDate);
    }

    syncDateInputConstraints();
    if (cached.startDate && cached.endDate) {
      currentRangeText = `${cached.startDate} ~ ${cached.endDate}`;
    }

    currentResult = cached.result;
    renderSummary(currentResult);
    renderSubscriptionTable(subscriptionTableWrapEl, currentResult.bySubscription || []);
    renderTable(dailyTableWrapEl, getDailyColumns(), getDailyRows(currentResult), { dimensionKey: "daily", enableFilters: true });
    renderTable(resourceTypeTableWrapEl, getResourceColumns(), currentResult.byResourceType || [], { dimensionKey: "resourceType", enableFilters: true });
    renderTable(dateTableWrapEl, getDateColumns(), currentResult.byDate || [], { dimensionKey: "date", enableFilters: true });

    const cachedAt = cached.cachedAt ? `，缓存时间: ${formatLocalDateTime(cached.cachedAt)}` : "";
    setCacheInfo(`已加载本地缓存查询结果${cachedAt}`);
  } catch (error) {
    console.error("加载查询缓存失败", error);
  }
}

function saveQueryCache(payload) {
  try {
    localStorage.setItem(QUERY_CACHE_KEY, JSON.stringify(payload));
  } catch (error) {
    console.error("保存查询缓存失败", error);
  }
}

function clearQueryCache() {
  localStorage.removeItem(QUERY_CACHE_KEY);
}

function formatLocalDateTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value || "-");
  }

  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  const hh = String(date.getHours()).padStart(2, "0");
  const mm = String(date.getMinutes()).padStart(2, "0");
  const ss = String(date.getSeconds()).padStart(2, "0");
  return `${y}-${m}-${d} ${hh}:${mm}:${ss}`;
}

