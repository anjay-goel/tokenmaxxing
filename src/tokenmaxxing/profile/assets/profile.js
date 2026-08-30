(() => {
  const root = document.documentElement;
  const storageKey = "tokenmaxxing-theme";

  function preferredTheme() {
    try {
      const saved = localStorage.getItem(storageKey);
      if (saved === "light" || saved === "dark") return saved;
    } catch (_) {}
    return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  root.dataset.theme = root.dataset.theme === "auto" ? preferredTheme() : root.dataset.theme;

  document.addEventListener("DOMContentLoaded", () => {
    const themeToggle = document.querySelector("[data-theme-toggle]");
    const tooltip = document.querySelector("#activity-tooltip");

    function updateThemeLabel() {
      if (!themeToggle) return;
      const next = root.dataset.theme === "dark" ? "light" : "dark";
      const label = `Switch to ${next} theme`;
      themeToggle.setAttribute("aria-label", label);
      themeToggle.setAttribute("title", label);
    }

    themeToggle?.addEventListener("click", () => {
      root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
      try { localStorage.setItem(storageKey, root.dataset.theme); } catch (_) {}
      updateThemeLabel();
    });
    updateThemeLabel();

    const tabs = [...document.querySelectorAll("[role=tab]")];
    function selectTab(tab) {
      tabs.forEach((candidate) => {
        const selected = candidate === tab;
        candidate.setAttribute("aria-selected", String(selected));
        candidate.tabIndex = selected ? 0 : -1;
        const panel = document.querySelector(`#${candidate.getAttribute("aria-controls")}`);
        if (panel) panel.hidden = !selected;
      });
      document.querySelector("#ranking-title").textContent = tab.textContent;
    }
    tabs.forEach((tab, index) => {
      tab.addEventListener("click", () => selectTab(tab));
      tab.addEventListener("keydown", (event) => {
        if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
        event.preventDefault();
        const direction = event.key === "ArrowRight" ? 1 : -1;
        const next = tabs[(index + direction + tabs.length) % tabs.length];
        selectTab(next);
        next.focus();
      });
    });

    function positionTooltip(target) {
      if (!tooltip || !target.dataset.tooltip) return;
      tooltip.textContent = target.dataset.tooltip;
      tooltip.hidden = false;
      const targetBox = target.getBoundingClientRect();
      const box = tooltip.getBoundingClientRect();
      const left = Math.min(window.innerWidth - box.width - 8, Math.max(8, targetBox.left + targetBox.width / 2 - box.width / 2));
      const above = targetBox.top - box.height - 8;
      tooltip.style.left = `${left}px`;
      tooltip.style.top = `${above > 8 ? above : targetBox.bottom + 8}px`;
    }

    let activeTapTooltip = null;

    function hideTooltip() {
      if (tooltip) tooltip.hidden = true;
    }

    function closeTapTooltip() {
      if (!activeTapTooltip) return;
      activeTapTooltip.setAttribute("aria-expanded", "false");
      activeTapTooltip = null;
      hideTooltip();
    }

    function registerTapTooltip(target) {
      target.setAttribute("aria-expanded", "false");
      target.addEventListener("click", (event) => {
        event.stopPropagation();
        const willOpen = activeTapTooltip !== target;
        closeTapTooltip();
        if (!willOpen) return;
        activeTapTooltip = target;
        target.setAttribute("aria-expanded", "true");
        if (target.dataset.tooltip) positionTooltip(target);
      });
    }

    document.querySelectorAll("[data-tooltip]").forEach((target) => {
      registerTapTooltip(target);
      target.addEventListener("mouseenter", () => {
        if (!activeTapTooltip) positionTooltip(target);
      });
      target.addEventListener("mouseleave", () => {
        if (!activeTapTooltip) hideTooltip();
      });
      target.addEventListener("focus", () => {
        if (!activeTapTooltip || activeTapTooltip === target) positionTooltip(target);
      });
      target.addEventListener("blur", () => {
        if (!activeTapTooltip) hideTooltip();
      });
    });

    document.querySelectorAll(".award-medal").forEach(registerTapTooltip);
    document.addEventListener("click", closeTapTooltip);
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeTapTooltip();
    });
    window.addEventListener("resize", () => {
      if (activeTapTooltip?.dataset.tooltip) positionTooltip(activeTapTooltip);
    });
    window.addEventListener("scroll", () => {
      if (activeTapTooltip?.dataset.tooltip) positionTooltip(activeTapTooltip);
    }, true);

    document.querySelectorAll("[data-roving]").forEach((group) => {
      const items = [...group.querySelectorAll("button")];
      group.addEventListener("keydown", (event) => {
        const index = items.indexOf(document.activeElement);
        if (index < 0) return;
        const activity = group.dataset.roving === "activity";
        const movements = activity
          ? { ArrowUp: -1, ArrowDown: 1, ArrowLeft: -7, ArrowRight: 7 }
          : { ArrowLeft: -1, ArrowRight: 1 };
        const movement = movements[event.key];
        if (!movement) return;
        event.preventDefault();
        const next = Math.max(0, Math.min(items.length - 1, index + movement));
        items[index].tabIndex = -1;
        items[next].tabIndex = 0;
        items[next].focus();
      });
    });
  });
})();
