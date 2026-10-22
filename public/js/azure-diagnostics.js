(() => {
  const el = (name) => document.getElementById(`diagnostics${name}`);
  const state = { signedIn: false, configured: false, busy: false, loading: false, workspaceLoading: false, resourceLoading: false, diagnosticLoading: false, subscriptionLoading: false, rows: [], resources: [], version: 0 };
  const diagnosticRequests = new Set();
  let cleanupTableScroll = () => {};
  const baseColumns = [
    ["TimeGenerated", "调用时间"], ["RequestId", "请求 ID"], ["OperationName", "操作"],
    ["Deployment", "部署"], ["Model", "模型"], ["ModelVersion", "模型版本"],
    ["InputTokens", "输入 Token"], ["CachedInputTokens", "缓存命中输入"],
    ["UncachedInputTokens", "未缓存输入"], ["CacheWriteTokens", "缓存写入"], ["OutputTokens", "输出 Token"],
    ["LongContextPromptTokens", "长上下文输入"], ["LongContextCachedTokens", "长上下文缓存命中"],
    ["LongContextCacheWriteTokens", "长上下文缓存写入"], ["LongContextGeneratedTokens", "长上下文输出"],
  ];
  const fieldLabels = Object.fromEntries([...baseColumns,
    ["ResourceId", "资源 ID"], ["_ResourceId", "完整资源 ID"], ["Category", "日志分类"],
    ["ResourceGroup", "资源组"], ["SubscriptionId", "订阅 ID"], ["_SubscriptionId", "订阅标识"],
    ["ResourceProvider", "资源提供程序"], ["Resource", "资源名称"], ["ResourceType", "资源类型"],
    ["CorrelationId", "关联 ID"], ["TenantId", "租户 ID"], ["Type", "日志表类型"],
    ["SourceSystem", "来源系统"], ["properties_s", "日志属性（原始 JSON）"],
    ["workspaceId", "工作区 ID"], ["PromptTokens", "原始输入 Token"], ["CachedTokens", "原始缓存命中 Token"],
    ["GeneratedTokens", "原始输出 Token"], ["OperationVersion", "操作版本"], ["ResultType", "结果类型"],
    ["ResultSignature", "结果签名"], ["ResultDescription", "结果描述"], ["DurationMs", "持续时间（毫秒）"],
    ["CallerIPAddress", "调用方 IP 地址"], ["Level", "日志级别"], ["Location", "位置"],
    ["_IsBillable", "是否计费"], ["_BilledSize", "计费数据大小"], ["identity_s", "身份信息"],
  ]);
  state.columns = [];
  state.selectedFields = new Set();
  state.knownFields = new Set();
  const detailFields = [
    "TimeGenerated", "RequestId", "OperationName", "CallerIPAddress", "DurationMs", "ResultSignature",
    "event_s", "threadID_d",
    "PromptTokens", "CachedTokens", "CacheWriteTokens", "GeneratedTokens",
    "LongContextPromptTokens", "LongContextCachedTokens", "LongContextCacheWriteTokens", "LongContextGeneratedTokens",
    "Model", "InputTokens", "OutputTokens", "CachedInputTokens", "UncachedInputTokens", "properties_s",
  ];
  const fieldLabel = (key) => fieldLabels[key] || key;
  const visibleColumns = () => state.columns.filter((key) => state.selectedFields.has(key));
  if (!el("Form")) return;

  function status(message, error = false) {
    el("Status").textContent = message;
    el("Status").classList.toggle("is-error", error);
  }

  function controls() {
    el("Login").disabled = !state.configured || state.signedIn || state.loading || state.busy;
    el("Logout").disabled = !state.signedIn || state.busy;
    el("Refresh").disabled = !state.signedIn || state.loading || state.busy;
    el("Subscription").disabled = !state.signedIn || state.subscriptionLoading || state.busy;
    el("Workspace").disabled = !state.signedIn || state.workspaceLoading || state.busy || !el("Workspace").value;
    el("Resource").disabled = !state.signedIn || state.resourceLoading || state.busy || !state.resources.length;
    el("Query").disabled = !state.signedIn || state.loading || state.busy || !el("Workspace").value || !el("Resource").value;
    el("Query").textContent = state.busy ? "查询中…" : "查询";
    el("Form").querySelectorAll("input, #diagnosticsTimezone").forEach((item) => { item.disabled = state.busy; });
  }

  async function api(url, requestOptions, isCurrent = () => true) {
    const response = await fetch(url, requestOptions);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 401 && isCurrent()) {
        state.signedIn = false;
        state.version += 1;
        state.loading = false;
        state.workspaceLoading = false;
        state.resourceLoading = false;
        state.subscriptionLoading = false;
        el("Account").textContent = "未登录";
        options("Subscription", [], null, null, "请重新登录");
        options("Workspace", [], null, null, "请重新登录");
        resetResources("请重新登录");
        clear();
        controls();
        status(data.message || "登录已过期，请重新登录。", true);
      }
      const error = new Error(data.message || `请求失败（HTTP ${response.status}）`);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function options(name, items, value, label, empty) {
    el(name).replaceChildren();
    if (!items.length) el(name).add(new Option(empty, ""));
    items.forEach((item) => {
      const option = new Option(label(item), value(item));
      if (name === "Resource") updateResourceOption(option, item);
      el(name).add(option);
    });
  }

  function clear() {
    cleanupTableScroll();
    state.rows = [];
    el("Count").textContent = "";
    el("Warning").textContent = "";
    el("Table").innerHTML = '<div class="empty">暂无数据</div>';
    el("Kql").hidden = true;
    el("QueryText").textContent = "";
    el("Download").disabled = true;
    el("FieldsToggle").disabled = true;
    el("FieldsPanel").hidden = true;
    el("FieldsToggle").setAttribute("aria-expanded", "false");
    status("");
  }

  function resetResources(message) {
    state.resources = [];
    state.diagnosticLoading = false;
    options("Resource", [], null, null, message);
    el("ResourceStatus").textContent = "";
    el("ResourceStatus").removeAttribute("data-log-state");
    el("ResourceId").textContent = "";
  }

  function resourceLabel(item) {
    const mark = item.diagnosticStatus === "pending" ? "… 检查中" : item.diagnosticStatus === "unknown" ? "? 状态未知" : item.logsEnabled ? "✅ 已启用日志" : "✗ 未启用日志";
    return `${mark}/${item.name}`;
  }

  function updateResourceOption(option, item) {
    const enabled = item.logsEnabled && !["pending", "unknown"].includes(item.diagnosticStatus);
    option.textContent = resourceLabel(item);
    option.style.color = enabled ? "#15803d" : "";
    option.style.fontWeight = enabled ? "700" : "";
  }

  function resourceChanged(autoSelect = true, reset = true) {
    const resource = state.resources.find((item) => item.id === el("Resource").value);
    if (!resource) return;
    if (reset) clear();
    el("ResourceId").textContent = resource.id;
    el("ResourceStatus").dataset.logState = ["pending", "unknown"].includes(resource.diagnosticStatus) ? resource.diagnosticStatus : resource.logsEnabled ? "enabled" : "disabled";
    if (resource.diagnosticStatus === "pending") {
      el("ResourceStatus").textContent = "日志诊断状态检查中，可先选择工作区并查询。";
    } else if (resource.diagnosticStatus === "unknown") {
      el("ResourceStatus").textContent = resource.diagnosticError
        ? `日志诊断状态检查失败：${resource.diagnosticError}；不能判断为未启用，可继续查询或刷新重试。`
        : "日志诊断状态未知：当前账号无权限读取诊断设置，不能判断为未启用。";
    } else if (!resource.logsEnabled) {
      el("ResourceStatus").textContent = "未启用日志诊断：请在 Azure 门户为该资源启用日志并发送到 Log Analytics 工作区。";
    } else if (!resource.usageLogsEnabled) {
      el("ResourceStatus").textContent = "已启用日志诊断，但尚未启用 AzureOpenAIRequestUsage 调用用量日志。";
    } else {
      el("ResourceStatus").textContent = "已启用日志诊断及 AzureOpenAIRequestUsage 调用用量日志。";
    }
    const destinations = resource.workspaceIds || [];
    const matching = Array.from(el("Workspace").options).find((option) => destinations.some((id) => id.toLowerCase() === option.value.toLowerCase()));
    if (matching && autoSelect && !state.busy) el("Workspace").value = matching.value;
    if (resource.logsEnabled) {
      if (!destinations.length) el("ResourceStatus").textContent += " 未配置日志的 Log Analytics 目标，可能仅发送至存储或 Event Hub。";
      else if (!matching) el("ResourceStatus").textContent += " 日志目标工作区不在当前订阅可见列表中，请检查工作区所属订阅及访问权限。";
      else if (!destinations.some((id) => id.toLowerCase() === el("Workspace").value.toLowerCase())) el("ResourceStatus").textContent += " 当前工作区不是该资源配置的诊断日志目标。";
    }
    controls();
  }

  async function diagnostics(query, version) {
    const current = () => version === state.version && state.signedIn;
    const batches = [];
    for (let index = 0; index < state.resources.length; index += 20) batches.push(state.resources.slice(index, index + 20));
    state.diagnosticLoading = Boolean(batches.length);
    const worker = async () => {
      while (current() && batches.length) {
        while (current() && diagnosticRequests.size >= 2) await Promise.race(diagnosticRequests);
        if (!current()) return;
        const batch = batches.shift();
        if (!batch) return;
        const params = new URLSearchParams(query);
        params.set("phase", "diagnostics");
        batch.forEach((item) => params.append("resourceIds", item.id));
        const request = api(`/api/azure-diagnostics/resources?${params}`, undefined, current).then((data) => {
          if (!current()) return;
          const updates = new Map((Array.isArray(data.resources) ? data.resources : []).filter((item) => typeof item.id === "string").map((item) => [item.id.toLowerCase(), item]));
          batch.forEach((item) => {
            const update = updates.get(item.id.toLowerCase());
            if (update && ["known", "unknown"].includes(update.diagnosticStatus)) Object.assign(item, update, { id: item.id });
            else Object.assign(item, { diagnosticStatus: "unknown", diagnosticError: "服务端未返回该资源的诊断状态" });
          });
        }).catch((error) => {
          if (!current()) return;
          batch.forEach((item) => Object.assign(item, { diagnosticStatus: "unknown", diagnosticError: error.message }));
        }).finally(() => {
          diagnosticRequests.delete(request);
          if (!current()) return;
          const resources = new Map(state.resources.map((item) => [item.id, item]));
          Array.from(el("Resource").options).forEach((option) => { if (resources.has(option.value)) updateResourceOption(option, resources.get(option.value)); });
          resourceChanged(!state.workspaceTouched, false);
        });
        diagnosticRequests.add(request);
        await request;
      }
    };
    try {
      await Promise.all([worker(), worker()]);
    } finally {
      if (current()) { state.diagnosticLoading = false; controls(); }
    }
  }

  async function workspaces() {
    const version = ++state.version;
    const sameSubscription = state.resourceSubscription === el("Subscription").value;
    const selectedWorkspace = sameSubscription ? el("Workspace").value : "";
    const selectedResource = sameSubscription ? el("Resource").value : "";
    state.resourceSubscription = el("Subscription").value;
    if (!sameSubscription) state.workspaceTouched = false;
    clear();
    options("Workspace", [], null, null, "正在加载工作区…");
    resetResources("正在加载 Foundry 资源…");
    status("正在加载工作区及 Foundry 资源列表…");
    state.loading = true;
    state.workspaceLoading = true;
    state.resourceLoading = true;
    controls();
    try {
      if (!el("Subscription").value) {
        options("Workspace", [], null, null, "没有可用订阅");
        resetResources("没有可用订阅");
        return;
      }
      const query = `subscriptionId=${encodeURIComponent(el("Subscription").value)}`;
      const errors = [];
      const current = () => version === state.version && state.signedIn;
      await Promise.allSettled([
        api(`/api/azure-diagnostics/workspaces?${query}`, undefined, current).then((data) => {
          if (!current()) return;
          options("Workspace", data.workspaces || [], (item) => item.id, (item) => `${item.name} (${item.location || "未知区域"})`, "未发现工作区");
          if (Array.from(el("Workspace").options).some((item) => item.value === selectedWorkspace)) el("Workspace").value = selectedWorkspace;
          if (!el("Workspace").value) errors.push("未发现 Log Analytics 工作区");
          resourceChanged(!state.workspaceTouched, false);
        }).catch((error) => {
          if (!current()) return;
          options("Workspace", [], null, null, "工作区加载失败");
          errors.push(error.message);
        }).finally(() => { if (current()) { state.workspaceLoading = false; controls(); } }),
        api(`/api/azure-diagnostics/resources?${query}&phase=accounts`, undefined, current).then((data) => {
          if (!current()) return;
          state.resources = Array.isArray(data.resources) ? data.resources : [];
          options("Resource", state.resources, (item) => item.id, resourceLabel, "未发现 Foundry / Azure OpenAI 资源");
          if (state.resources.some((item) => item.id === selectedResource)) el("Resource").value = selectedResource;
          resourceChanged(!state.workspaceTouched, false);
          if (!state.resources.length) errors.push("所选订阅未发现 Foundry / Azure OpenAI 资源");
          void diagnostics(query, version);
        }).catch((error) => {
          if (!current()) return;
          resetResources("Foundry 资源加载失败");
          errors.push(error.message);
        }).finally(() => { if (current()) { state.resourceLoading = false; controls(); } }),
      ]);
      if (!current()) return;
      status(errors.length ? errors.join("；") : "请选择 Foundry 资源及日志工作区后查询。", Boolean(errors.length));
    } catch (error) {
      if (version !== state.version || !state.signedIn) return;
      options("Workspace", [], null, null, "工作区加载失败");
      status(error.message, true);
    } finally {
      if (version === state.version) { state.loading = false; state.workspaceLoading = false; state.resourceLoading = false; controls(); }
    }
  }

  async function subscriptions() {
    clear();
    const selected = el("Subscription").value;
    if (!selected) {
      options("Subscription", [], null, null, "正在加载订阅…");
      options("Workspace", [], null, null, "请先选择订阅");
      resetResources("请先选择订阅");
    }
    state.loading = true;
    state.subscriptionLoading = true;
    controls();
    const requestVersion = (state.subscriptionVersion || 0) + 1;
    state.subscriptionVersion = requestVersion;
    let version;
    const current = () => requestVersion === state.subscriptionVersion && version === state.version && state.signedIn;
    try {
      const listsRequest = selected ? workspaces() : Promise.resolve();
      version = state.version;
      const subscriptionRequest = api("/api/openai-tier/subscriptions", undefined, current);
      const data = await subscriptionRequest;
      if (!current()) return;
      options("Subscription", data.subscriptions || [], (item) => item.subscriptionId, (item) => `${item.subscriptionName} (${item.subscriptionId})`, "没有可用订阅");
      if (Array.from(el("Subscription").options).some((item) => item.value === selected)) el("Subscription").value = selected;
      state.subscriptionLoading = false;
      controls();
      await listsRequest;
      if (!state.signedIn) return;
      if (!selected || el("Subscription").value !== selected) await workspaces();
    } catch (error) {
      if (!current()) return;
      if (error.status !== 401) {
        state.version += 1;
        state.workspaceLoading = false;
        state.resourceLoading = false;
        options("Subscription", [], null, null, "订阅加载失败，请刷新重试");
        options("Workspace", [], null, null, "请先选择订阅");
        resetResources("请先选择订阅");
      }
      status(error.message, true);
    } finally {
      if (requestVersion === state.subscriptionVersion) {
        state.loading = false;
        state.subscriptionLoading = false;
        controls();
      }
    }
  }

  async function auth() {
    try {
      const data = await api("/api/openai-tier/auth/status");
      state.configured = Boolean(data.configured);
      state.signedIn = Boolean(data.signedIn);
      el("Account").textContent = data.account?.preferredUsername || data.account?.name || "未登录";
      controls();
      if (state.signedIn) await subscriptions();
      else status(state.configured ? "请先使用 Entra ID 账号登录。" : "服务端尚未配置 Microsoft 登录参数。", true);
    } catch (error) {
      status(error.message, true);
      if (error.status === 401) el("Login").disabled = false;
    }
  }

  function display(value, key) {
    if (value == null) return "—";
    if (key === "TimeGenerated") {
      const date = new Date(value);
      if (!Number.isNaN(date.getTime())) return date.toLocaleString("zh-CN", { timeZone: el("Timezone").value === "+8" ? "Asia/Shanghai" : "UTC", hour12: false });
    }
    return typeof value === "object" ? JSON.stringify(value) : String(value);
  }

  function updateColumns(data) {
    el("FieldsSearch").value = "";
    const names = (data.columns || []).map((item) => typeof item === "string" ? item : item.name).filter((key) => typeof key === "string" && key !== "p");
    state.rows.forEach((row) => names.push(...Object.keys(row).filter((key) => key !== "p")));
    const available = new Set(names);
    state.columns = detailFields.filter((key) => available.has(key));
    state.columns.forEach((key) => {
      if (!state.knownFields.has(key)) state.selectedFields.add(key);
      state.knownFields.add(key);
    });
    el("FieldsToggle").disabled = !state.columns.length;
    renderFieldList();
  }

  function renderFieldList() {
    const search = el("FieldsSearch").value.trim().toLowerCase();
    el("FieldsCount").textContent = `已选 ${visibleColumns().length} / ${state.columns.length} 个字段`;
    const fragment = document.createDocumentFragment();
    state.columns.filter((key) => `${fieldLabel(key)} ${key}`.toLowerCase().includes(search)).forEach((key) => {
      const label = document.createElement("label");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.dataset.field = key;
      input.checked = state.selectedFields.has(key);
      const text = document.createElement("span");
      text.textContent = fieldLabel(key);
      text.title = key;
      label.append(input, text);
      fragment.append(label);
    });
    el("FieldsList").replaceChildren(fragment);
    if (!el("FieldsList").children.length) el("FieldsList").textContent = "没有匹配的字段";
  }

  function setupTableScroll(table) {
    const wrapper = el("Table");
    const scrollbar = el("TableScroll");
    const spacer = el("TableScrollSpacer");
    let active = true;
    const syncTable = () => { if (wrapper.scrollLeft !== scrollbar.scrollLeft) wrapper.scrollLeft = scrollbar.scrollLeft; };
    const syncScrollbar = () => { if (scrollbar.scrollLeft !== wrapper.scrollLeft) scrollbar.scrollLeft = wrapper.scrollLeft; };
    const updateWidth = () => {
      if (!active) return;
      spacer.style.width = `${wrapper.scrollWidth}px`;
      scrollbar.hidden = wrapper.clientWidth === 0 || wrapper.scrollWidth <= wrapper.clientWidth;
      syncScrollbar();
    };
    scrollbar.addEventListener("scroll", syncTable);
    wrapper.addEventListener("scroll", syncScrollbar);
    const observer = new ResizeObserver(updateWidth);
    observer.observe(wrapper);
    observer.observe(table);
    cleanupTableScroll = () => {
      active = false;
      observer.disconnect();
      scrollbar.removeEventListener("scroll", syncTable);
      wrapper.removeEventListener("scroll", syncScrollbar);
      scrollbar.hidden = true;
      spacer.style.width = "0px";
      scrollbar.scrollLeft = 0;
      wrapper.scrollLeft = 0;
      cleanupTableScroll = () => {};
    };
    updateWidth();
  }

  function render() {
    cleanupTableScroll();
    el("Count").textContent = `（${state.rows.length} 条）`;
    const columns = visibleColumns();
    el("Download").disabled = !state.rows.length || !columns.length;
    if (!state.rows.length) {
      el("Table").innerHTML = '<div class="empty">该范围内暂无调用日志，请检查资源 ID、日志分类、诊断设置及采集延迟。</div>';
      return;
    }
    if (!columns.length) {
      el("Table").innerHTML = state.columns.length
        ? '<div class="empty">未选择字段，请点击“筛选字段”勾选需要显示的列。</div>'
        : '<div class="empty">查询结果没有可用字段。</div>';
      return;
    }
    const table = document.createElement("table");
    const head = table.createTHead().insertRow();
    columns.forEach((key) => { const th = document.createElement("th"); th.textContent = fieldLabel(key); th.title = key; head.append(th); });
    const body = table.createTBody();
    state.rows.forEach((row) => {
      const tr = body.insertRow();
      columns.forEach((key) => { const td = tr.insertCell(); td.textContent = display(row[key], key); });
    });
    el("Table").replaceChildren(table);
    setupTableScroll(table);
  }

  el("Login").addEventListener("click", async () => {
    try {
      const data = await api("/api/openai-tier/auth/login-url?source=azure-diagnostics");
      if (!data.authUrl) throw new Error("生成登录地址失败");
      window.location.href = data.authUrl;
    } catch (error) { status(error.message, true); }
  });
  el("Logout").addEventListener("click", () => { state.version += 1; state.signedIn = false; window.location.href = "/auth/openai/logout?source=azure-diagnostics"; });
  el("Refresh").addEventListener("click", subscriptions);
  el("FieldsToggle").addEventListener("click", () => {
    el("FieldsPanel").hidden = !el("FieldsPanel").hidden;
    el("FieldsToggle").setAttribute("aria-expanded", String(!el("FieldsPanel").hidden));
    if (!el("FieldsPanel").hidden) el("FieldsSearch").focus();
  });
  el("FieldsSearch").addEventListener("input", renderFieldList);
  el("FieldsList").addEventListener("change", (event) => {
    const key = event.target.dataset.field;
    if (!key) return;
    if (event.target.checked) state.selectedFields.add(key);
    else state.selectedFields.delete(key);
    renderFieldList();
    render();
  });
  ["All", "None"].forEach((action) => el(`Fields${action}`).addEventListener("click", () => {
    state.columns.forEach((key) => action === "All" ? state.selectedFields.add(key) : state.selectedFields.delete(key));
    renderFieldList();
    render();
  }));
  function closeFieldPicker() {
    el("FieldsPanel").hidden = true;
    el("FieldsToggle").setAttribute("aria-expanded", "false");
  }
  document.addEventListener("click", (event) => { if (!el("FieldPicker").contains(event.target)) closeFieldPicker(); });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !el("FieldsPanel").hidden) { closeFieldPicker(); el("FieldsToggle").focus(); }
  });
  el("Subscription").addEventListener("change", workspaces);
  el("Resource").addEventListener("change", () => resourceChanged());
  el("Workspace").addEventListener("change", () => { state.workspaceTouched = true; resourceChanged(false); });
  ["Start", "End", "Timezone"].forEach((name) => el(name).addEventListener("change", clear));
  el("Form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.busy || state.loading || !state.signedIn) return;
    if (el("Start").value > el("End").value) { status("开始日期不能晚于结束日期。", true); return; }
    const version = state.version;
    const current = () => version === state.version && state.signedIn;
    state.workspaceTouched = true;
    state.busy = true;
    clear();
    controls();
    status("正在查询 AzureDiagnostics 调用日志…");
    try {
      const data = await api("/api/azure-diagnostics/query", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ subscriptionIds: [el("Subscription").value], workspaceIds: [el("Workspace").value], resourceIds: [el("Resource").value.trim()], startDate: el("Start").value, endDate: el("End").value, tz: el("Timezone").value }),
      }, current);
      if (!current()) return;
      state.rows = Array.isArray(data.rows) ? data.rows : [];
      updateColumns(data);
      el("Warning").textContent = (data.warnings || []).join(" ");
      el("QueryText").textContent = data.query || "";
      el("Kql").hidden = !data.query;
      render();
      status(`查询完成：${state.rows.length} 条调用日志${data.truncated ? "（结果已截断，请缩小范围）" : ""}。`);
    } catch (error) { if (current()) status(error.message, true); }
    finally { state.busy = false; controls(); }
  });
  el("Download").addEventListener("click", () => {
    const quote = (value) => {
      let text = String(value ?? "");
      if (/^[=+\-@\t\r]/.test(text)) text = `'${text}`;
      return `"${text.replace(/"/g, '""')}"`;
    };
    const columns = visibleColumns();
    if (!columns.length || !state.rows.length) return;
    const csv = [columns.map((key) => quote(fieldLabel(key))).join(","), ...state.rows.map((row) => columns.map((key) => quote(display(row[key], key))).join(","))].join("\r\n");
    const url = URL.createObjectURL(new Blob(["\uFEFF", csv], { type: "text/csv;charset=utf-8;" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `AzureDiagnostics_${el("Start").value}_${el("End").value}.csv`;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  const today = new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Shanghai" });
  ["Start", "End"].forEach((name) => { el(name).value = today; el(name).max = today; });
  auth();
})();
