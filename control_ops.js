(() => {
  // The court options are static HTML for badminton's 10 courts; tennis has 7.
  const COURT_COUNT = { badminton: 10, tennis: 7 };

  function courtCount() {
    return COURT_COUNT[window.courtActiveSportId?.() || "badminton"] || 10;
  }

  // 「不限」: every court of the sport, in number order.
  function defaultCourtsForSport() {
    return Array.from({ length: courtCount() }, (_, i) => i + 1).join(",");
  }

  function fillTimeSelect(sel, fromMin, toMin) {
    if (!sel || (sel.options && sel.options.length)) return;
    for (let m = fromMin; m <= toMin; m += 30) {
      const hh = String(Math.floor(m / 60)).padStart(2, "0");
      const mm = String(m % 60).padStart(2, "0");
      const val = `${hh}:${mm}`;
      const opt = document.createElement("option");
      opt.value = opt.textContent = val;
      sel.appendChild(opt);
    }
  }

  function syncHiddenAutoSlots() {
    const start = document.getElementById("op-slot-start");
    const end = document.getElementById("op-slot-end");
    const autoSs = document.getElementById("auto-ss");
    const autoSe = document.getElementById("auto-se");
    if (start && autoSs && start.value) autoSs.value = start.value;
    if (end && autoSe && end.value) autoSe.value = end.value;
  }

  function syncWatchTargetsFromSlots() {
    const start = document.getElementById("op-slot-start");
    const end = document.getElementById("op-slot-end");
    const targets = document.getElementById("watch-targets");
    if (!start || !end || !targets) return;
    if (start.value && end.value) targets.value = `${start.value}-${end.value}`;
  }

  function syncSlotsFromHidden() {
    const start = document.getElementById("op-slot-start");
    const end = document.getElementById("op-slot-end");
    const autoSs = document.getElementById("auto-ss");
    const autoSe = document.getElementById("auto-se");
    const targets = document.getElementById("watch-targets");
    if (autoSs?.value && start) start.value = autoSs.value;
    if (autoSe?.value && end) end.value = autoSe.value;
    if (!start?.value || !end?.value) {
      const line = (targets?.value || "19:00-21:00").trim().split(/\n+/)[0] || "19:00-21:00";
      const [s, e] = line.split("-");
      if (s && start) start.value = s.trim();
      if (e && end) end.value = e.trim();
    }
    syncHiddenAutoSlots();
    syncWatchTargetsFromSlots();
  }

  function courtPriorityList() {
    const courts = document.getElementById("watch-courts");
    if (!courts?.value) return null;
    const list = courts.value
      .split(/[,，\s]+/)
      .filter(Boolean)
      .map(Number)
      .filter((n) => n > 0);
    return list.length ? list : null;
  }

  async function persistRushPlanFromShell(extra = {}) {
    syncHiddenAutoSlots();
    syncWatchTargetsFromSlots();
    applyCourtScope();
    const merged = { ...extra };
    if (merged.court_priority == null) {
      const courts = courtPriorityList();
      if (courts) merged.court_priority = courts;
    }
    if (typeof window.saveAutoPlan === "function") {
      await window.saveAutoPlan(merged);
    }
  }

  function applyCourtScope() {
    const scope = document.getElementById("watch-court-scope");
    const courts = document.getElementById("watch-courts");
    const advanced = document.getElementById("watch-courts-advanced");
    if (!scope || !courts) return;
    const val = scope.value;
    if (val === "all") {
      courts.value = defaultCourtsForSport();
    } else if (val === "priority") {
      const fallback = defaultCourtsForSport();
      courts.value = (advanced?.value || fallback).trim() || fallback;
    } else if (/^\d+$/.test(val)) {
      courts.value = val;
    }
  }

  function syncCourtScopeOptions() {
    const scope = document.getElementById("watch-court-scope");
    if (!scope) return;
    const count = courtCount();
    for (const opt of scope.options) {
      if (opt.value === "all") opt.textContent = `不限（全部 ${count} 个场地）`;
      else if (/^\d+$/.test(opt.value)) opt.hidden = opt.disabled = Number(opt.value) > count;
    }
  }

  function syncCourtScopeFromCourts() {
    const scope = document.getElementById("watch-court-scope");
    const courts = document.getElementById("watch-courts");
    const advanced = document.getElementById("watch-courts-advanced");
    if (!scope || !courts) return;
    const list = courts.value.trim();
    const defaultCourts = defaultCourtsForSport();
    if (!list || list === defaultCourts) {
      scope.value = "all";
      return;
    }
    if (!list.includes(",") && /^\d+$/.test(list)) {
      scope.value = list;
      return;
    }
    scope.value = "priority";
    if (advanced) advanced.value = list;
  }

  function syncControlDayTabs(offset) {
    const off =
      offset !== undefined && offset !== null
        ? Number(offset)
        : typeof window.courtSelectedOffset === "function"
          ? window.courtSelectedOffset()
          : 1;
    document.querySelectorAll(".control-day-tab[data-control-offset]").forEach((btn) => {
      const on = Number(btn.dataset.controlOffset) === off;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    });
  }

  function updateOpShellTitle() {
    const stack = document.querySelector(".control-stack");
    const title = document.getElementById("op-shell-title");
    if (!stack || !title) return;
    title.textContent = stack.dataset.mode === "rush" ? "抢场设置" : "监控设置";
  }

  function bindControlDayTabs() {
    document.querySelectorAll(".control-day-tab[data-control-offset]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const off = Number(btn.dataset.controlOffset);
        if (typeof window.courtSetSelectedDateOffset === "function") {
          window.courtSetSelectedDateOffset(off, { persist: true });
        } else if (typeof window.setSelectedDateOffset === "function") {
          window.setSelectedDateOffset(off, { persist: true });
        }
        syncControlDayTabs(off);
      });
    });
  }

  function bindSlotSync() {
    ["op-slot-start", "op-slot-end"].forEach((id) => {
      const el = document.getElementById(id);
      el?.addEventListener("change", () => {
        syncHiddenAutoSlots();
        syncWatchTargetsFromSlots();
        void persistRushPlanFromShell();
      });
    });
    const scope = document.getElementById("watch-court-scope");
    scope?.addEventListener("change", () => {
      applyCourtScope();
      void persistRushPlanFromShell();
    });
    const advanced = document.getElementById("watch-courts-advanced");
    advanced?.addEventListener("change", () => {
      if (scope?.value === "priority") {
        applyCourtScope();
        void persistRushPlanFromShell();
      }
    });
    advanced?.addEventListener("input", () => {
      if (scope?.value === "priority") {
        applyCourtScope();
        void persistRushPlanFromShell();
      }
    });
    const form = document.getElementById("watch-form");
    form?.addEventListener("submit", () => {
      syncWatchTargetsFromSlots();
      applyCourtScope();
    });
  }

  function bindRushStop() {
    const stop = document.getElementById("rush-stop");
    stop?.addEventListener("click", async () => {
      try {
        await fetch('/api/auto/stop?sport_id='+encodeURIComponent(window.courtActiveSportId?.() || 'badminton'), { method: "POST" });
      } catch {
        /* pollAuto will reflect state */
      }
    });
    setInterval(() => {
      const running =
        typeof window.courtAutoRunning === "function" ? window.courtAutoRunning() : false;
      const start = document.getElementById("btn-auto");
      if (start && !start.disabled) {
        start.textContent = running ? "▶ 抢场运行中…" : "▶ 开始抢场 (Rush)";
      }
    }, 600);
  }

  function initControlOpsShell() {
    fillTimeSelect(document.getElementById("op-slot-start"), 8 * 60, 21 * 60 + 30);
    fillTimeSelect(document.getElementById("op-slot-end"), 8 * 60 + 30, 22 * 60);
    fillTimeSelect(document.getElementById("auto-ss"), 8 * 60, 21 * 60 + 30);
    fillTimeSelect(document.getElementById("auto-se"), 8 * 60 + 30, 22 * 60);
    syncSlotsFromHidden();
    syncCourtScopeOptions();
    syncCourtScopeFromCourts();
    syncControlDayTabs();
    bindControlDayTabs();
    updateOpShellTitle();
    bindSlotSync();
    bindRushStop();
  }

  window.syncControlDayTabs = syncControlDayTabs;
  window.syncWatchTargetsFromSlots = syncWatchTargetsFromSlots;
  window.applyCourtScopeFromShell = applyCourtScope;
  window.persistRushPlanFromShell = persistRushPlanFromShell;
  window.updateOpShellTitle = updateOpShellTitle;
  window.syncCourtScopeOptions = syncCourtScopeOptions;
  window.courtCountForSport = (sportId) => COURT_COUNT[sportId] || 10;
  window.initControlOpsShell = initControlOpsShell;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initControlOpsShell);
  } else {
    initControlOpsShell();
  }
})();
