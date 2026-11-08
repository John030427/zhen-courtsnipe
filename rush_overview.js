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
    target_not_open: "目标时段在查到的场地上一直是「不可约」：学校没开放这段，不是被别人抢完。",
    target_full: "目标时段在查到的场地上都已约满或不可约，没有出现过空位。",
  };

  function courtsText(st) {
    const first = st.first_courts || [];
    if (!st.courts) return "";
    return first.length && first.length < st.courts
      ? `${first.join("、")} 号优先，共 ${st.courts} 个场地` : `${st.courts} 个场地`;
  }

  // What the school said, in plain words: a page instead of data means no normal answer.
  const schoolSaid = (order) => (["non-json", "invalid-response"].includes(order.msg) || !order.msg
    ? "学校没有正常回复" : `学校回「${order.msg}」`);

  // One row per sport that has a Rush (running, or ended and not cleared); pure, for tests.
  function rushOverviewRows(bySport, today) {
    const rows = [];
    for (const [sid, st] of Object.entries(bySport || {})) {
      if (!st || (!st.running && (!st.phase || st.phase === "idle"))) continue;
      const [emoji, label] = SPORTS[sid] || ["", sid];
      const windows = (st.windows || []).map(([a, b]) => `${a}–${b}`).join("、");
      const row = { sid, title: `${emoji} ${label} ${dayLabel(st.date, today)} ${windows}`.trim(),
        sub: courtsText(st), pill: "", kind: "", detail: "", actions: [] };
      const order = st.last_order || {};
      if (st.running) {
        row.pill = ["scanning"].includes(st.phase) ? "扫描中" : st.phase === "ordering" ? "下单中"
          : st.release_in_sec ? `等待放号 · ${clock(st.release_in_sec)}` : "准备中";
        row.actions = ["stop"];
      } else if (st.outcome === "booked") {
        row.pill = "预约成功"; row.kind = "ok";
        row.detail = order.court ? `${order.court} 号场已约上。` : "";
      } else if (st.outcome === "simulated") {
        row.pill = "模拟完成"; row.kind = "ok";
      } else if (st.outcome === "uncertain") {
        row.pill = "结果待核实"; row.kind = "err";
        row.detail = `${order.court ? `${order.court} 号场已提交，` : ""}${schoolSaid(order)}`
          + `${order.reply_hint ? `（${order.reply_hint}）` : ""}。不知道约没约上：去企业微信「我的预约」核对，工作台不会自动重下。`;
      } else if (st.phase === "degraded") {
        row.pill = "已停止（需处理）"; row.kind = "err"; row.detail = st.msg || "";
      } else if (st.phase === "stopped") {
        row.pill = "已停止"; row.detail = "";
      } else {
        row.pill = "没抢到"; row.kind = "warn";
        row.detail = ENDINGS[st.reason] || (st.matches
          ? `找到过 ${st.matches} 次空场，但都没下成（被别人先约走、复查时已没了或验证码过期），详情看运行日志。`
          : "扫描时间内没有出现能约的空场。");
        if (ENDINGS[st.reason]) row.actions = ["watch"];
      }
      rows.push(row);
    }
    return rows;
  }

  let lastKey = "";

  function render(bySport) {
    const card = document.getElementById("rush-overview");
    if (!card) return;
    const rows = rushOverviewRows(bySport);
    card.hidden = !rows.length;
    if (!rows.length) return;
    const list = card.querySelector(".rush-overview-rows");
    // Rebuild only when something besides the countdown changed, so a button is not swapped out
    // under the pointer in the minutes before release; the countdown text is updated in place.
    const key = JSON.stringify(rows.map((row) => ({ ...row, pill: row.pill.startsWith("等待放号") ? "等待放号" : row.pill })));
    if (key === lastKey) {
      rows.forEach((row, i) => {
        const pill = list.children[i]?.querySelector(".rush-overview-pill");
        if (pill && pill.textContent !== row.pill) pill.textContent = row.pill;
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
      if (!r.ok) throw Error();
      window.toast?.(`${(SPORTS[sid] || ["", sid])[1]}抢场已停止`, "ok");
    } catch {
      window.toast?.("停止失败，请再试一次", "err");
    }
  }

  // Same sport, date and time range: the shared settings already hold them for that sport.
  function toWatch(sid) {
    showSport(sid);
    document.querySelector('[data-mode-tab="watch"]')?.click();
    window.toast?.("已切到持续监控：日期和时段沿用这次抢场，选好预约时长后点「开始监控」", "ok", 5000);
  }

  window.rushOverviewRows = rushOverviewRows;
  window.renderRushOverview = render;
})();
