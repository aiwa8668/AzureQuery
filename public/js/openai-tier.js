const openAiTierStatusEl = document.getElementById("openAiTierStatus");
const openAiTierLoginBtn = document.getElementById("openAiTierLoginBtn");
const openAiTierLogoutBtn = document.getElementById("openAiTierLogoutBtn");
const openAiTierRefreshSubsBtn = document.getElementById("openAiTierRefreshSubsBtn");
const openAiTierQueryBtn = document.getElementById("openAiTierQueryBtn");
const openAiTierDownloadBtn = document.getElementById("openAiTierDownloadBtn");
const openAiTierSummaryGridEl = document.getElementById("openAiTierSummaryGrid");
const openAiTierTableWrapEl = document.getElementById("openAiTierTableWrap");
const OPENAI_TIER_CACHE_KEY = "azure-query-openai-tier-cache-v2";

const openAiTierState = {
  configured: false,
  signedIn: false,
  loadingAuthStatus: true,
  loginUrlApi: "",
  logoutUrl: "",
  account: null,
  subscriptions: [],
  selectedSubscriptionIds: new Set(),
  resultRows: [],
  subscriptionDropdownOpen: false,
  subscriptionSearch: ""
};
const openAiTierTableFilters = {
  openColumn: "",
  currentTierName: { search: "", selectedValues: [], initialized: false }
};
let openAiTierFilterEventsBound = false;

initOpenAiTier();

function initOpenAiTier() {
  if (!openAiTierStatusEl || !openAiTierLoginBtn || !openAiTierSummaryGridEl) {
    return;
  }

  updateOpenAiTierAuthButtons();
  setOpenAiTierStatus("正在检查 Microsoft 登录配置...", false, true);

  openAiTierLoginBtn.addEventListener("click", () => {
    startOpenAiTierLogin().catch((error) => {
      setOpenAiTierStatus(error.message || "跳转 Microsoft 登录页失败", true);
    });
  });

  if (openAiTierLogoutBtn) {
    openAiTierLogoutBtn.addEventListener("click", () => {
      if (openAiTierState.logoutUrl) {
        window.location.href = openAiTierState.logoutUrl;
      }
    });
  }

  if (openAiTierRefreshSubsBtn) {
    openAiTierRefreshSubsBtn.addEventListener("click", async () => {
      try {
        await loadOpenAiTierSubscriptions(true);
      } catch (error) {
        setOpenAiTierStatus(error.message || "刷新订阅列表失败", true);
      }
    });
  }

  if (openAiTierQueryBtn) {
    openAiTierQueryBtn.addEventListener("click", async () => {
      await runOpenAiTierQuery();
    });
  }

  if (openAiTierDownloadBtn) {
    openAiTierDownloadBtn.addEventListener("click", () => {
      exportOpenAiTierCsv();
    });
  }

  if (openAiTierTableWrapEl) {
    openAiTierTableWrapEl.addEventListener("click", (event) => {
      const targetRow = event.target.closest("tr.selectable-row");
      if (!targetRow) {
        return;
      }

      const allRows = Array.from(openAiTierTableWrapEl.querySelectorAll("tbody tr.selectable-row"));
      allRows.forEach((row) => {
        const isSelected = row === targetRow;
        row.classList.toggle("selected", isSelected);
        if (isSelected) {
          row.setAttribute("aria-selected", "true");
        } else {
          row.removeAttribute("aria-selected");
        }
      });
    });
  }

  openAiTierSummaryGridEl.addEventListener("change", (event) => {
    const checkbox = event.target.closest("input[data-subscription-id]");
    if (!checkbox) {
      return;
    }

    const subscriptionId = checkbox.getAttribute("data-subscription-id");
    if (!subscriptionId) {
      return;
    }

    if (checkbox.checked) {
      openAiTierState.selectedSubscriptionIds.add(subscriptionId);
    } else {
      openAiTierState.selectedSubscriptionIds.delete(subscriptionId);
    }
    openAiTierState.subscriptionDropdownOpen = true;
    renderOpenAiTierSummary();
    loadOpenAiTierCachedResult();
  });

  openAiTierSummaryGridEl.addEventListener("input", (event) => {
    const searchInput = event.target.closest("[data-action='subscription-search']");
    if (!searchInput) {
      return;
    }

    openAiTierState.subscriptionSearch = String(searchInput.value || "");
    openAiTierState.subscriptionDropdownOpen = true;
    if (!event.isComposing) {
      const keyword = openAiTierState.subscriptionSearch.trim().toLocaleLowerCase("zh-CN");
      openAiTierSummaryGridEl.querySelectorAll(".tier-subscription-option").forEach((item) => {
        item.hidden = !item.textContent.toLocaleLowerCase("zh-CN").includes(keyword);
      });
    }
  });

  openAiTierSummaryGridEl.addEventListener("click", (event) => {
    const actionButton = event.target.closest("[data-picker-action]");
    const toggleButton = event.target.closest("[data-action='toggle-subscription-dropdown']");
    const dropdownMenu = event.target.closest(".tier-summary-menu");
    const inlineQueryButton = event.target.closest("#openAiTierInlineQueryBtn");

    if (actionButton) {
      event.stopPropagation();
      const action = actionButton.getAttribute("data-picker-action");
      if (action === "select-all") {
        setAllOpenAiTierSubscriptionsSelected(true);
      }
      if (action === "clear-all") {
        setAllOpenAiTierSubscriptionsSelected(false);
      }
      return;
    }

    if (dropdownMenu) {
      event.stopPropagation();
      return;
    }

    if (inlineQueryButton) {
      event.stopPropagation();
      runOpenAiTierQuery().catch((error) => {
        setOpenAiTierStatus(error.message || "查询 OpenAI Tier 失败", true);
      });
      return;
    }

    if (toggleButton) {
      event.stopPropagation();
      openAiTierState.subscriptionDropdownOpen = !openAiTierState.subscriptionDropdownOpen;
      if (!openAiTierState.subscriptionDropdownOpen) {
        openAiTierState.subscriptionSearch = "";
      }
      renderOpenAiTierSummary();
      return;
    }
  });

  document.addEventListener("click", (event) => {
    if (!openAiTierState.subscriptionDropdownOpen) {
      return;
    }

    if (openAiTierSummaryGridEl.contains(event.target)) {
      return;
    }

    openAiTierState.subscriptionDropdownOpen = false;
    openAiTierState.subscriptionSearch = "";
    renderOpenAiTierSummary();
  });

  window.addEventListener("app-view-change", (event) => {
    if (event.detail?.view === "openai-tier") {
      window.requestAnimationFrame(syncOpenAiTierSummaryCardWidths);
    }
  });

  renderOpenAiTierSummary();
  renderOpenAiTierTable([]);
  fetchOpenAiTierAuthStatus().catch((error) => {
    setOpenAiTierStatus(error.message || "初始化 OpenAI Tier 查询失败", true);
  });
}

