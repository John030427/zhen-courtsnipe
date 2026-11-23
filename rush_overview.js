// 「今晚的抢场」: every sport's Rush in one card, whichever sport tab is open, so two Rushes can
// be followed and stopped side by side. Reads /api/dashboard's rush.by_sport; only the stop
// button sends anything, and only to this computer's workbench.
(() => {
  const SPORTS = { badminton: ["🏸", "羽毛球"], tennis: ["🎾", "网球"], table_tennis: ["🏓", "乒乓球"],
    indoor_basketball: ["🏀", "室内篮球"] };
  // Shown in the card from the start; the others are added with their own button.
  const MAIN_SPORTS = ["badminton", "tennis"];
  // A court as the school names it ("1号场A场"); "N号场" when nothing else is known.
  const courtName = (sid, n) => (typeof window.courtName === "function" && window.courtName(sid, n)) || `${n}号场`;
  // A sport whose courts are all "N号场" is shown by numbers; one with other names (basketball)
  // is shown by name throughout, so its "1号场" does not read as a bare "1".
  const numbered = (sid, n) => Array.from({ length: (COURTS_FOR_NAMES[sid] || 10) }, (_, i) => i + 1)
    .every((k) => courtName(sid, k) === `${k}号场`) && courtName(sid, n) === `${n}号场`;
  const COURTS_FOR_NAMES = { badminton: 10, tennis: 7, table_tennis: 15, indoor_basketball: 5 };
  // Several courts in one phrase: "8、9 号场" or "1号场A场、2号场（半场）". Pure.
  function courtsPhrase(sid, list) {
    const nums = (list || []).filter((n) => n !== undefined && n !== null && n !== "");
    if (!nums.length) return "";
    return nums.every((n) => numbered(sid, n)) ? `${nums.join("、")} 号场` : nums.map((n) => courtName(sid, n)).join("、");
  }
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

  function courtsText(st, sid = "badminton") {
    const first = st.first_courts || [];
    if (!st.courts) return "";
    return first.length && first.length < st.courts
      ? `${courtsPhrase(sid, first)}优先，共 ${st.courts} 个场地` : `${st.courts} 个场地`;
  }

  // What the school said, in plain words: a page instead of data means no normal answer, and a
  // network error means nothing came back to this computer at all.
  const schoolSaid = (order) => (String(order.msg || "").startsWith("网络异常") ? "本机没收到学校的回复（网络异常）"
    : ["non-json", "invalid-response"].includes(order.msg) || !order.msg
      ? "学校没有正常回复" : `学校回「${order.msg}」`);
  const mostlyFailed = (st) => st.queries > 0 && st.query_errors * 2 > st.queries;

  // The run as the engine saw it, one line per step: when (from release), which court, what.
  function replayLines(st, sid = "badminton") {
    return (st.timeline || []).filter((step) => step.kind !== "end").map((step) => {
      const t = typeof step.t === "number" ? (step.t < 0 ? `放号前 ${Math.round(-step.t)} 秒` : `+${step.t.toFixed(1)} 秒`) : "";
      return [t, step.court ? courtsPhrase(sid, [step.court]) : "", step.text].filter(Boolean).join("　");
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
        sub: courtsText(st, sid), pill: "", kind: "", detail: "", actions: [], replay: replayLines(st, sid) };
      const order = st.last_order || {};
      const booked = courtsPhrase(sid, (st.booked_courts || []).length ? st.booked_courts : [order.court]);
      const open = courtsPhrase(sid, (st.open_courts || []).length ? st.open_courts : [order.court]);
      if (st.running) {
        row.pill = ["scanning"].includes(st.phase) ? "扫描中" : st.phase === "ordering" ? "下单中"
          : st.release_in_sec ? `等待放号 · ${clock(st.release_in_sec)}` : "准备中";
        // Stopping halfway through an order cannot take it back; wait for its answer first.
        row.actions = st.phase === "ordering" ? [] : ["stop"];
      } else if (st.outcome === "booked") {
        row.pill = "预约成功"; row.kind = "ok";
        row.detail = (booked ? `${booked}已约上。` : "")
          + (st.doubled ? "同一时段约上了不止一个场：请到企业微信「我的预约」取消多余的。" : "")
          + ((st.open_courts || []).length ? `另有 ${open}的订单结果没法确认，也请核对。` : "");
      } else if (st.outcome === "simulated") {
        row.pill = "模拟完成"; row.kind = "ok";
      } else if (st.outcome === "uncertain") {
        row.pill = "结果待核实"; row.kind = "err";
        row.detail = `${open ? `${open}已提交，` : ""}${schoolSaid(order)}`
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
    const empty = card.querySelector(".rush-overview-empty");
    if (empty) empty.hidden = !!rows.length;
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
  const COURTS = { badminton: 10, tennis: 7, table_tennis: 15, indoor_basketball: 5 };
  const SETUP_KEY = (sid) => `rushSetup:${sid}`;
  // court: "all" (1 → N), "reverse" (N → 1), a court number (it first, then the rest), or
  // "priority" (the courts listed in `order` first). Older saved setups used 0 for "all".
  const DEFAULT_SETUP = { offset: 2, court: "all", order: "", start: "20:00", end: "22:00" };

  // The court order a setup asks for, as /api/auto/start's court_priority. Pure.
  function courtPriority(sid, court, order) {
    const total = COURTS[sid] || 10;
    if (court === "reverse") return Array.from({ length: total }, (_, i) => total - i);
    if (court === "priority") {
      const seen = new Set();
      return String(order || "").split(/[,，\s→>]+/).map(Number)
        .filter((n) => Number.isInteger(n) && n >= 1 && n <= total && !seen.has(n) && seen.add(n));
    }
    const n = Number(court);
    return Number.isInteger(n) && n >= 1 && n <= total ? [n] : [];
  }

  // The court order in words, for the confirmation. Pure.
  function courtOrderText(sid, list) {
    const total = COURTS[sid] || 10;
    const plain = Array.from({ length: total }, (_, i) => i + 1).every((n) => numbered(sid, n));
    if (!list.length) return plain ? `全部 ${total} 个，从 1 号依次到 ${total} 号` : `全部 ${total} 个，按列表顺序`;
    if (list.length === total && list.every((n, i) => n === total - i)) {
      return plain ? `全部 ${total} 个，从 ${total} 号往前到 1 号` : `全部 ${total} 个，按列表倒序`;
    }
    const names = list.map((n) => (plain ? String(n) : courtName(sid, n)));
    const head = list.length === 1 ? `${plain ? `${list[0]} 号场` : names[0]}优先` : `按 ${names.join(" → ")} 优先`;
    return list.length >= total ? `全部 ${total} 个，${head}` : `${head}，没空接着试其他场`;
  }
  const minutes = (hm) => { const [h, m] = String(hm).split(":").map(Number); return h * 60 + m; };
  const dayFromOffset = (offset, today = shanghaiDay()) =>
    new Date(Date.parse(`${today}T00:00:00Z`) + offset * 86400e3).toISOString().slice(0, 10);

  // What /api/auto/start receives for one sport's setup. Court 0 means every court in number
  // order; a chosen court is scanned first and then the rest, as in the main form. Pure.
  function rushPlanFromSetup(sid, setup, releaseAt, today = shanghaiDay()) {
    const s = { ...DEFAULT_SETUP, ...(setup || {}) };
    const missing = s.court === "priority" && !courtPriority(sid, s.court, s.order).length;
    return { sport_id: sid, date: dayFromOffset(Number(s.offset), today), slot_start: s.start, slot_end: s.end,
      ...(missing ? { court_order_missing: true } : {}),
      release_at: String(releaseAt || "20:00:00").trim() || "20:00:00", court_priority: courtPriority(sid, s.court, s.order) };
  }

  // Why these plans cannot start together, in words; "" when they can. Pure.
  function rushSetupProblem(plans) {
    for (const p of plans) {
      if (!(minutes(p.slot_end) > minutes(p.slot_start))) return `${SPORTS[p.sport_id][1]}：结束时间要晚于开始时间`;
      if (p.court_order_missing) return `${SPORTS[p.sport_id][1]}：选了「自己排顺序」，请先点场地号排好先后`;
    }
    return "";
  }

  // Badminton and tennis may be booked for the same hours; the confirmation says so. Pure.
  function rushOverlapNote(plans) {
    const overlap = plans.some((a, i) => plans.slice(i + 1).some((b) => a.date === b.date
      && minutes(a.slot_start) < minutes(b.slot_end) && minutes(b.slot_start) < minutes(a.slot_end)));
    return overlap ? "注意：有运动是同一天、时段重叠，可能都约上。用不上的请到企业微信「我的预约」取消。" : "";
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

  // Tap courts in the order wanted: each shows its place; a second tap takes it out. Courts not
  // tapped follow in number order. Stored as "9,8,10" in setup.order.
  function buildOrderPicker(sid, label, setup, save) {
    const box = document.createElement("div");
    box.className = "rush-order-picker";
    const hint = Object.assign(document.createElement("p"), { className: "rush-order-hint",
      textContent: "按想要的先后点场地号，再点一次取消。没点的场地排在后面。" });
    const grid = document.createElement("div");
    grid.className = "rush-order-grid";
    grid.setAttribute("role", "group");
    grid.setAttribute("aria-label", `${label}场地先后顺序`);
    const summary = Object.assign(document.createElement("p"), { className: "rush-order-summary" });
    const clear = Object.assign(document.createElement("button"), { type: "button", className: "ghost rush-order-clear",
      textContent: "清空" });
    const list = () => courtPriority(sid, "priority", setup.order);
    const buttons = [];
    const paint = () => {
      const picked = list();
      buttons.forEach((b, i) => {
        const place = picked.indexOf(i + 1);
        b.classList.toggle("picked", place >= 0);
        b.setAttribute("aria-pressed", place >= 0 ? "true" : "false");
        b.querySelector(".rush-order-place").textContent = place >= 0 ? String(place + 1) : "";
        b.setAttribute("aria-label", `${courtName(sid, i + 1)}${place >= 0 ? `，第 ${place + 1} 个` : ""}`);
      });
      const names = picked.map((n) => (numbered(sid, n) ? String(n) : courtName(sid, n)));
      summary.textContent = picked.length ? `顺序：${names.join(" → ")}${picked.length < (COURTS[sid] || 10) ? "，其余按列表顺序" : ""}`
        : "还没点场地";
      clear.hidden = !picked.length;
    };
    for (let n = 1; n <= (COURTS[sid] || 10); n++) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "rush-order-court";
      b.title = courtName(sid, n);
      const short = typeof window.courtShort === "function" ? window.courtShort(sid, n) : `${n}号`;
      b.append(Object.assign(document.createElement("span"), { textContent: numbered(sid, n) ? String(n) : short }),
        Object.assign(document.createElement("small"), { className: "rush-order-place" }));
      b.addEventListener("click", () => {
        const picked = list();
        const at = picked.indexOf(n);
        if (at >= 0) picked.splice(at, 1); else picked.push(n);
        setup.order = picked.join(",");
        save(); paint();
      });
      buttons.push(b);
    }
    clear.addEventListener("click", () => { setup.order = ""; save(); paint(); });
    grid.append(...buttons);
    const foot = document.createElement("div");
    foot.className = "rush-order-foot";
    foot.append(summary, clear);
    box.append(hint, grid, foot);
    paint();
    return box;
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
    if (!MAIN_SPORTS.includes(sid)) {
      const hide = Object.assign(document.createElement("button"), { type: "button", className: "ghost rush-setup-hide",
        textContent: "收起" });
      hide.addEventListener("click", () => setShown(sid, false));
      head.append(hide);
    }

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
    const picked = String(setup.court === 0 ? "all" : setup.court);
    const plain = Array.from({ length: COURTS[sid] }, (_, i) => i + 1).every((n) => numbered(sid, n));
    court.append(option("all", plain ? `全部（1→${COURTS[sid]}）` : `全部（${COURTS[sid]} 个）`, picked === "all"),
      option("reverse", plain ? `从后往前（${COURTS[sid]}→1）` : "全部，倒序", picked === "reverse"));
    for (let n = 1; n <= COURTS[sid]; n++) {
      court.append(option(String(n), numbered(sid, n) ? `${n} 号场优先` : `${courtName(sid, n)}优先`, picked === String(n)));
    }
    court.append(option("priority", "自己排顺序", picked === "priority"));
    const order = buildOrderPicker(sid, label, setup, save);
    order.hidden = picked !== "priority";
    court.addEventListener("change", () => {
      setup.court = court.value;
      order.hidden = court.value !== "priority";
      save();
    });

    const times = document.createElement("div");
    times.className = "rush-setup-times";
    times.append(timeSelect(setup.start, 8 * 60, 21 * 60 + 30, `${label}开始时间`, (v) => { setup.start = v; save(); }),
      Object.assign(document.createElement("span"), { textContent: "–" }),
      timeSelect(setup.end, 8 * 60 + 30, 22 * 60, `${label}结束时间`, (v) => { setup.end = v; save(); }));

    const go = document.createElement("button");
    go.type = "button"; go.className = "primary rush-setup-go"; go.textContent = `开始抢${label}`;
    go.addEventListener("click", () => start([sid]));

    form.append(head, days, court, order, times, go);
    return form;
  }

  function confirmText(plans) {
    const mode = window.safetyDryRunNow?.() ? "测试模式：完整走一遍流程，但不会提交订单。" : "真实预约：抢到空场会直接提交订单。";
    const lines = plans.map((p) => {
      const [emoji, label] = SPORTS[p.sport_id];
      const courts = courtOrderText(p.sport_id, p.court_priority);
      return `${emoji} ${label}：${dayLabel(p.date)} ${p.date} ${p.slot_start}–${p.slot_end}（${(minutes(p.slot_end) - minutes(p.slot_start)) / 60} 小时整段），${courts}`;
    });
    const note = rushOverlapNote(plans);
    return `确认开始抢场？\n\n${lines.join("\n")}\n放号时间：今天 ${plans[0].release_at}（北京时间）\n${note ? `\n${note}\n` : ""}\n${mode}`;
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

  // Whether a reply to /api/auto/start is the workbench's own answer. Pure.
  const startAnswered = (status, body) => status >= 200 && status < 300 && !!body && typeof body.ok === "boolean";

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
      const d = await r.json().catch(() => null);
      // Only the workbench's own answer is final: a success status with a true/false "ok". A 5xx
      // or a body without it (a gateway page, {"detail": ...}) may follow a start the workbench
      // took, so it is read back like a lost reply.
      if (!startAnswered(r.status, d)) throw new Error(`start reply HTTP ${r.status}`);
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
      window.toast?.(seen === "started" ? `${label}抢场已启动（没收到工作台的正常回复，查询确认这次的任务在运行）`
        : seen === "finished" ? `${label}抢场已启动并且已经结束（没收到工作台的正常回复）。结果：${ended}`
          : seen === "unconfirmed" ? `${label}没确认到这次启动：工作台里不是这次的任务。看卡片，没有${label}的倒计时再点一次`
            : `${label}启动结果待核实：没收到工作台的正常回复，也查不到状态。看卡片里有没有${label}的倒计时，没有再点一次`,
      seen === "started" ? "ok" : "warn", 9000);
      return seen;
    }
  }

  // Plans for these sports after the same checks and confirmation as a start; null if cancelled.
  function confirmedPlans(sids, intro = "确认开始抢场？") {
    if (!window.safetyModeKnown?.()) {
      window.toast?.("还没读到工作台现在是真实预约还是测试模式，等几秒再点（读不到时看看工作台是否在运行）", "warn", 6000);
      return null;
    }
    const plans = sids.map((sid) => rushPlanFromSetup(sid, setups[sid], releaseAt()));
    const problem = rushSetupProblem(plans);
    if (problem) { window.toast?.(problem, "warn", 5000); return null; }
    for (const p of plans) {
      if (minutes(p.slot_end) - minutes(p.slot_start) > 120
          && !window.confirm(`${SPORTS[p.sport_id][1]}选了 ${p.slot_start}–${p.slot_end}，一次预约 ${(minutes(p.slot_end) - minutes(p.slot_start)) / 60} 小时整段。确定没选错？`)) return null;
    }
    const dry = window.safetyDryRunNow();  // the mode the confirmation shows is the one sent
    if (!window.confirm(confirmText(plans).replace("确认开始抢场？", intro))) return null;
    for (const p of plans) {
      p.confirmed_dry_run = dry;
      p.request_id ||= newRequestId(p.sport_id);
    }
    return plans;
  }

  async function start(sids) {
    if (starting) return;
    const plans = confirmedPlans(sids);
    if (!plans) return;
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
  // Sports whose setup is shown in the card: badminton and tennis, plus any added by hand.
  const SHOWN_KEY = "rushSetupShown";
  function shownSports() {
    try {
      const saved = JSON.parse(localStorage.getItem(SHOWN_KEY) || "null");
      if (Array.isArray(saved)) return Object.keys(SPORTS).filter((sid) => saved.includes(sid) || MAIN_SPORTS.includes(sid));
    } catch {}
    return [...MAIN_SPORTS];
  }
  function setShown(sid, on) {
    const next = new Set(shownSports());
    if (on) next.add(sid); else next.delete(sid);
    try { localStorage.setItem(SHOWN_KEY, JSON.stringify([...next])); } catch {}
    paintForms();
  }

  // ── Timed start: the workbench checks the capture proxy and the login at the chosen time,
  // then starts these sports itself ──
  const SCHEDULE_AT_KEY = "rushScheduleAt";
  let lastSchedule = [];
  const pendingSports = () => new Set(lastSchedule.filter((i) => i.status === "pending")
    .flatMap((i) => i.plans.map((p) => p.sport_id)));

  async function schedule(sids) {
    if (starting || !sids.length) return;
    const at = (document.querySelector(".rush-schedule-at")?.value || "19:55").slice(0, 5);
    const plans = confirmedPlans(sids,
      `确认今天 ${at} 自动开始？到点工作台会先检查抓取代理（端口不通就重启）和登录，再开始抢。电脑和工作台要一直开着。`);
    if (!plans) return;
    starting = true;
    paintForms();
    try {
      const r = await fetch("/api/rush/schedule", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ at, plans }) });
      const d = await r.json().catch(() => null);
      if (r.ok && d?.ok) window.toast?.(`已设定：今天 ${at} 自动开始 ${plans.map((p) => SPORTS[p.sport_id][1]).join("、")}`, "ok", 6000);
      else window.toast?.(`没有设定成功：${d?.msg || `HTTP ${r.status}`}`, "err", 6000);
    } catch {
      window.toast?.("无法连接本机服务，定时开始没有设定", "err");
    } finally {
      starting = false;
      window.pollDashboardNow?.();
      paintForms();
    }
  }

  async function cancelSchedule(id) {
    if (!window.confirm("取消这个定时开始？")) return;
    try {
      const d = await (await fetch(`/api/rush/schedule/${encodeURIComponent(id)}/cancel`, { method: "POST" })).json();
      window.toast?.(d.ok ? "已取消定时开始" : "取消失败：它可能已经开始了", d.ok ? "ok" : "warn");
    } catch {
      window.toast?.("无法连接本机服务", "err");
    }
    window.pollDashboardNow?.();
  }

  // One line per timed start of today, for the card. Pure.
  function scheduleLines(items, today = shanghaiDay()) {
    const names = (item) => item.plans.map((p) => `${(SPORTS[p.sport_id] || ["", p.sport_id]).join(" ")}`.trim()).join("、");
    return (items || []).filter((i) => String(i.at).slice(0, 10) === today && i.status !== "cancelled").map((i) => {
      const at = String(i.at).slice(11, 16);
      if (i.status === "pending") return { id: i.id, kind: "", cancel: true,
        text: `⏰ ${at} 自动开始：${names(i)}（到点先检查抓取代理和登录）` };
      if (i.status === "preparing") return { id: i.id, kind: "", text: `⏰ ${at} 正在准备：${names(i)}` };
      const steps = (i.steps || []).map((s) => s.text).join("；");
      const kind = i.status === "started" ? "ok" : i.status === "partly" ? "warn" : "err";
      const head = i.status === "missed" ? `${at} 定时开始没有执行` : `${at} 定时开始`;
      return { id: i.id, kind, text: `${head}：${steps}` };
    });
  }

  function paintSchedule() {
    const box = document.querySelector("#rush-overview .rush-schedule-list");
    if (!box) return;
    const lines = scheduleLines(lastSchedule);
    const key = JSON.stringify(lines);
    if (box.dataset.key === key) return;
    box.dataset.key = key;
    box.replaceChildren(...lines.map((line) => {
      const row = Object.assign(document.createElement("div"), { className: `rush-schedule-line ${line.kind}` });
      row.append(Object.assign(document.createElement("span"), { textContent: line.text }));
      if (line.cancel) {
        const b = Object.assign(document.createElement("button"), { type: "button", className: "ghost", textContent: "取消" });
        b.addEventListener("click", () => cancelSchedule(line.id));
        row.append(b);
      }
      return row;
    }));
    box.hidden = !lines.length;
  }

  // A sport with a Rush running has no form; the joint start covers every shown idle sport.
  function paintForms() {
    const card = document.getElementById("rush-overview");
    if (!card) return;
    const box = card.querySelector(".rush-setup-list");
    if (!box) return;
    const shown = shownSports();
    // A sport waiting for a timed start shows that line instead of its form.
    const waiting = pendingSports();
    const idle = shown.filter((sid) => !(lastBySport[sid] || {}).running && !waiting.has(sid));
    for (const sid of Object.keys(SPORTS)) {
      if (!forms[sid]) forms[sid] = buildForm(sid);
      const show = idle.includes(sid);
      if (show && !forms[sid].isConnected) box.append(forms[sid]);
      if (!show && forms[sid].isConnected) forms[sid].remove();
      forms[sid].querySelector(".rush-setup-go").disabled = starting;
    }
    // Keep the sports in a fixed order however they were shown.
    for (const sid of Object.keys(SPORTS)) if (forms[sid].isConnected) box.append(forms[sid]);
    const adds = card.querySelector(".rush-setup-add");
    adds.replaceChildren(...Object.keys(SPORTS).filter((sid) => !shown.includes(sid)).map((sid) => {
      const b = Object.assign(document.createElement("button"), { type: "button", className: "ghost",
        textContent: `＋ ${SPORTS[sid][0]} ${SPORTS[sid][1]}` });
      b.addEventListener("click", () => setShown(sid, true));
      return b;
    }));
    adds.hidden = !adds.children.length;
    const both = card.querySelector(".rush-setup-both");
    both.hidden = idle.length < 2;
    both.textContent = idle.length === 2 ? "两个一起开始" : `${idle.length} 个一起开始`;
    both.disabled = starting;
    card.querySelector(".rush-setup-title").hidden = !idle.length;
    const timed = card.querySelector(".rush-schedule-row");
    timed.hidden = !idle.length;
    paintSchedulePick(timed, idle);
    paintSchedule();
  }

  // Which sports the timed start takes: ticked per sport, remembered on this computer.
  const SCHEDULE_PICK_KEY = "rushScheduleSports";
  function schedulePicked(idle) {
    try {
      const saved = JSON.parse(localStorage.getItem(SCHEDULE_PICK_KEY) || "null");
      if (Array.isArray(saved)) return idle.filter((sid) => saved.includes(sid));
    } catch {}
    return idle.slice(0, 1);  // first time: only the first sport, nothing ticked by surprise
  }
  function paintSchedulePick(timed, idle) {
    const pick = timed.querySelector(".rush-schedule-pick");
    const chosen = schedulePicked(idle);
    const key = idle.join(",") + "|" + chosen.join(",");
    if (pick.dataset.key !== key) {
      pick.dataset.key = key;
      pick.replaceChildren(...idle.map((sid) => {
        const label = document.createElement("label");
        const box = Object.assign(document.createElement("input"), { type: "checkbox", checked: chosen.includes(sid) });
        box.addEventListener("change", () => {
          let saved = [];
          try { saved = JSON.parse(localStorage.getItem(SCHEDULE_PICK_KEY) || "null") || schedulePicked(idle); } catch {}
          const next = new Set(saved);
          if (box.checked) next.add(sid); else next.delete(sid);
          try { localStorage.setItem(SCHEDULE_PICK_KEY, JSON.stringify([...next])); } catch {}
          paintForms();
        });
        label.append(box, document.createTextNode(` ${SPORTS[sid].join(" ")}`));
        return label;
      }));
    }
    const go = timed.querySelector(".rush-schedule-go");
    go.disabled = starting || !chosen.length;
    go.textContent = chosen.length ? `设定（${chosen.map((sid) => SPORTS[sid][1]).join("、")}）` : "先勾选运动";
  }

  function mountSetup() {
    const card = document.getElementById("rush-overview");
    if (!card || card.querySelector(".rush-setup-list")) return;
    const empty = Object.assign(document.createElement("p"), { className: "rush-overview-empty",
      textContent: "暂无抢场任务。开始后，这里会显示倒计时、进度和过程回放。" });
    card.querySelector(".rush-overview-rows")?.after(empty);
    const title = Object.assign(document.createElement("p"), { className: "rush-setup-title",
      textContent: "在这里分别设置各个运动，可以同时抢。放号时间在下面「放号时间和通知」里填。" });
    const box = Object.assign(document.createElement("div"), { className: "rush-setup-list" });
    const adds = Object.assign(document.createElement("div"), { className: "rush-setup-add" });
    const both = Object.assign(document.createElement("button"), { type: "button", className: "ghost rush-setup-both",
      textContent: "两个一起开始" });
    const idleShown = () => shownSports().filter((sid) => !(lastBySport[sid] || {}).running && !pendingSports().has(sid));
    both.addEventListener("click", () => start(idleShown()));
    const timed = Object.assign(document.createElement("div"), { className: "rush-schedule-row" });
    const atInput = Object.assign(document.createElement("input"), { type: "time", className: "rush-schedule-at" });
    atInput.setAttribute("aria-label", "定时开始的时间");
    try { atInput.value = localStorage.getItem(SCHEDULE_AT_KEY) || "19:55"; } catch { atInput.value = "19:55"; }
    atInput.addEventListener("change", () => { try { localStorage.setItem(SCHEDULE_AT_KEY, atInput.value); } catch {} });
    const go = Object.assign(document.createElement("button"), { type: "button", className: "ghost rush-schedule-go", textContent: "设定" });
    go.addEventListener("click", () => schedule(schedulePicked(idleShown())));
    const pick = Object.assign(document.createElement("div"), { className: "rush-schedule-pick" });
    pick.setAttribute("aria-label", "定时开始哪些运动");
    timed.append(Object.assign(document.createElement("span"), { textContent: "到点自动开始" }), atInput, pick, go,
      Object.assign(document.createElement("small"), { textContent: "到点先检查抓取代理（不通就重启）和登录，再开始抢；比一直挂着更省事。" }));
    const sched = Object.assign(document.createElement("div"), { className: "rush-schedule-list" });
    card.append(title, box, adds, both, timed, sched);
    card.hidden = false;
    paintForms();
  }

  // Court names arrive with the config after the forms were first built: build them again
  // (choices are saved on every change, so nothing typed is lost).
  window.addEventListener?.("sport-courts-changed", () => {
    for (const sid of Object.keys(forms)) {
      const old = forms[sid];
      forms[sid] = buildForm(sid);
      if (old.isConnected) old.replaceWith(forms[sid]);
    }
    paintForms();
  });

  function renderAll(bySport, scheduleItems) {
    lastBySport = bySport || {};
    if (Array.isArray(scheduleItems)) lastSchedule = scheduleItems;
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
  window.rushStartAnswered = startAnswered;
  window.startRushPlan = startOne;
  window.rushSetupProblem = rushSetupProblem;
  window.rushOverlapNote = rushOverlapNote;
  window.rushCourtOrderText = courtOrderText;
  window.rushScheduleLines = scheduleLines;
  window.renderRushOverview = renderAll;
})();
