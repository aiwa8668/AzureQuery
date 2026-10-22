const STORAGE_KEY = "azure-cost-analyzer-config-v1";
const AUTH_CONFIGS_KEY = "azure-cost-analyzer-auth-configs-v1";

if (typeof window.escapeHtml !== "function") {
  window.escapeHtml = function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/\"/g, "&quot;")
      .replace(/'/g, "&#39;");
  };
}

if (typeof window.escapeAttr !== "function") {
  window.escapeAttr = function escapeAttr(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/\"/g, "&quot;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  };
}

if (typeof window.summaryCard !== "function") {
  window.summaryCard = function summaryCard(k, v, extraClass = "") {
    const className = extraClass ? `summary-card ${extraClass}` : "summary-card";
    return `<div class="${className}"><div class="k">${window.escapeHtml(k)}</div><div class="v">${window.escapeHtml(v)}</div></div>`;
  };
}

function clearConfig() {
  localStorage.removeItem(STORAGE_KEY);
}

function loadAuthConfigs() {
  try {
    const raw = localStorage.getItem(AUTH_CONFIGS_KEY);
    if (!raw) {
      return [];
    }
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) {
      return parsed;
    }
    return [];
  } catch (error) {
    console.error("读取认证配置失败", error);
    return [];
  }
}

function saveAuthConfigs(authConfigs) {
  localStorage.setItem(AUTH_CONFIGS_KEY, JSON.stringify(Array.isArray(authConfigs) ? authConfigs : []));
}

function clearAuthConfigs() {
  localStorage.removeItem(AUTH_CONFIGS_KEY);
}