async function fetchOpenAiTierAuthStatus() {
  try {
    const response = await fetch("/api/openai-tier/auth/status");
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.message || "读取登录状态失败");
    }

    openAiTierState.configured = Boolean(data.configured);
    openAiTierState.signedIn = Boolean(data.signedIn);
    openAiTierState.loginUrlApi = data.loginUrlApi || "";
    openAiTierState.logoutUrl = data.logoutUrl || "";
    openAiTierState.account = data.account || null;
    renderOpenAiTierSummary();

    if (!openAiTierState.configured) {
      setOpenAiTierStatus("未配置 Microsoft 登录参数，请先在服务端设置 OPENAI_TIER_* 环境变量。", true);
      return;
    }

    if (data.notice && data.notice.message) {
      setOpenAiTierStatus(data.notice.message, Boolean(data.notice.isError));
    } else if (openAiTierState.signedIn) {
      const accountName = formatOpenAiTierAccountName(openAiTierState.account);
      setOpenAiTierStatus(`当前已登录：${accountName}`);
    } else {
      setOpenAiTierStatus("请先通过Entra ID账号登录，然后再查询 OpenAI Tier。", true);
    }

    if (openAiTierState.signedIn) {
      await loadOpenAiTierSubscriptions(false);
      loadOpenAiTierCachedResult();
    }
  } finally {
    openAiTierState.loadingAuthStatus = false;
    updateOpenAiTierAuthButtons();
  }
}

function updateOpenAiTierAuthButtons() {
  const isBusy = openAiTierState.loadingAuthStatus;
  openAiTierLoginBtn.disabled = isBusy || !openAiTierState.configured || openAiTierState.signedIn;
  if (openAiTierLogoutBtn) {
    openAiTierLogoutBtn.disabled = isBusy || !openAiTierState.signedIn;
  }
  if (openAiTierRefreshSubsBtn) {
    openAiTierRefreshSubsBtn.disabled = isBusy || !openAiTierState.signedIn;
  }
  if (openAiTierQueryBtn) {
    openAiTierQueryBtn.disabled = isBusy || !openAiTierState.signedIn;
  }

  const inlineQueryBtn = document.getElementById("openAiTierInlineQueryBtn");
  if (inlineQueryBtn) {
    inlineQueryBtn.disabled = isBusy || !openAiTierState.signedIn;
  }

  if (openAiTierDownloadBtn) {
    openAiTierDownloadBtn.disabled = isBusy || openAiTierState.resultRows.length === 0;
  }
}

