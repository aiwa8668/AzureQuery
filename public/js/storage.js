const STORAGE_KEY = "azure-cost-analyzer-config-v1";
const AUTH_CONFIGS_KEY = "azure-cost-analyzer-auth-configs-v1";

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
