const appViewButtons = Array.from(document.querySelectorAll("[data-view-target]"));
const appViews = Array.from(document.querySelectorAll("[data-app-view]"));
const APP_VIEW_STORAGE_KEY = "azure-query-workbench-active-view-v1";
const COST_MODE_STORAGE_KEY = "azure-query-workbench-cost-mode-v1";

initAppShell();

function initAppShell() {
  if (appViewButtons.length === 0 || appViews.length === 0) {
    return;
  }

  appViewButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const targetView = button.getAttribute("data-view-target");
      if (!targetView) {
        return;
      }

      const costMode = button.getAttribute("data-cost-mode") || getStoredCostMode();
      switchAppView(targetView, true, costMode);
    });
  });

  const initialView = resolveInitialView();
  const initialCostMode = resolveInitialCostMode();
  switchAppView(initialView, false, initialCostMode);
}

function resolveInitialView() {
  const params = new URLSearchParams(window.location.search);
  const queryView = params.get("view");
  if (queryView && hasAppView(queryView)) {
    localStorage.setItem(APP_VIEW_STORAGE_KEY, queryView);
    return queryView;
  }

  return "openai-tier";
}

function hasAppView(view) {
  return appViews.some((section) => section.getAttribute("data-app-view") === view);
}

function resolveInitialCostMode() {
  const params = new URLSearchParams(window.location.search);
  const queryMode = params.get("costMode");
  if (queryMode === "single" || queryMode === "multi") {
    localStorage.setItem(COST_MODE_STORAGE_KEY, queryMode);
    return queryMode;
  }

  const cachedMode = localStorage.getItem(COST_MODE_STORAGE_KEY);
  if (cachedMode === "single" || cachedMode === "multi") {
    return cachedMode;
  }

  return "multi";
}

function getStoredCostMode() {
  const storedMode = localStorage.getItem(COST_MODE_STORAGE_KEY);
  return storedMode === "single" ? "single" : "multi";
}

function applyCostMode(costMode) {
  const normalizedMode = costMode === "single" ? "single" : "multi";
  document.body.dataset.costMode = normalizedMode;
  localStorage.setItem(COST_MODE_STORAGE_KEY, normalizedMode);
  window.dispatchEvent(new CustomEvent("cost-mode-change", { detail: { costMode: normalizedMode } }));
}

function switchAppView(view, syncUrl, costMode) {
  appViews.forEach((section) => {
    section.hidden = section.getAttribute("data-app-view") !== view;
  });

  appViewButtons.forEach((button) => {
    const targetView = button.getAttribute("data-view-target");
    const buttonCostMode = button.getAttribute("data-cost-mode") || "";
    const normalizedMode = (costMode === "single" || costMode === "multi") ? costMode : getStoredCostMode();
    const isActive = targetView === view && (!buttonCostMode || buttonCostMode === normalizedMode);
    button.classList.toggle("active", isActive);
  });

  localStorage.setItem(APP_VIEW_STORAGE_KEY, view);
  applyCostMode(costMode);

  if (syncUrl) {
    const url = new URL(window.location.href);
    url.searchParams.set("view", view);
    url.searchParams.set("costMode", costMode === "single" ? "single" : "multi");
    window.history.replaceState({}, "", url);
  }
}