async function startOpenAiTierLogin() {
  if (openAiTierState.loadingAuthStatus) {
    throw new Error("正在检查服务端登录配置，请稍后重试");
  }
  if (!openAiTierState.configured) {
    throw new Error("服务端尚未配置 Microsoft 登录参数");
  }
  if (openAiTierState.signedIn) {
    return;
  }
  if (!openAiTierState.loginUrlApi) {
    throw new Error("未获取到 Microsoft 登录入口");
  }

  setOpenAiTierStatus("即将跳转到 Microsoft 登录页...", false, true);
  const baseUrl = openAiTierState.loginUrlApi;
  const separator = baseUrl.includes("?") ? "&" : "?";
  const response = await fetch(`${baseUrl}${separator}source=openai-tier`);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.message || "生成 Microsoft 登录地址失败");
  }

  const authUrl = String(data.authUrl || "").trim();
  if (!authUrl) {
    throw new Error("Microsoft 登录地址为空");
  }

  window.location.href = authUrl;
}

async function loadOpenAiTierSubscriptions(showLoading) {
  if (!openAiTierState.signedIn) {
    return;
  }

  if (showLoading) {
    setOpenAiTierStatus("正在加载订阅列表...", false, true);
  }

  const response = await fetch("/api/openai-tier/subscriptions");
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.message || "加载订阅列表失败");
  }

  openAiTierState.subscriptions = Array.isArray(data.subscriptions) ? data.subscriptions : [];
  openAiTierState.selectedSubscriptionIds = new Set(
    openAiTierState.subscriptions.map((item) => String(item.subscriptionId || "").trim()).filter(Boolean)
  );
  openAiTierState.subscriptionDropdownOpen = false;
  openAiTierState.subscriptionSearch = "";

  renderOpenAiTierSummary();
  loadOpenAiTierCachedResult();
  if (openAiTierState.signedIn) {
    setOpenAiTierStatus("");
  }
}

function setAllOpenAiTierSubscriptionsSelected(checked) {
  const targetSubscriptions = checked
    ? getFilteredOpenAiTierSubscriptions()
    : (Array.isArray(openAiTierState.subscriptions) ? openAiTierState.subscriptions : []);
  if (checked) {
    targetSubscriptions.forEach((item) => {
      const subscriptionId = String(item.subscriptionId || "").trim();
      if (subscriptionId) {
        openAiTierState.selectedSubscriptionIds.add(subscriptionId);
      }
    });
  } else {
    targetSubscriptions.forEach((item) => {
      const subscriptionId = String(item.subscriptionId || "").trim();
      if (subscriptionId) {
        openAiTierState.selectedSubscriptionIds.delete(subscriptionId);
      }
    });
  }

  openAiTierState.subscriptionDropdownOpen = true;
  renderOpenAiTierSummary();
}

