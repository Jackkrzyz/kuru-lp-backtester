"use strict";

// A late transition must never reopen a panel after a newer navigation request.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.BacktestWorkflow = api;
})(typeof globalThis !== "undefined" ? globalThis : this, () => {
  function create({ panels, steps, wait, reducedMotion, onEnter, persistent = false }) {
    let revision = 0;
    let current = "timeframe";
    async function show(next) {
      if (!panels[next]) throw new Error("Unknown backtest step.");
      const token = ++revision;
      const previous = panels[current];
      if (!persistent && next !== current && previous && !previous.hidden) {
        previous.classList.add("leaving");
        if (!reducedMotion()) await wait(160);
      }
      if (token !== revision) return false;
      Object.entries(panels).forEach(([name, panel]) => {
        panel.hidden = persistent ? false : name !== next;
        panel.classList.remove("leaving");
      });
      current = next;
      steps.forEach((step) => {
        const selected = step.dataset.step === next;
        step.classList.toggle("active", selected);
        if (selected) step.setAttribute("aria-current", "step");
        else step.removeAttribute("aria-current");
      });
      await onEnter(next);
      return true;
    }
    function invalidate() {
      revision += 1;
      Object.values(panels).forEach((panel) => panel.classList.remove("leaving"));
    }
    return { show, invalidate, current: () => current };
  }
  return { create };
});
