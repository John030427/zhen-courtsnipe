// 「今晚的抢场」: every sport's Rush in one card, whichever sport tab is open, so two Rushes can
// be followed and stopped side by side. Reads /api/dashboard's rush.by_sport; only the stop
// button sends anything, and only to this computer's workbench.
(() => {
  const SPORTS = { badminton: ["🏸", "羽毛球"], tennis: ["🎾", "网球"] };
  const shanghaiDay = (ms = Date.now()) => new Date(ms + 8 * 3600e3).toISOString().slice(0, 10);

  function dayLabel(iso, today = shanghaiDay()) {
    if (!iso) return "";
    const diff = Math.round((Date.parse(`${iso}T00:00:00Z`) - Date.parse(`${today}T00:00:00Z`)) / 86400e3);
    const weekday = "周" + "日一二三四五六"[new Date(`${iso}T00:00:00Z`).getUTCDay()];
    return `${({ 0: "今天", 1: "明天", 2: "后天" })[diff] || iso.slice(5)}（${weekday}）`;
  }

  const clock = (sec) => `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
  const ENDINGS = {
    target_not_open: "目标时段在查到的场地上一直是「不可约」：具体原因未确认，不能据此判断是否被别人抢走。",
    target_full: "目标时段在查到的场地上都已约满或不可约，没有出现过空位。",
  };

  function courtsText(st) {
    const first = st.first_courts || [];
    if (!st.courts) return "";
    return first.length && first.length < st.courts
      ? `${first.join("、")} 号优先，共 ${st.courts} 个场地` : `${st.courts} 个场地`;
  }

  // What the school said, in plain words: a page instead of data means no normal answer, and a
  // network error means nothing came back to this computer at all.
  const schoolSaid = (order) => (String(order.msg || "").startsWith("网络异常") ? "本机没收到学校的回复（网络异常）"
    : ["non-json", "invalid-response"].includes(order.msg) || !order.msg
      ? "学校没有正常回复" : `学校回「${order.msg}」`);
  const mostlyFailed = (st) => st.queries > 0 && st.query_errors * 2 > st.queries;

  // The run as the engine saw it, one line per step: when (from release), which court, what.
  function replayLines(st) {
    return (st.timeline || []).filter((step) => step.kind !== "end").map((step) => {
      const t = typeof step.t === "number" ? (step.t < 0 ? `放号前 ${Math.round(-step.t)} 秒` : `+${step.t.toFixed(1)} 秒`) : "";
      return [t, step.court ? `${step.court} 号场` : "", step.text].filter(Boolean).join("　");
    });
  }

  // One row per sport that has a Rush: running, or ended today. An ending from an earlier day
  // is dropped unless the order is still to be checked. Pure, for tests.
  function rushOverviewRows(bySport, today = shanghaiDay()) {
    const rows = [];
    for (const [sid, st] of Object.entries(bySport || {})) {
      if (!st || (!st.running && (!st.phase || st.phase === "idle"))) continue;
      if (!st.running && st.finished_on && st.finished_on !== today && st.outcome !== "uncertain") continue;
      const [emoji, label] = SPORTS[sid] || ["", sid];
      const windows = (st.windows || []).map(([a, b]) => `${a}–${b}`).join("、");
      const row = { sid, title: `${emoji} ${label} ${dayLabel(st.date, today)} ${windows}`.trim(),
        sub: courtsText(st), pill: "", kind: "", detail: "", actions: [], replay: replayLines(st) };
      const order = st.last_order || {};
      const booked = (st.booked_courts || []).join("、") || order.court;
      const open = (st.open_courts || []).join("、") || order.court;
      if (st.running) {
        row.pill = ["scanning"].includes(st.phase) ? "扫描中" : st.phase === "ordering" ? "下单中"
          : st.release_in_sec ? `等待放号 · ${clock(st.release_in_sec)}` : "准备中";
        // Stopping halfway through an order cannot take it back; wait for its answer first.
        row.actions = st.phase === "ordering" ? [] : ["stop"];
      } else if (st.outcome === "booked") {
        row.pill = "预约成功"; row.kind = "ok";
        row.detail = (booked ? `${booked} 号场已约上。` : "")
          + (st.doubled ? "同一时段约上了不止一个场：请到企业微信「我的预约」取消多余的。" : "")
          + ((st.open_courts || []).length ? `另有 ${open} 号场的订单结果没法确认，也请核对。` : "");
      } else if (st.outcome === "simulated") {
        row.pill = "模拟完成"; row.kind = "ok";
      } else if (st.outcome === "uncertain") {
        row.pill = "结果待核实"; row.kind = "err";
        row.detail = `${open ? `${open} 号场已提交，` : ""}${schoolSaid(order)}`
          + `${order.reply_hint ? `（${order.reply_hint}）` : ""}。当前无法确认是否约上：去企业微信「我的预约」核对，工作台不会再自动下单。`;
      } else if (st.phase === "degraded") {
        row.pill = "已停止（需处理）"; row.kind = "err"; row.detail = st.msg || "";
      } else if (st.phase === "stopped") {
        row.pill = "已停止"; row.detail = "";
      } else {
        row.pill = "没抢到"; row.kind = "warn";
        row.detail = ENDINGS[st.reason] || (st.matches
          ? `找到过 ${st.matches} 次空场，但都没下成${order.msg ? `；最后一次：${order.msg}` : ""}。详情看运行日志。`
          : mostlyFailed(st) ? `${st.query_errors}/${st.queries} 次查询失败，没能确认有没有空场：看状态线「学校验证」和运行日志。`
            : "扫描时间内没有出现能约的空场。");
        if (ENDINGS[st.reason]) row.actions = ["watch"];
      }
      rows.push(row);
    }
    return rows;
  }

  let lastKey = "";

  // Steps only ever get appended during a run; an open list stays open.
  function fillReplay(details, lines) {
    if (!details) return;
    const list = details.querySelector("ol");
    if (JSON.stringify([...list.children].map((li) => li.textContent)) === JSON.stringify(lines)) return;
    details.querySelector("summary").textContent = `过程回放（${lines.length} 步）`;
    list.replaceChildren(...lines.map((text) => Object.assign(document.createElement("li"), { textContent: text })));
  }

  function render(bySport) {
    const card = document.getElementById("rush-overview");
    if (!card) return;
    const rows = rushOverviewRows(bySport);
    const list = card.querySelector(".rush-overview-rows");
    list.hidden = !rows.length;
    // Rebuild only when something besides the countdown and the replay changed, so a button is
    // not swapped out under the pointer; those two are updated in place.
    const key = JSON.stringify(rows.map((row) => ({ ...row, replay: row.replay.length > 0,
      pill: row.pill.startsWith("等待放号") ? "等待放号" : row.pill })));
    if (key === lastKey) {
      rows.forEach((row, i) => {
        const pill = list.children[i]?.querySelector(".rush-overview-pill");
        if (pill && pill.textContent !== row.pill) pill.textContent = row.pill;
        fillReplay(list.children[i]?.querySelector(".rush-overview-replay"), row.replay);
      });
      return;
    }
    lastKey = key;
    list.replaceChildren(...rows.map((row) => {
      const el = document.createElement("div");
      el.className = "rush-overview-row";
      const main = document.createElement("div");
      main.className = "rush-overview-main";
      const head = document.createElement("span");
      head.className = "rush-overview-title";
      head.textContent = row.title;
      const pill = document.createElement("span");
      pill.className = `rush-overview-pill ${row.kind}`;
      pill.textContent = row.pill;
      main.append(head, pill);
      for (const text of [row.sub, row.detail]) {
        if (!text) continue;
        const line = document.createElement("span");
        line.className = "rush-overview-sub";
        line.textContent = text;
        main.append(line);
      }
      if (row.replay.length) {
        const replay = document.createElement("details");
        replay.className = "rush-overview-replay";
        replay.append(document.createElement("summary"), document.createElement("ol"));
        fillReplay(replay, row.replay);
        main.append(replay);
      }
      const acts = document.createElement("div");
      acts.className = "rush-overview-actions";
      for (const action of row.actions) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "ghost";
        btn.textContent = action === "stop" ? "停止" : "改成持续监控";
        btn.addEventListener("click", () => (action === "stop" ? stop(row.sid) : toWatch(row.sid)));
        acts.append(btn);
      }
      el.append(main, acts);
      return el;
    }));
  }

  function showSport(sid) {
    if ((window.courtActiveSportId?.() || "badminton") !== sid) {
      document.querySelector(`.sport-tab[data-sport-id="${sid}"]`)?.click();
    }
  }

  async function stop(sid) {
    if (!window.confirm(`停止${(SPORTS[sid] || ["", sid])[1]}抢场？停止后这一轮就不抢了。`)) return;
    try {
      const r = await fetch(`/api/auto/stop?sport_id=${encodeURIComponent(sid)}`, { method: "POST" });
      if (!r.ok || (await r.json()).ok !== true) throw Error();
      window.toast?.(`${(SPORTS[sid] || ["", sid])[1]}抢场已停止`, "ok");
    } catch {
      window.toast?.("停止失败，请再试一次", "err");
    }
  }

  // Same sport, date and time range: the shared settings already hold them for that sport.
  function toWatch(sid) {
    showSport(sid);
    document.querySelector('[data-mode-tab="watch"]')?.click();
    window.toast?.("已切到持续监控：请确认日期、时段和预约时长，再点「开始监控」", "ok", 5000);
  }

  // ── Setting up both sports here, without switching the sport tab ────────────────────────
  const COURTS = { badminton: 10, tennis: 7 };
  const SETUP_KEY = (sid) => `rushSetup:${sid}`;
  const DEFAULT_SETUP = { offset: 2, court: 0, start: "20:00", end: "22:00" };
  const minutes = (hm) => { const [h, m] = String(hm).split(":").map(Number); return h * 60 + m; };
  const dayFromOffset = (offset, today = shanghaiDay()) =>
    new Date(Date.parse(`${today}T00:00:00Z`) + offset * 86400e3).toISOString().slice(0, 10);

  // What /api/auto/start receives for one sport's setup. Court 0 means every court in number
  // order; a chosen court is scanned first and then the rest, as in the main form. Pure.
  function rushPlanFromSetup(sid, setup, releaseAt, today = shanghaiDay()) {
    const s = { ...DEFAULT_SETUP, ...(setup || {}) };
    return { sport_id: sid, date: dayFromOffset(Number(s.offset), today), slot_start: s.start, slot_end: s.end,
      release_at: String(releaseAt || "20:00:00").trim() || "20:00:00", court_priority: Number(s.court) ? [Number(s.court)] : [] };
  }

  // Why these plans cannot start together, in words; "" when they can. Pure.
  function rushSetupProblem(plans) {
    for (const p of plans) {
      if (!(minutes(p.slot_end) > minutes(p.slot_start))) return `${SPORTS[p.sport_id][1]}：结束时间要晚于开始时间`;
    }
    const [a, b] = plans;
    if (a && b && a.date === b.date && minutes(a.slot_start) < minutes(b.slot_end) && minutes(b.slot_start) < minutes(a.slot_end)) {
      return `两个运动是同一天，时段重叠（${a.slot_start}–${a.slot_end} 和 ${b.slot_start}–${b.slot_end}）：同一个人不能同时在两个场地，请错开`;
    }
    return "";
  }

  function loadSetup(sid) {
    try { return { ...DEFAULT_SETUP, ...JSON.parse(localStorage.getItem(SETUP_KEY(sid)) || "{}") }; }
    catch { return { ...DEFAULT_SETUP }; }
  }
  function saveSetup(sid, setup) {
    try { localStorage.setItem(SETUP_KEY(sid), JSON.stringify(setup)); } catch {}
  }

  const releaseAt = () => document.getElementById("auto-rt")?.value || "20:00:00";
  const setups = {};   // sid -> current setup, edited by the forms
  const forms = {};    // sid -> form element, built once so typing is never interrupted by a poll
  let lastBySport = {};
  let starting = false;

  function option(value, text, selected) {
    const o = document.createElement("option");
    o.value = value; o.textContent = text; o.selected = selected;
    return o;
  }

  function timeSelect(value, from, to, label, onChange) {
    const sel = document.createElement("select");
    sel.setAttribute("aria-label", label);
    for (let m = from; m <= to; m += 30) {
      const hm = `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
      sel.append(option(hm, hm, hm === value));
    }
    sel.addEventListener("change", () => onChange(sel.value));
    return sel;
  }

  function buildForm(sid) {
    const setup = setups[sid] = loadSetup(sid);
    const [emoji, label] = SPORTS[sid];
    const form = document.createElement("div");
    form.className = "rush-setup";
    form.dataset.sid = sid;
    const save = () => saveSetup(sid, setup);

    const head = document.createElement("div");
    head.className = "rush-setup-head";
    head.textContent = `${emoji} ${label}`;

    const days = document.createElement("div");
    days.className = "rush-setup-days";
    days.setAttribute("role", "group");
    days.setAttribute("aria-label", `${label}目标日期`);
    const dayButtons = ["今天", "明天", "后天"].map((text, offset) => {
      const b = document.createElement("button");
      b.type = "button"; b.textContent = text;
      b.addEventListener("click", () => { setup.offset = offset; save(); paintDays(); });
      return b;
    });
    const paintDays = () => dayButtons.forEach((b, i) => {
      b.classList.toggle("active", i === Number(setup.offset));
      b.setAttribute("aria-pressed", i === Number(setup.offset) ? "true" : "false");
    });
    days.append(...dayButtons);
    paintDays();

    const court = document.createElement("select");
    court.setAttribute("aria-label", `${label}场地`);
    court.append(option("0", `不限（全部 ${COURTS[sid]} 个）`, !Number(setup.court)));
    for (let n = 1; n <= COURTS[sid]; n++) court.append(option(String(n), `${n} 号场优先`, Number(setup.court) === n));
    court.addEventListener("change", () => { setup.court = Number(court.value); save(); });

    const times = document.createElement("div");
    times.className = "rush-setup-times";
    times.append(timeSelect(setup.start, 8 * 60, 21 * 60 + 30, `${label}开始时间`, (v) => { setup.start = v; save(); }),
      Object.assign(document.createElement("span"), { textContent: "–" }),
      timeSelect(setup.end, 8 * 60 + 30, 22 * 60, `${label}结束时间`, (v) => { setup.end = v; save(); }));

    const go = document.createElement("button");
    go.type = "button"; go.className = "primary rush-setup-go"; go.textContent = `开始抢${label}`;
    go.addEventListener("click", () => start([sid]));

    form.append(head, days, court, times, go);
    return form;
  }

  function confirmText(plans) {
    const mode = window.safetyDryRunNow?.() ? "测试模式：完整走一遍流程，但不会提交订单。" : "真实预约：抢到空场会直接提交订单。";
    const lines = plans.map((p) => {
      const [emoji, label] = SPORTS[p.sport_id];
      const courts = p.court_priority.length ? `${p.court_priority[0]} 号场优先，没空接着试其他场` : `全部 ${COURTS[p.sport_id]} 个场地`;
      return `${emoji} ${label}：${dayLabel(p.date)} ${p.date} ${p.slot_start}–${p.slot_end}（${(minutes(p.slot_end) - minutes(p.slot_start)) / 60} 小时整段），${courts}`;
    });
    return `确认开始抢场？\n\n${lines.join("\n")}\n放号时间：今天 ${plans[0].release_at}（北京时间）\n\n${mode}`;
  }

  // What became of a start whose reply was lost, read from the sport's state. Only the task made
  // by this very request (same request_id) counts: "started" (running), "finished" (it ran and
  // already ended), "unconfirmed" (the workbench holds no task from this request), "unknown"
  // (the state could not be read). Pure.
  function startReadback(plan, state) {
    if (!state || typeof state !== "object" || !plan.request_id) return "unknown";
    if ((state.plan || {}).request_id !== plan.request_id) return "unconfirmed";
    return state.running ? "started" : "finished";
  }

  const newRequestId = (sid) => `${sid}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  const OUTCOME_WORDS = { booked: "预约成功", simulated: "模拟完成", uncertain: "结果待核实", none: "没抢到" };

  // Send one start and say what happened. A lost reply is not "not started": the workbench may
  // have taken it, so the state is read back before anything is claimed.
  async function startOne(plan) {
    const label = SPORTS[plan.sport_id]?.[1] || plan.sport_id;
    // The page must know the workbench's mode and send the one the person confirmed.
    if (typeof plan.confirmed_dry_run !== "boolean") {
      window.toast?.(`${label}没有启动：还没读到工作台的预约模式`, "warn", 6000);
      return "refused";
    }
    plan = { ...plan, request_id: plan.request_id || newRequestId(plan.sport_id) };
    try {
      const r = await fetch("/api/auto/start", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(plan) });
      const d = await r.json();
      if (d.ok) { window.toast?.(`${label}抢场已启动`, "ok"); return "started"; }
      window.toast?.(`${label}没有启动：${d.msg || "原因未知"}`, "err", 6000);
      return "refused";
    } catch {
      let seen = "unknown", state = null;
      try {
        const r = await fetch(`/api/auto/state?sport_id=${encodeURIComponent(plan.sport_id)}`);
        if (r.ok) { state = await r.json(); seen = startReadback(plan, state); }
      } catch {}
      const word = state ? OUTCOME_WORDS[state.outcome] || "已结束" : "";
      const ended = !state ? "" : state.msg && state.msg.startsWith(word) ? state.msg : word + (state.msg ? `：${state.msg}` : "");
      window.toast?.(seen === "started" ? `${label}抢场已启动（启动回复没收到，查询确认这次的任务在运行）`
        : seen === "finished" ? `${label}抢场已启动并且已经结束（启动回复没收到）。结果：${ended}`
          : seen === "unconfirmed" ? `${label}没确认到这次启动：工作台里不是这次的任务。看卡片，没有${label}的倒计时再点一次`
            : `${label}启动结果待核实：没收到回复，也查不到状态。看卡片里有没有${label}的倒计时，没有再点一次`,
      seen === "started" ? "ok" : "warn", 9000);
      return seen;
    }
  }

  async function start(sids) {
    if (starting) return;
    if (!window.safetyModeKnown?.()) {
      window.toast?.("还没读到工作台现在是真实预约还是测试模式，等几秒再点（读不到时看看工作台是否在运行）", "warn", 6000);
      return;
    }
    const plans = sids.map((sid) => rushPlanFromSetup(sid, setups[sid], releaseAt()));
    const problem = rushSetupProblem(plans);
    if (problem) { window.toast?.(problem, "warn", 5000); return; }
    for (const p of plans) {
      if (minutes(p.slot_end) - minutes(p.slot_start) > 120
          && !window.confirm(`${SPORTS[p.sport_id][1]}选了 ${p.slot_start}–${p.slot_end}，一次预约 ${(minutes(p.slot_end) - minutes(p.slot_start)) / 60} 小时整段。确定没选错？`)) return;
    }
    const dry = window.safetyDryRunNow();  // the mode the confirmation shows is the one sent
    if (!window.confirm(confirmText(plans))) return;
    for (const p of plans) p.confirmed_dry_run = dry;
    starting = true;
    paintForms();
    try {
      for (const plan of plans) await startOne(plan);  // each sport gets its own answer
    } finally {
      starting = false;
      window.pollDashboardNow?.();
      paintForms();
    }
  }

  // A sport with a Rush running has no form; "两个一起开始" only when both can start.
  function paintForms() {
    const card = document.getElementById("rush-overview");
    if (!card) return;
    const box = card.querySelector(".rush-setup-list");
    if (!box) return;
    const idle = Object.keys(SPORTS).filter((sid) => !(lastBySport[sid] || {}).running);
    for (const sid of Object.keys(SPORTS)) {
      if (!forms[sid]) forms[sid] = buildForm(sid);
      const show = idle.includes(sid);
      if (show && !forms[sid].isConnected) box.append(forms[sid]);
      if (!show && forms[sid].isConnected) forms[sid].remove();
      forms[sid].querySelector(".rush-setup-go").disabled = starting;
    }
    // Keep badminton above tennis however they were shown.
    for (const sid of Object.keys(SPORTS)) if (forms[sid].isConnected) box.append(forms[sid]);
    const both = card.querySelector(".rush-setup-both");
    both.hidden = idle.length < 2;
    both.disabled = starting;
    card.querySelector(".rush-setup-title").hidden = !idle.length;
  }

  function mountSetup() {
    const card = document.getElementById("rush-overview");
    if (!card || card.querySelector(".rush-setup-list")) return;
    const title = Object.assign(document.createElement("p"), { className: "rush-setup-title",
      textContent: "在这里分别设置羽毛球和网球，可以同时抢。放号时间用下面「计划放场时间」。" });
    const box = Object.assign(document.createElement("div"), { className: "rush-setup-list" });
    const both = Object.assign(document.createElement("button"), { type: "button", className: "ghost rush-setup-both",
      textContent: "两个一起开始" });
    both.addEventListener("click", () => start(Object.keys(SPORTS)));
    card.append(title, box, both);
    card.hidden = false;
    paintForms();
  }

  function renderAll(bySport) {
    lastBySport = bySport || {};
    mountSetup();
    render(lastBySport);
    const card = document.getElementById("rush-overview");
    if (card) card.hidden = false;
    paintForms();
  }

  if (typeof document.addEventListener === "function") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => renderAll({}));
    else renderAll({});
  }

  window.rushOverviewRows = rushOverviewRows;
  window.rushPlanFromSetup = rushPlanFromSetup;
  window.rushStartReadback = startReadback;
  window.startRushPlan = startOne;
  window.rushSetupProblem = rushSetupProblem;
  window.renderRushOverview = renderAll;
})();