async function runOpenAiTierQuery() {
  if (!openAiTierState.signedIn) {
    setOpenAiTierStatus("请先登录后再查询。", true);
    return;
  }

  const selectedIds = Array.from(openAiTierState.selectedSubscriptionIds);
  if (selectedIds.length === 0) {
    setOpenAiTierStatus("请至少勾选一个订阅后再查询。", true);
    return;
  }

  const queryButton = openAiTierQueryBtn || document.getElementById("openAiTierInlineQueryBtn");
  const originalText = queryButton ? queryButton.textContent : "";
  try {
    if (queryButton) {
      queryButton.disabled = true;
      queryButton.textContent = "查询中...";
    }
    setOpenAiTierStatus("正在查询 OpenAI Tier 信息...", false, true);

    const response = await fetch("/api/openai-tier/query", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        subscriptionIds: selectedIds
      })
    });

    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.message || "查询 OpenAI Tier 失败");
    }

    openAiTierState.resultRows = Array.isArray(data.results) ? data.results : [];
    const cachedAt = new Date().toISOString();
    const cacheKey = getOpenAiTierCacheKey(selectedIds);
    localStorage.setItem(cacheKey, JSON.stringify({ ...data, cachedAt }));
    renderOpenAiTierTable(openAiTierState.resultRows);
    const summary = data.summary || {};
    const warnings = Array.isArray(data.warnings) ? data.warnings : [];
    const okCount = Number(summary.okCount || 0);
    const failedCount = Number(summary.failedCount || 0);
    const cachedAtText = formatOpenAiTierCacheTime(cachedAt);
    renderOpenAiTierSummary(summary);
    if (warnings.length > 0 || failedCount > 0) {
      const warningPreview = warnings.slice(0, 3).join("；");
      const warningSuffix = warnings.length > 3 ? "..." : "";
      const message = warningPreview
        ? `查询完成，成功 ${okCount} 个，未使用 ${failedCount} 个：${warningPreview}${warningSuffix}`
        : `查询完成，成功 ${okCount} 个，未使用 ${failedCount} 个。`;
      setOpenAiTierStatus(`${message} 缓存时间：${cachedAtText}`, true);
    } else {
      setOpenAiTierStatus(`查询完成，共返回 ${openAiTierState.resultRows.length} 条订阅结果。缓存时间：${cachedAtText}`);
    }
  } catch (error) {
    setOpenAiTierStatus(error.message || "查询 OpenAI Tier 失败", true);
  } finally {
    if (queryButton) {
      queryButton.disabled = !openAiTierState.signedIn;
      queryButton.textContent = originalText;
    }
  }
}

function getOpenAiTierCacheKey(subscriptionIds = Array.from(openAiTierState.selectedSubscriptionIds)) {
  return `${OPENAI_TIER_CACHE_KEY}-${subscriptionIds.slice().sort().join(",")}`;
}

function loadOpenAiTierCachedResult() {
  if (!openAiTierState.signedIn || openAiTierState.selectedSubscriptionIds.size === 0) {
    return false;
  }

  const cacheKey = getOpenAiTierCacheKey();
  const cached = localStorage.getItem(cacheKey);
  if (!cached) {
    return false;
  }

  try {
    const data = JSON.parse(cached);
    openAiTierState.resultRows = Array.isArray(data.results) ? data.results : [];
    renderOpenAiTierTable(openAiTierState.resultRows);
    renderOpenAiTierSummary(data.summary || {});
    setOpenAiTierStatus(`已加载本地缓存结果，缓存时间：${formatOpenAiTierCacheTime(data.cachedAt)}`);
    return true;
  } catch (error) {
    localStorage.removeItem(cacheKey);
    return false;
  }
}

function renderOpenAiTierSummary(summary = {}) {
  if (!openAiTierSummaryGridEl) {
    return;
  }

  const loginText = openAiTierState.signedIn
    ? formatOpenAiTierAccountName(openAiTierState.account)
    : (openAiTierState.configured ? "未登录" : "未配置");

  const subscriptionCount = Number(summary.subscriptionCount || openAiTierState.subscriptions.length || 0);
  const selectedCount = openAiTierState.selectedSubscriptionIds.size;
  const dropdownDisabled = !openAiTierState.signedIn || subscriptionCount === 0;
  const selectedSubscriptions = getSelectedOpenAiTierSubscriptions();

  openAiTierSummaryGridEl.innerHTML = [
    summaryCard("登录账号：", loginText, "summary-card--range summary-card--login-account"),
    buildOpenAiTierSubscriptionSummaryCard(selectedSubscriptions, selectedCount, subscriptionCount, dropdownDisabled),
    buildOpenAiTierQueryActionCard()
  ].join("");
  syncOpenAiTierSummaryCardWidths();
}

