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

    function splitTooltipLine(line) {
      const marker = " · ";
      const index = line.lastIndexOf(marker);
      return index < 0 ? [line, ""] : [line.slice(0, index), line.slice(index + marker.length)];
    }

    function formatTooltipDate(value) {
      if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
      return new Date(`${value}T00:00:00Z`).toLocaleDateString(undefined, {
        day: "numeric",
        month: "short",
        year: "numeric",
        timeZone: "UTC",
      });
    }

    function populateTooltip(value) {
      if (!tooltip) return;
      const [headline, ...details] = value.split("\n");
      const [titleText, totalText] = splitTooltipLine(headline);
      tooltip.replaceChildren();

      const title = document.createElement("strong");
      title.className = "chart-tooltip-title";
      title.textContent = formatTooltipDate(titleText);
      tooltip.append(title);

      const total = document.createElement("span");
      total.className = "chart-tooltip-total";
      total.textContent = totalText;
      tooltip.append(total);

      if (!details.length) return;
      const breakdown = document.createElement("dl");
      breakdown.className = "chart-tooltip-breakdown";
      details.forEach((line) => {
        const [name, amount] = splitTooltipLine(line);
        const term = document.createElement("dt");
        const description = document.createElement("dd");
        term.textContent = name;
        description.textContent = amount;
        breakdown.append(term, description);
      });
      tooltip.append(breakdown);
    }

    function positionTooltip(target) {
      if (!tooltip || !target.dataset.tooltip) return;
      populateTooltip(target.dataset.tooltip);
      tooltip.hidden = false;
      const targetBox = target.getBoundingClientRect();
      const box = tooltip.getBoundingClientRect();
      const left = Math.min(window.innerWidth - box.width - 8, Math.max(8, targetBox.left + targetBox.width / 2 - box.width / 2));
      const above = targetBox.top - box.height - 8;
      const preferredTop = above > 8 ? above : targetBox.bottom + 8;
      const top = Math.max(8, Math.min(window.innerHeight - box.height - 8, preferredTop));
      tooltip.style.left = `${left}px`;
      tooltip.style.top = `${top}px`;
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
      if (event.key === "Escape") {
        closeTapTooltip();
        hideTooltip();
      }
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
