(() => {
  const STORAGE_KEY = "workbench_active_sport";
  const DEFAULT_CAPS = {
    badminton: {
      id: "badminton",
      label: "羽毛球",
      emoji: "🏸",
      matrix: true,
      watch: true,
      rush: true,
      reserve: true,
    },
    tennis: {
      id: "tennis",
      label: "网球",
      emoji: "🎾",
      matrix: true,
      watch: true,
      rush: true,
      reserve: true,
    },
  };

  let capabilities = { ...DEFAULT_CAPS };
  let activeSportId = "badminton";
  let logSportFilter = "all";
  let onSportSwitch = null;

  const sportViews = {
    badminton: freshView(),
    tennis: freshView(),
  };

  function freshView() {
    return {
      matrixOffset: 1,
      matrixTargetSlots: [],
      matrixViewDate: "",
      matrixLastScan: null,
      watchForm: null,
      rushSlots: null,
    };
  }

  function cap(id) {
    return capabilities[id] || DEFAULT_CAPS[id] || DEFAULT_CAPS.badminton;
  }

  function activeView() {
    return sportViews[activeSportId] || sportViews.badminton;
  }

  function persistActiveSport() {
    try {
      localStorage.setItem(STORAGE_KEY, activeSportId);
    } catch {
      /* ignore */
    }
  }

  function readWatchForm() {
    const g = (id) => document.getElementById(id);
    return {
      targets: g("watch-targets")?.value ?? "",
      interval: g("watch-interval")?.value ?? "",
      courts: g("watch-courts")?.value ?? "",
      courtScope: g("watch-court-scope")?.value ?? "",
      courtsAdvanced: g("watch-courts-advanced")?.value ?? "",
      duration: g("watch-duration")?.value ?? "",
      span: g("watch-span")?.value ?? "",
      at: g("watch-at")?.value ?? "",
      attempts: g("watch-attempts")?.value ?? "",
      reserve: !!g("watch-reserve")?.checked,
      autoSs: g("auto-ss")?.value ?? g("op-slot-start")?.value ?? "",
      autoSe: g("auto-se")?.value ?? g("op-slot-end")?.value ?? "",
      autoRt: g("auto-rt")?.value ?? "",
      opSlotStart: g("op-slot-start")?.value ?? "",
      opSlotEnd: g("op-slot-end")?.value ?? "",
    };
  }

  function applyWatchForm(form) {
    if (!form) return;
    const g = (id) => document.getElementById(id);
    if (g("watch-targets")) g("watch-targets").value = form.targets;
    if (g("watch-interval")) g("watch-interval").value = form.interval;
    if (g("watch-courts")) g("watch-courts").value = form.courts;
    if (g("watch-courts-advanced") && form.courtsAdvanced)
      g("watch-courts-advanced").value = form.courtsAdvanced;
    if (g("watch-court-scope") && form.courtScope) g("watch-court-scope").value = form.courtScope;
    if (g("watch-duration")) g("watch-duration").value = form.duration;
    if (g("watch-span") && form.span) g("watch-span").value = form.span;
    if (g("watch-at")) g("watch-at").value = form.at;
    if (g("watch-attempts")) g("watch-attempts").value = form.attempts;
    if (g("watch-reserve")) g("watch-reserve").checked = form.reserve;
    if (g("auto-ss") && form.autoSs) g("auto-ss").value = form.autoSs;
    if (g("auto-se") && form.autoSe) g("auto-se").value = form.autoSe;
    if (g("auto-rt") && form.autoRt) g("auto-rt").value = form.autoRt;
    if (g("op-slot-start") && form.opSlotStart) g("op-slot-start").value = form.opSlotStart;
    if (g("op-slot-end") && form.opSlotEnd) g("op-slot-end").value = form.opSlotEnd;
    window.syncWatchTargetsFromSlots?.();
    window.applyCourtScopeFromShell?.();
    window.syncControlDayTabs?.();
    window.syncWatchControls?.();
  }

  function snapshotCurrentView(getters) {
    const view = activeView();
    view.matrixOffset = getters.matrixOffset();
    view.matrixTargetSlots = getters.matrixTargetSlots().slice();
    view.matrixViewDate = getters.matrixViewDate();
    view.matrixLastScan = getters.matrixLastScan();
    view.watchForm = readWatchForm();
  }

  function restoreViewToGlobals(id, setters) {
    const view = sportViews[id] || sportViews.badminton;
    setters.setMatrixOffset(view.matrixOffset);
    setters.setMatrixTargetSlots(view.matrixTargetSlots.slice());
    setters.setMatrixViewDate(view.matrixViewDate);
    setters.setMatrixLastScan(view.matrixLastScan);
    applyWatchForm(view.watchForm);
  }

  function syncSportTabs() {
    document.querySelectorAll("[data-sport-id]").forEach((btn) => {
      const on = btn.dataset.sportId === activeSportId;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    });
    const brand = document.getElementById("nav-brand-title");
    if (brand) {
      const c = cap(activeSportId);
      brand.textContent = `${c.emoji || ""} ${c.label || activeSportId}`.trim();
    }
  }

  function applyCapabilityChrome() {
    const c = cap(activeSportId);
    const stub = document.getElementById("matrix-sport-stub");
    const wrap = document.querySelector(".matrix-wrap");
    const legend = document.getElementById("matrix-legend");
    const scanBtn = document.getElementById("btn-matrix-scan");
    const targetPanel = document.getElementById("matrix-target-panel");
    const stack = document.querySelector(".control-stack");
    const tennisNote = document.getElementById("sport-capability-note");

    const matrixOn = !!c.matrix;
    if (stub) {
      stub.hidden = matrixOn;
      const title = stub.querySelector(".stub-title");
      if (title) title.textContent = c.stub_message || "尚未接入";
    }
    if (wrap) wrap.hidden = !matrixOn;
    if (legend) legend.hidden = !matrixOn;
    if (scanBtn) scanBtn.disabled = !matrixOn;
    if (targetPanel) targetPanel.hidden = !matrixOn;

    const watchOk = !!c.watch;
    const rushOk = !!c.rush;
    const watchStart = document.getElementById("watch-start");
    const watchStop = document.getElementById("watch-stop");
    const rushStop = document.getElementById("rush-stop");
    const watchFields = document.getElementById("watch-fields");
    const btnAuto = document.getElementById("btn-auto");
    const btnReserve = document.getElementById("btn-target-reserve");
    const stubMsg = c.stub_message || "尚未接入";
    if (watchFields) watchFields.disabled = !watchOk;
    const watchRunning =
      typeof window.courtWatchRunning === "function" ? window.courtWatchRunning() : false;
    const watchReserve = document.getElementById("watch-reserve");
    if (watchReserve) {
      const notifyOnly = !!c.notify_only || !watchOk;
      watchReserve.disabled = notifyOnly || watchFields?.disabled;
      if (notifyOnly) {
        watchReserve.checked = false;
        watchReserve.title = c.stub_message || "该运动仅通知，不自动预约";
      } else {
        watchReserve.removeAttribute("title");
      }
    }
    const rushRunning =
      typeof window.courtAutoRunning === "function" ? window.courtAutoRunning() : false;

    if (watchStart) {
      watchStart.disabled = !watchOk;
      watchStart.title = watchOk ? "" : stubMsg;
    }
    if (watchStop) {
      if (!watchOk && !watchRunning) {
        watchStop.disabled = true;
        watchStop.title = stubMsg;
      } else if (!watchRunning) {
        watchStop.disabled = true;
        watchStop.removeAttribute("title");
      } else {
        watchStop.disabled = false;
        watchStop.title = watchOk ? "" : "停止后台监控任务";
      }
    }
    if (btnAuto) {
      btnAuto.disabled = !rushOk && !rushRunning;
      btnAuto.title = rushOk || rushRunning ? "" : stubMsg;
    }
    if (rushStop) {
      if (!rushOk && !rushRunning) {
        rushStop.disabled = true;
        rushStop.title = stubMsg;
      } else if (!rushRunning) {
        rushStop.disabled = true;
        rushStop.removeAttribute("title");
      } else {
        rushStop.disabled = false;
        rushStop.title = rushOk ? "" : "停止后台抢场任务";
      }
    }
    const reserveOk = matrixOn && c.reserve !== false;
    if (btnReserve) btnReserve.disabled = !reserveOk;

    const shellDisabled = !watchOk && !rushOk;
    document
      .querySelectorAll(
        "#op-settings select, #op-settings .control-day-tab, #op-slot-start, #op-slot-end, #auto-rt",
      )
      .forEach((el) => {
        if (shellDisabled) {
          el.setAttribute("disabled", "true");
          el.title = stubMsg;
        } else {
          el.removeAttribute("disabled");
          el.removeAttribute("title");
        }
      });

    if (stack) stack.dataset.sportCap = matrixOn ? "ready" : "stub";
    if (tennisNote) {
      tennisNote.textContent = matrixOn ? "" : stubMsg;
      tennisNote.hidden = matrixOn;
    }

    document.querySelectorAll("[data-mode-tab]").forEach((tab) => {
      if (tab.dataset.modeTab === "watch" && !watchOk) tab.setAttribute("aria-disabled", "true");
      else if (tab.dataset.modeTab === "rush" && !rushOk) tab.setAttribute("aria-disabled", "true");
      else tab.removeAttribute("aria-disabled");
    });
  }

  function sportTagForLog(msg) {
    if (/网球|🎾|tennis/i.test(msg || "")) return "tennis";
    return "badminton";
  }

  function logSportFilterValue() {
    return logSportFilter;
  }

  function decorateLogEntry(div, entry) {
    const sport = entry?.sport_id || sportTagForLog(entry?.msg);
    div.dataset.sportId = sport;
    const badge = cap(sport).emoji || "";
    if (badge && div.textContent && !div.textContent.includes(badge)) {
      div.textContent = `${badge} ${div.textContent}`;
    }
  }

  function switchSport(nextId, hooks) {
    if (!capabilities[nextId] && !DEFAULT_CAPS[nextId]) return;
    if (nextId === activeSportId) return;
    snapshotCurrentView(hooks.getters);
    activeSportId = nextId;
    persistActiveSport();
    restoreViewToGlobals(activeSportId, hooks.setters);
    syncSportTabs();
    applyCapabilityChrome();
    hooks.afterSwitch(activeSportId);
    if (typeof onSportSwitch === "function") onSportSwitch(activeSportId);
  }

  function initSportScope(options = {}) {
    onSportSwitch = options.onSwitch || null;
    if (options.capabilities) {
      capabilities = { ...DEFAULT_CAPS, ...options.capabilities };
    }
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved && (capabilities[saved] || DEFAULT_CAPS[saved])) activeSportId = saved;
    } catch {
      /* ignore */
    }

    document.querySelectorAll("[data-sport-id]").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (!options.hooks) return;
        switchSport(btn.dataset.sportId, options.hooks);
      });
    });

    document.querySelectorAll("[data-log-sport]").forEach((btn) => {
      btn.addEventListener("click", () => {
        logSportFilter = btn.dataset.logSport || "all";
        document.querySelectorAll("[data-log-sport]").forEach((b) => {
          b.classList.toggle("active", b.dataset.logSport === logSportFilter);
        });
        if (typeof window.applyLogFilter === "function") window.applyLogFilter();
      });
    });

    syncSportTabs();
    applyCapabilityChrome();
    return activeSportId;
  }

  function mergeCapabilitiesFromConfig(cfg) {
    if (cfg?.sports) capabilities = { ...DEFAULT_CAPS, ...cfg.sports };
  }

  window.SportScope = {
    initSportScope,
    mergeCapabilitiesFromConfig,
    decorateLogEntry,
    logSportFilter: logSportFilterValue,
    applyCapabilityChrome,
    applyWatchForm,
    snapshotCurrentView,
    setSportOffset: (id, offset) => {
      if (sportViews[id]) sportViews[id].matrixOffset = offset;
    },
    activeSportId: () => activeSportId,
    activeView,
    cap,
    sportWatchEnabled: () => !!cap(activeSportId).watch,
    sportRushEnabled: () => !!cap(activeSportId).rush,
    sportMatrixEnabled: () => !!cap(activeSportId).matrix,
    setActiveSportId: (id) => {
      activeSportId = id;
    },
  };
  window.courtActiveSportId = () => activeSportId;
  window.sportWatchEnabled = () => !!cap(activeSportId).watch;
  window.sportRushEnabled = () => !!cap(activeSportId).rush;
  window.sportMatrixEnabled = () => !!cap(activeSportId).matrix;
})();