function renderOpenAiTierTable(rows) {
  if (!openAiTierTableWrapEl) {
    return;
  }
  const filterPanelState = window.FilterPopupPortal?.capture(openAiTierTableWrapEl);

  if (!Array.isArray(rows) || rows.length === 0) {
    openAiTierTableWrapEl.innerHTML = '<div class="empty">暂无数据</div>';
    updateOpenAiTierAuthButtons();
    return;
  }

  const columns = [
    { key: "subscriptionName", label: "订阅名称" },
    { key: "subscriptionId", label: "订阅ID" },
    { key: "currentTierName", label: "当前层级", filterable: true },
    { key: "assignmentDate", label: "分配日期", filterable: false },
    { key: "tierUpgradePolicy", label: "升级策略", filterable: false },
    { key: "nextTierName", label: "下一层级", filterable: false },
    { key: "nextTierStatus", label: "下一层级状态", filterable: false },
    { key: "nextTierDate", label: "下一层级日期", filterable: false },
    { key: "queryStatus", label: "查询状态" }
  ];

  const filterOptionsByColumn = {
    currentTierName: syncOpenAiTierFilterColumnOptions("currentTierName", rows)
  };
  const filteredRows = applyOpenAiTierFilters(rows);
  const header = ["<th>No.</th>"]
    .concat(columns.map((column) => {
      if (!column.filterable) {
        return `<th>${escapeHtml(column.label)}</th>`;
      }
      return renderOpenAiTierFilterHeader(column.key, column.label, filterOptionsByColumn[column.key] || []);
    }))
    .join("");

  const body = filteredRows
    .map((row, index) => `
      <tr class="selectable-row${isOpenAiTierSix(row.currentTierName) ? " openai-tier-row--tier-6" : ""}">
        <td>${index + 1}</td>
        ${columns.map((column) => `<td>${escapeHtml(formatOpenAiTierCell(row[column.key]))}</td>`).join("")}
      </tr>
    `)
    .join("");

  const emptyRow = filteredRows.length === 0
    ? `<tr><td colspan="${columns.length + 1}" class="empty-cell">暂无数据</td></tr>`
    : "";

  openAiTierTableWrapEl.innerHTML = `
    <table class="zebra-table openai-tier-table">
      <thead>
        <tr>${header}</tr>
      </thead>
      <tbody>${body}${emptyRow}</tbody>
    </table>
  `;

  bindOpenAiTierFilterEvents();
  if (window.FilterPopupPortal) {
    window.FilterPopupPortal.clear(openAiTierTableWrapEl);
    const popup = openAiTierTableWrapEl.querySelector(".filter-popup[data-filter-popup]");
    if (popup) {
      window.FilterPopupPortal.mount(openAiTierTableWrapEl, filterPanelState || {});
    }
  }
  updateOpenAiTierAuthButtons();
}

function syncOpenAiTierFilterColumnOptions(columnKey, rows) {
  const options = getOpenAiTierFilterOptions(columnKey, rows);
  const columnState = openAiTierTableFilters[columnKey];
  if (!columnState) {
    return options;
  }

  if (!columnState.initialized) {
    columnState.selectedValues = [];
    columnState.initialized = true;
    return options;
  }

  columnState.selectedValues = columnState.selectedValues.filter((value) => options.includes(value));
  return options;
}

function getOpenAiTierFilterOptions(columnKey, rows) {
  return Array.from(new Set(rows.map((row) => formatOpenAiTierCell(row[columnKey])))).sort((a, b) => a.localeCompare(b));
}

function applyOpenAiTierFilters(rows) {
  return rows.filter((row) => {
    return ["currentTierName"].every((columnKey) => {
      const columnState = openAiTierTableFilters[columnKey];
      const selectedSet = new Set(columnState.selectedValues);
      if (selectedSet.size === 0) {
        return true;
      }

      return selectedSet.has(formatOpenAiTierCell(row[columnKey]));
    });
  });
}

function renderOpenAiTierFilterHeader(columnKey, label, options) {
  const columnState = openAiTierTableFilters[columnKey];
  const selectedSet = new Set(columnState.selectedValues);
  const isOpen = openAiTierTableFilters.openColumn === columnKey;
  const isFiltered = columnState.selectedValues.length > 0 && columnState.selectedValues.length !== options.length;
  const searchKeyword = (columnState.search || "").trim().toLowerCase();
  const visibleOptions = options.filter((option) => option.toLowerCase().includes(searchKeyword));

  const optionList = visibleOptions.length > 0
    ? visibleOptions.map((option) => {
      const encodedValue = encodeURIComponent(option);
      const checked = selectedSet.has(option) ? "checked" : "";
      return `<label class="filter-option-item"><input type="checkbox" data-openai-tier-action="toggle-option" data-column="${columnKey}" data-value="${encodedValue}" ${checked} />${escapeHtml(option)}</label>`;
    }).join("")
    : '<div class="filter-empty">无匹配项</div>';

  const panel = isOpen
    ? `
      <div class="filter-popup" data-filter-popup="${columnKey}">
        <input type="text" class="filter-search" data-openai-tier-action="search" data-column="${columnKey}" value="${escapeAttr(columnState.search)}" placeholder="搜索..." />
        <div class="filter-actions">
          <button type="button" class="filter-link" data-openai-tier-action="select-all" data-column="${columnKey}">全选</button>
          <button type="button" class="filter-link filter-link-danger" data-openai-tier-action="clear-all" data-column="${columnKey}">清除</button>
        </div>
        <div class="filter-options">${optionList}</div>
      </div>
    `
    : "";

  return `
    <th class="filterable-th ${columnKey === "subscriptionId" ? "subscription-id-header" : ""}">
      <div class="filter-head-row">
        <span>${escapeHtml(label)}</span>
        <button type="button" class="filter-toggle ${isFiltered ? "filtered" : ""}" data-openai-tier-action="toggle-filter" data-column="${columnKey}">▼</button>
      </div>
      ${panel}
    </th>
  `;
}

