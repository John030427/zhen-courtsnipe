// First-visit guide: a highlighted box per part of the page with 上一步 / 下一步. Shown once per
// browser (localStorage), reopened from 设置 → 新手引导. It only switches views and tabs; it
// never starts a task or sends a booking request; auto-open reads local task state.
(() => {
  const DONE_KEY = "onboardingDone";
  const GAP = 12;     // space between the highlight and the card
  const MARGIN = 16;  // card keeps this far from the window edge
  const nav = (view) => () => document.querySelector(`[data-nav="${view}"]`)?.click();
  const mode = (m) => () => {
    nav("dashboard")();
    document.querySelector(`[data-mode-tab="${m}"]`)?.click();
  };

  const STEPS = [
    {
      title: "欢迎使用场地预约工作台",
      body: "工作台只在你这台电脑上运行，用你自己的账号查询和预约。花 1 分钟认识一下页面；以后可以在「设置 → 新手引导」再打开。",
    },
    {
      target: ".status-row",
      before: nav("dashboard"),
      title: "先看这条状态线",
      body: "从左到右：连接 → 登录状态 → 学校验证 → 后台任务。前面几步都变绿才能查询和预约；哪一步变红，就照提示处理那一步。",
    },
    {
      target: "#safety-panel",
      before: nav("settings"),
      title: "默认是真实预约",
      body: "抢到空场会直接提交订单。第一次想先完整演练一遍、不下单，就在这里点「开启测试模式」，用完再关掉。",
    },
    {
      target: "#connection-panel",
      before: nav("settings"),
      title: "接入自己的账号",
      body: "推荐运行「配置自动获取登录」，按提示配置后，在企业微信里打开预约页。确认连接、登录同步和只读验证分别成功；采集到信息不代表验证通过。不用 Clash 的话，可以用这里的连接脚本。",
    },
    {
      target: ".matrix-toolbar",
      before: nav("dashboard"),
      title: "场地表",
      body: "下面的表格里，绿色「空」可以约：点格子选时段（同一场地、连续、最多 2 小时），再点「预约所选时段」。每天 20:00 放后天的场；勾着「20:00 自动切到后天并刷新」，到点会自动切过去刷新。",
    },
    {
      target: ".control-stack",
      before: mode("rush"),
      title: "定时抢场：提前设好，到点自动抢",
      body: "目标日期选「后天」，选场地和时段，放号时间填 20:00:00，点「开始抢场」。确认框里看清日期和星期；指定的场地优先，没空会接着试其他场地。",
    },
    {
      target: ".control-stack",
      before: mode("watch"),
      title: "持续监控：等别人退场",
      body: "选一段时间和「预约时长」，比如 16:00–21:00 里任意 1 小时。有连续空场就提醒你，也可以选「自动预约」。登录失效会暂停，恢复后接着查。",
    },
    {
      target: ".sport-switch",
      before: nav("dashboard"),
      title: "羽毛球、网球可以同时抢",
      body: "切换运动后再开一个任务，两个任务各管各的。同一天两个运动的时段不能重叠。",
    },
    {
      target: "#btn-quit",
      title: "退出",
      body: "关掉网页，服务和任务仍在后台运行。要彻底退出就点这里；有任务在运行时会先提醒你。",
    },
  ];

  let index = -1, saved = null, ui = null, lastFocus = null;

  const storage = {
    done() { try { return localStorage.getItem(DONE_KEY) === "1"; } catch { return true; } },
    finish() { try { localStorage.setItem(DONE_KEY, "1"); } catch {} },
  };

  function build() {
    const backdrop = document.createElement("div");
    backdrop.className = "tour-backdrop";
    const spot = document.createElement("div");
    spot.className = "tour-spot";
    const card = document.createElement("div");
    card.className = "tour-card";
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-modal", "true");
    card.setAttribute("aria-labelledby", "tour-title");
    card.setAttribute("aria-describedby", "tour-body");
    card.innerHTML = `
      <p class="tour-count" id="tour-count"></p>
      <h2 class="tour-title" id="tour-title"></h2>
      <p class="tour-body" id="tour-body"></p>
      <div class="tour-dots" aria-hidden="true">${STEPS.map(() => "<span></span>").join("")}</div>
      <div class="tour-actions">
        <button type="button" class="ghost tour-skip">跳过</button>
        <span class="tour-spacer"></span>
        <button type="button" class="ghost tour-prev">上一步</button>
        <button type="button" class="primary tour-next">下一步</button>
      </div>`;
    card.querySelector(".tour-skip").addEventListener("click", () => close());
    card.querySelector(".tour-prev").addEventListener("click", () => show(index - 1));
    card.querySelector(".tour-next").addEventListener("click", () => (index >= STEPS.length - 1 ? close() : show(index + 1)));
    document.body.append(backdrop, spot, card);
    return { backdrop, spot, card };
  }

  function visibleTarget(step) {
    if (!step.target) return null;
    const el = document.querySelector(step.target);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 ? el : null;
  }

  function place() {
    if (!ui || index < 0) return;
    const { spot, card } = ui;
    const el = visibleTarget(STEPS[index]);
    const vw = document.documentElement.clientWidth, vh = window.innerHeight;
    const narrow = vw < 640;
    card.classList.toggle("tour-sheet", narrow && !!el);
    ui.backdrop.classList.toggle("dim", !el);
    card.style.left = card.style.top = "";
    if (!el) {
      spot.hidden = true;
      card.classList.add("tour-center");
      return;
    }
    card.classList.remove("tour-center");
    const r = el.getBoundingClientRect();
    const pad = 6;
    const top = Math.max(r.top - pad, 4), bottom = Math.min(r.bottom + pad, vh - 4);
    Object.assign(spot.style, {
      left: `${Math.max(r.left - pad, 4)}px`, top: `${top}px`,
      width: `${Math.min(r.right + pad, vw - 4) - Math.max(r.left - pad, 4)}px`,
      height: `${Math.max(bottom - top, 0)}px`,
    });
    spot.hidden = false;
    if (narrow) return;  // bottom sheet, positioned by CSS
    const cw = card.offsetWidth, ch = card.offsetHeight;
    const clampX = (x) => Math.min(Math.max(x, MARGIN), vw - cw - MARGIN);
    const clampY = (y) => Math.min(Math.max(y, MARGIN), vh - ch - MARGIN);
    let x, y;
    if (vh - bottom >= ch + GAP + MARGIN) { x = clampX(r.left); y = bottom + GAP; }
    else if (top >= ch + GAP + MARGIN) { x = clampX(r.left); y = top - GAP - ch; }
    else if (r.left >= cw + GAP + MARGIN) { x = r.left - GAP - cw; y = clampY(r.top); }
    else if (vw - r.right >= cw + GAP + MARGIN) { x = r.right + GAP; y = clampY(r.top); }
    else { x = clampX((vw - cw) / 2); y = vh - ch - MARGIN; }
    card.style.left = `${x}px`;
    card.style.top = `${y}px`;
  }

  function show(i) {
    if (i < 0 || i >= STEPS.length) return;
    index = i;
    const step = STEPS[i];
    step.before?.();
    const { card } = ui;
    card.querySelector("#tour-count").textContent = i ? `第 ${i} 步，共 ${STEPS.length - 1} 步` : "新手引导";
    card.querySelector("#tour-title").textContent = step.title;
    card.querySelector("#tour-body").textContent = step.body;
    card.querySelectorAll(".tour-dots span").forEach((dot, n) => dot.classList.toggle("on", n === i));
    card.querySelector(".tour-prev").hidden = i === 0;
    card.querySelector(".tour-skip").hidden = i === STEPS.length - 1;
    card.querySelector(".tour-next").textContent = i === 0 ? "开始" : i === STEPS.length - 1 ? "完成" : "下一步";
    const el = visibleTarget(step);
    // On a phone the card sits at the bottom, so bring the highlighted part to the top.
    if (el) el.scrollIntoView({ block: document.documentElement.clientWidth < 640 ? "start" : "center", behavior: "instant" });
    place();
    card.querySelector(".tour-next").focus({ preventScroll: true });
  }

  function onKey(event) {
    if (event.key === "Escape") { event.preventDefault(); close(); }
    else if (event.key === "ArrowRight") { event.preventDefault(); ui.card.querySelector(".tour-next").click(); }
    else if (event.key === "ArrowLeft" && index > 0) { event.preventDefault(); show(index - 1); }
    else if (event.key === "Tab") {
      // Keep keyboard focus inside the card while it is open.
      const items = [...ui.card.querySelectorAll("button:not([hidden])")];
      const at = items.indexOf(document.activeElement);
      const next = event.shiftKey ? (at <= 0 ? items.length - 1 : at - 1) : (at + 1) % items.length;
      event.preventDefault();
      items[next].focus();
    }
  }

  function open() {
    if (ui) return;
    lastFocus = document.activeElement;
    saved = {
      view: document.querySelector(".nav-item.active[data-nav]")?.dataset.nav || "dashboard",
      mode: document.querySelector(".control-stack")?.dataset.mode || "watch",
      scroll: window.scrollY,
    };
    ui = build();
    document.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    show(0);
  }

  function close() {
    if (!ui) return;
    storage.finish();
    document.removeEventListener("keydown", onKey, true);
    window.removeEventListener("resize", place);
    window.removeEventListener("scroll", place, true);
    ui.backdrop.remove(); ui.spot.remove(); ui.card.remove();
    ui = null; index = -1;
    // Back to where the user was before the guide moved things around.
    nav(saved.view)();
    if (saved.view !== "monitor") document.querySelector(`[data-mode-tab="${saved.mode}"]`)?.click();
    window.scrollTo(0, saved.scroll);
    lastFocus?.focus?.({ preventScroll: true });
  }

  // The guide covers the page, so it does not open by itself around the 20:00 release or while a
  // rush or watch is running; it waits for the next visit instead.
  async function busyNow() {
    const minute = Math.floor((Date.now() + 8 * 3600e3) / 60e3) % 1440;
    if (minute >= 19 * 60 + 50 && minute < 20 * 60 + 10) return true;
    try {
      const response = await fetch("/api/dashboard");
      if (!response.ok) return true;
      const d = await response.json();
      return !!(d.rush?.running || Object.values(d.rush?.by_sport || {}).some((s) => s.running)
        || (d.watch?.running_sports || []).length);
    } catch {
      return true;
    }
  }

  window.openOnboarding = open;
  document.getElementById("btn-onboarding")?.addEventListener("click", open);
  if (!storage.done()) {
    setTimeout(async () => { if (!ui && !(await busyNow())) open(); }, 1200);  // after the page has laid out
  }
})();
