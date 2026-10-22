window.FilterPopupPortal = (() => {
  let nextOwnerId = 1;

  function getOwnerId(container) {
    if (!container.dataset.filterPopupOwner) {
      container.dataset.filterPopupOwner = `filter-popup-owner-${nextOwnerId}`;
      nextOwnerId += 1;
    }
    return container.dataset.filterPopupOwner;
  }

  function getPopup(container) {
    const ownerId = getOwnerId(container);
    return document.querySelector(`.filter-popup[data-filter-popup-portal-owner="${ownerId}"]`)
      || container.querySelector(".filter-popup[data-filter-popup], .filter-popup[data-admin-table-filter-popup]");
  }

  function contains(container, target) {
    return container.contains(target) || Boolean(getPopup(container)?.contains(target));
  }

  function capture(container) {
    const popup = getPopup(container);
    const options = popup?.querySelector(".filter-options");
    return {
      columnKey: popup?.getAttribute("data-filter-popup") || "",
      scrollTop: options?.scrollTop || 0
    };
  }

  function clear(container) {
    const ownerId = getOwnerId(container);
    document.querySelector(`.filter-popup[data-filter-popup-portal-owner="${ownerId}"]`)?.remove();
  }

  function mount(container, previousState) {
    const popup = container.querySelector(".filter-popup[data-filter-popup]");
    if (!popup) return;
    const trigger = findTrigger(container, popup);
    if (!trigger) return;

    const ownerId = getOwnerId(container);
    popup.dataset.filterPopupPortalOwner = ownerId;
    popup.classList.add("filter-popup--portal");
    popup.style.position = "fixed";
    popup.style.zIndex = "1000";
    popup.style.width = "max-content";
    popup.style.maxWidth = "calc(100vw - 24px)";
    popup.style.overflow = "hidden";
    document.body.appendChild(popup);
    ["click", "input", "change", "compositionend", "pointerdown"].forEach((eventName) => {
      popup.addEventListener(eventName, (event) => {
        event.stopPropagation();
        const forwardedEvent = new Event(eventName, { bubbles: false, cancelable: true });
        Object.defineProperty(forwardedEvent, "target", { value: event.target });
        Object.defineProperty(forwardedEvent, "isComposing", { value: Boolean(event.isComposing) });
        container.dispatchEvent(forwardedEvent);
      });
    });
    const options = popup.querySelector(".filter-options");
    if (options && previousState.columnKey === popup.getAttribute("data-filter-popup")) options.scrollTop = previousState.scrollTop;
    position(popup, trigger);
  }

  function position(popup, trigger) {
    const gap = 4;
    const viewportPadding = 12;
    popup.style.maxHeight = "calc(100vh - 24px)";
    popup.style.visibility = "hidden";
    popup.style.left = "0px";
    popup.style.top = "0px";
    const triggerRect = trigger.getBoundingClientRect();
    const columnHeader = trigger.closest("th, .filterable-th");
    const anchorLeft = columnHeader ? columnHeader.getBoundingClientRect().left : triggerRect.left;
    const popupRect = popup.getBoundingClientRect();
    const belowTop = triggerRect.bottom + gap;
    const aboveTop = triggerRect.top - popupRect.height - gap;
    const top = belowTop + popupRect.height <= window.innerHeight - viewportPadding || aboveTop < viewportPadding
      ? belowTop
      : aboveTop;
    const left = Math.min(
      Math.max(viewportPadding, anchorLeft),
      Math.max(viewportPadding, window.innerWidth - popupRect.width - viewportPadding)
    );
    const popupTop = Math.min(
      Math.max(viewportPadding, top),
      Math.max(viewportPadding, window.innerHeight - popupRect.height - viewportPadding)
    );
    const availableHeight = Math.max(120, window.innerHeight - popupTop - viewportPadding);
    popup.style.top = `${popupTop}px`;
    popup.style.left = `${left}px`;
    popup.style.maxHeight = `${availableHeight}px`;
    const options = popup.querySelector(".filter-options");
    if (options) {
      const fixedHeight = popup.offsetHeight - options.offsetHeight;
      options.style.maxHeight = `${Math.max(80, availableHeight - fixedHeight)}px`;
    }
    popup.style.visibility = "visible";
  }

  function reposition(container) {
    const popup = getPopup(container);
    if (!popup) return;
    const trigger = findTrigger(container, popup);
    if (trigger) position(popup, trigger);
  }

  function findTrigger(container, popup) {
    const rawColumnKey = popup.getAttribute("data-column") || popup.getAttribute("data-filter-popup") || "";
    const columnKey = rawColumnKey.replace(/^(?:admin-|unmapped-|subscription-allocation-|subscription-summary-)/, "");
    return Array.from(popup.parentElement?.querySelectorAll(".filter-toggle") || [])
      .find((trigger) => trigger.dataset.column === columnKey)
      || Array.from(container.querySelectorAll(".filter-toggle"))
        .find((trigger) => trigger.dataset.column === columnKey)
      || container.querySelector(".filter-toggle.filtered, .filter-toggle");
  }

  window.addEventListener("resize", () => document.querySelectorAll(".filter-popup[data-filter-popup-portal-owner]").forEach((popup) => {
    const owner = document.querySelector(`[data-filter-popup-owner="${popup.dataset.filterPopupPortalOwner}"]`);
    if (owner) reposition(owner);
  }));
  window.addEventListener("scroll", () => document.querySelectorAll(".filter-popup[data-filter-popup-portal-owner]").forEach((popup) => {
    const owner = document.querySelector(`[data-filter-popup-owner="${popup.dataset.filterPopupPortalOwner}"]`);
    if (owner) reposition(owner);
  }), true);

  return { capture, clear, contains, mount };
})();