function bindOpenAiTierFilterEvents() {
  if (openAiTierFilterEventsBound || !openAiTierTableWrapEl) {
    return;
  }

  openAiTierTableWrapEl.addEventListener("click", (event) => {
    const toggleBtn = event.target.closest('[data-openai-tier-action="toggle-filter"]');
    if (toggleBtn) {
      event.stopPropagation();
      const columnKey = toggleBtn.getAttribute("data-column") || "";
      openAiTierTableFilters.openColumn = openAiTierTableFilters.openColumn === columnKey ? "" : columnKey;
      renderOpenAiTierTable(openAiTierState.resultRows);
      return;
    }

    const actionBtn = event.target.closest('[data-openai-tier-action="select-all"], [data-openai-tier-action="clear-all"]');
    if (!actionBtn) {
      return;
    }

    event.stopPropagation();
    const columnKey = actionBtn.getAttribute("data-column") || "";
    const columnState = openAiTierTableFilters[columnKey];
    if (!columnState) {
      return;
    }

    const options = getOpenAiTierFilterOptions(columnKey, openAiTierState.resultRows);
    if (actionBtn.getAttribute("data-openai-tier-action") === "select-all") {
      columnState.selectedValues = [...options];
    } else {
      columnState.selectedValues = [];
      columnState.search = "";
    }
    renderOpenAiTierTable(openAiTierState.resultRows);
  });

  openAiTierTableWrapEl.addEventListener("input", (event) => {
    const searchInput = event.target.closest('input[data-openai-tier-action="search"]');
    if (!searchInput) {
      return;
    }

    const columnKey = searchInput.getAttribute("data-column") || "";
    const columnState = openAiTierTableFilters[columnKey];
    if (!columnState) {
      return;
    }

    columnState.search = searchInput.value || "";
    const caretPos = searchInput.selectionStart || (searchInput.value || "").length;
    renderOpenAiTierTable(openAiTierState.resultRows);

    const nextInput = openAiTierTableWrapEl.querySelector(`input[data-openai-tier-action="search"][data-column="${columnKey}"]`);
    if (nextInput) {
      nextInput.focus();
      nextInput.setSelectionRange(caretPos, caretPos);
    }
  });

  openAiTierTableWrapEl.addEventListener("change", (event) => {
    const checkbox = event.target.closest('input[data-openai-tier-action="toggle-option"]');
    if (!checkbox) {
      return;
    }

    const columnKey = checkbox.getAttribute("data-column") || "";
    const columnState = openAiTierTableFilters[columnKey];
    if (!columnState) {
      return;
    }

    const optionValue = decodeURIComponent(checkbox.getAttribute("data-value") || "");
    const selectedSet = new Set(columnState.selectedValues);
    if (checkbox.checked) {
      selectedSet.add(optionValue);
    } else {
      selectedSet.delete(optionValue);
    }
    columnState.selectedValues = Array.from(selectedSet);
    renderOpenAiTierTable(openAiTierState.resultRows);
  });

  document.addEventListener("click", (event) => {
    if (!openAiTierTableFilters.openColumn || !openAiTierTableWrapEl) {
      return;
    }
    if (openAiTierTableWrapEl.contains(event.target) || window.FilterPopupPortal?.contains(openAiTierTableWrapEl, event.target)) {
      return;
    }
    openAiTierTableFilters.openColumn = "";
    renderOpenAiTierTable(openAiTierState.resultRows);
  });

  openAiTierFilterEventsBound = true;
}

function formatOpenAiTierCell(value) {
  if (value === undefined || value === null || value === "") {
    return "-";
  }
  return String(value);
}

function exportOpenAiTierCsv() {
  const rows = Array.isArray(openAiTierState.resultRows) ? openAiTierState.resultRows : [];
  if (rows.length === 0) {
    setOpenAiTierStatus("暂无可下载的 Tier 查询结果，请先执行查询。", true);
    return;
  }

  const columns = [
    { key: "subscriptionName", label: "Subscription Name" },
    { key: "subscriptionId", label: "Subscription ID" },
    { key: "currentTierName", label: "Current Tier Name" },
    { key: "assignmentDate", label: "Assignment Date" },
    { key: "tierUpgradePolicy", label: "Tier Upgrade Policy" },
    { key: "nextTierName", label: "Next Tier Name" },
    { key: "nextTierStatus", label: "Next Tier Status" },
    { key: "nextTierDate", label: "Next Tier Date" },
    { key: "queryStatus", label: "Query Status" }
  ];

  const header = columns.map((column) => openAiTierCsvEscape(rowText(column.label))).join(",");
  const body = rows
    .map((row) => columns.map((column) => openAiTierCsvEscape(rowText(row[column.key]))).join(","))
    .join("\n");
  const csvContent = `${header}\n${body}`;

  const blob = new Blob([`\uFEFF${csvContent}`], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `openai-tier-${formatOpenAiTierFileTimestamp()}.csv`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function openAiTierCsvEscape(value) {
  return `"${String(value).replace(/"/g, '""')}"`;
}

function rowText(value) {
  if (value === undefined || value === null) {
    return "";
  }
  return String(value);
}

function formatOpenAiTierFileTimestamp(date = new Date()) {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  const hh = String(date.getHours()).padStart(2, "0");
  const mm = String(date.getMinutes()).padStart(2, "0");
  return `${y}${m}${d}${hh}${mm}`;
}

function formatOpenAiTierCacheTime(value) {
  const date = new Date(value || Date.now());
  if (Number.isNaN(date.getTime())) {
    return "未知";
  }

  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  const hours = String(date.getHours()).padStart(2, "0");
  const minutes = String(date.getMinutes()).padStart(2, "0");
  const seconds = String(date.getSeconds()).padStart(2, "0");
  return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}`;
}

function setOpenAiTierStatus(text, isError = false, isLoading = false) {
  if (!openAiTierStatusEl) {
    return;
  }

  openAiTierStatusEl.classList.toggle("error", isError);
  openAiTierStatusEl.classList.toggle("loading", isLoading);
  if (isLoading) {
    openAiTierStatusEl.innerHTML = `<span class="loading-dots" aria-hidden="true"><span></span><span></span><span></span></span><span>${escapeHtml(text)}</span>`;
  } else {
    openAiTierStatusEl.textContent = text;
  }
}

function formatOpenAiTierAccountName(account) {
  if (!account || typeof account !== "object") {
    return "-";
  }

  const name = String(account.name || "").trim();
  const username = String(account.preferredUsername || "").trim();
  return username || name || "-";
}

function isOpenAiTierSix(tierName) {
  return /^tier\s*6$/i.test(String(tierName || "").trim());
}

function syncOpenAiTierSummaryCardWidths() {
  if (!openAiTierSummaryGridEl) {
    return;
  }

  const view = openAiTierSummaryGridEl.closest('[data-app-view="openai-tier"]');
  if (view?.hidden || openAiTierSummaryGridEl.getClientRects().length === 0) {
    return;
  }

  const loginCard = openAiTierSummaryGridEl.querySelector(".summary-card--login-account");
  const subscriptionCard = openAiTierSummaryGridEl.querySelector(".summary-card--subscription-select");
  if (!loginCard || !subscriptionCard) {
    return;
  }

  const loginLabel = loginCard.querySelector(".k");
  const loginValue = loginCard.querySelector(".v");
  const subscriptionLabel = subscriptionCard.querySelector(".k");
  const selectedCount = subscriptionCard.querySelector(".tier-selected-count");
  const trigger = subscriptionCard.querySelector(".tier-summary-trigger");
  const loginWidth = (loginLabel?.scrollWidth || 0) + (loginValue?.scrollWidth || 0) + 30;
  const subscriptionWidth = (subscriptionLabel?.scrollWidth || 0)
    + (selectedCount?.scrollWidth || 0)
    + (trigger?.offsetWidth || 0)
    + 38;
  const minimumWidth = Math.ceil(Math.max(loginWidth, subscriptionWidth));

  if (minimumWidth > 0) {
    openAiTierSummaryGridEl.style.setProperty("--openai-tier-summary-card-min-width", `${minimumWidth}px`);
  }
}

function buildOpenAiTierSubscriptionSummaryCard(selectedSubscriptions, selectedCount, subscriptionCount, disabled) {
  const dropdownContent = buildOpenAiTierDropdownContent();
  const expanded = openAiTierState.subscriptionDropdownOpen ? "true" : "false";
  const disabledAttr = disabled ? "disabled" : "";
  const countText = openAiTierState.signedIn
    ? `${selectedCount} / ${subscriptionCount} 个订阅`
    : "";

  return `
    <div class="summary-card summary-card--range summary-card--subscription-select">
      <div class="k">已选订阅：</div>
      <div class="v tier-summary-inline">
        <div class="tier-selected-count">${escapeHtml(countText)}</div>
        <div class="tier-summary-dropdown">
          <button
            type="button"
            class="tier-summary-trigger"
            data-action="toggle-subscription-dropdown"
            aria-expanded="${expanded}"
            ${disabledAttr}
            title="选择订阅"
          ></button>
          ${dropdownContent}
        </div>
      </div>
    </div>
  `;
}

function buildOpenAiTierDropdownContent() {
  if (!openAiTierState.subscriptionDropdownOpen) {
    return "";
  }

  if (!openAiTierState.signedIn) {
    return '<div class="tier-summary-menu"><div class="tier-picker-meta">请先登录后加载订阅列表</div></div>';
  }

  if (!Array.isArray(openAiTierState.subscriptions) || openAiTierState.subscriptions.length === 0) {
    return '<div class="tier-summary-menu"><div class="tier-picker-meta">当前账号下没有可查询的 Enabled 订阅</div></div>';
  }

  const selectedCount = openAiTierState.selectedSubscriptionIds.size;
  const filteredSubscriptions = getFilteredOpenAiTierSubscriptions();
  const items = filteredSubscriptions
    .map((item) => {
      const subscriptionId = String(item.subscriptionId || "").trim();
      const checked = openAiTierState.selectedSubscriptionIds.has(subscriptionId) ? "checked" : "";
      const subscriptionName = String(item.subscriptionName || subscriptionId).trim();
      return `
        <label class="tier-subscription-option">
          <input type="checkbox" data-subscription-id="${escapeAttr(subscriptionId)}" ${checked} />
          <span class="tier-subscription-option-text">${escapeHtml(subscriptionName)} / ${escapeHtml(subscriptionId)}</span>
        </label>
      `;
    })
    .join("");
  const emptyState = filteredSubscriptions.length === 0
    ? '<div class="tier-picker-empty">没有匹配的订阅</div>'
    : "";

  return `
    <div class="tier-summary-menu">
      <input
        type="text"
        class="tier-picker-search"
        data-action="subscription-search"
        value="${escapeAttr(openAiTierState.subscriptionSearch)}"
        placeholder="搜索订阅..."
      />
      <div class="tier-picker-toolbar">
        <div class="tier-picker-meta">已选择 ${selectedCount} / ${openAiTierState.subscriptions.length} 个订阅</div>
        <div class="tier-picker-actions">
          <button type="button" class="tier-picker-action" data-picker-action="select-all">全选</button>
          <button type="button" class="tier-picker-action" data-picker-action="clear-all">清空</button>
        </div>
      </div>
      <div class="tier-subscription-grid">${items}${emptyState}</div>
    </div>
  `;
}

function buildOpenAiTierQueryActionCard() {
  const disabledAttr = openAiTierState.signedIn ? "" : "disabled";
  return `
    <div class="summary-card summary-card--range summary-card--query-action">
      <button id="openAiTierInlineQueryBtn" type="button" class="tier-inline-query-btn" ${disabledAttr}>查询 Tier</button>
    </div>
  `;
}

function getSelectedOpenAiTierSubscriptions() {
  if (!Array.isArray(openAiTierState.subscriptions) || openAiTierState.subscriptions.length === 0) {
    return [];
  }

  return openAiTierState.subscriptions.filter((item) =>
    openAiTierState.selectedSubscriptionIds.has(String(item.subscriptionId || "").trim())
  );
}

function getFilteredOpenAiTierSubscriptions() {
  if (!Array.isArray(openAiTierState.subscriptions)) {
    return [];
  }

  const keyword = String(openAiTierState.subscriptionSearch || "").trim().toLowerCase();
  if (!keyword) {
    return openAiTierState.subscriptions;
  }

  return openAiTierState.subscriptions.filter((item) => {
    const subscriptionName = String(item.subscriptionName || "").toLowerCase();
    const subscriptionId = String(item.subscriptionId || "").toLowerCase();
    return subscriptionName.includes(keyword) || subscriptionId.includes(keyword);
  });
}
