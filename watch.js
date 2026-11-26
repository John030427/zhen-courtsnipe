(() => {
  const el = id => document.getElementById(`watch-${id}`);
  const labels = {idle:'未启动',waiting:'等待开始',scanning:'查询中',watching:'监控中',backoff:'稍后重试',refreshing_token:'恢复登录中',auth_paused:'登录失效，已暂停',reserving:'预约中',notify:'已提醒',success:'预约成功',uncertain:'结果未确认',exhausted:'没约到',completed:'监控结束',stopped:'已停止',error:'异常停止'};
  let busy = false, polling = false, running = false;
  const restoredBySport = {};  // per sport: form filled from its running plan

  window.watchRunningBySport = {};  // every sport has its own watch

  function activeSportId() {
    return window.courtActiveSportId?.() || "badminton";
  }

  function selectedWatchDates() {
    if (typeof window.courtSelectedDateIso === 'function') {
      const iso = window.courtSelectedDateIso();
      return iso ? [iso] : [];
    }
    return [];
  }

  window.courtWatchRunning = () => !!window.watchRunningBySport[activeSportId()];

  // Dates are absolute; the 今天 / 明天 / 后天 label is worked out on every render, so a watch
  // started for 后天 reads 明天 after midnight.
  const shanghaiDay = (ms = Date.now()) => new Date(ms + 8 * 3600e3).toISOString().slice(0, 10);
  function dayLabel(iso, today = shanghaiDay()) {
    const diff = Math.round((Date.parse(`${iso}T00:00:00Z`) - Date.parse(`${today}T00:00:00Z`)) / 86400e3);
    const rel = {0: '今天', 1: '明天', 2: '后天'}[diff];
    const [, m, d] = iso.split('-').map(Number);
    return rel || `${m}月${d}日`;
  }
  const weekday = iso => '周' + '日一二三四五六'[new Date(`${iso}T00:00:00Z`).getUTCDay()];
  const minutes = hm => { const [h, m] = String(hm).split(':').map(Number); return h * 60 + m; };
  const hm = m => `${String(m / 60 | 0).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
  const hours = m => `${m / 60} 小时`;
  function whenText(isoTime, today = shanghaiDay()) {
    const t = Date.parse(isoTime);
    const clock = new Date(t + 8 * 3600e3).toISOString().slice(11, 16);
    const day = shanghaiDay(t);
    return day === today ? clock : `${dayLabel(day, today)} ${clock}`;
  }
  const dryRun = () => typeof safetyDryRun !== 'undefined' && !!safetyDryRun;
  const reserveAllowed = () => !window.SportScope?.cap?.(activeSportId())?.notify_only;

  function slotRange() {
    const [start, end] = (el('targets').value.trim().split(/\n+/)[0] || '').split('-');
    return {start: (start || '').trim(), end: (end || '').trim()};
  }

  // Settings rows outside the fieldset follow its disabled state.
  function syncWatchControls() {
    const reserve = el('reserve').checked && reserveAllowed();
    el('reserve').checked = reserve;
    document.querySelectorAll('[data-watch-reserve]').forEach(btn => {
      const on = (btn.dataset.watchReserve === '1') === reserve;
      btn.classList.toggle('active', on);
      btn.setAttribute('aria-checked', on ? 'true' : 'false');
      btn.disabled = running || (btn.dataset.watchReserve === '1' && !reserveAllowed());
    });
    el('reserve-row').hidden = !reserve;
    el('reserve-text').textContent = dryRun()
      ? '测试模式：只模拟，不提交订单。'
      : '会直接提交订单（真实预约），约到一次就结束。';
    const dur = Number(el('duration').value);
    el('duration-hint').textContent = `时段内任意位置有连续 ${hours(dur)}空场就${reserve ? `预约这 ${hours(dur)}` : '提醒你'}`;
    const date = selectedWatchDates()[0];
    const {end} = slotRange();
    el('span').options[0].textContent = date && end ? `到目标时段结束（${dayLabel(date)} ${end}）` : '到目标时段结束';
    el('at-row').hidden = el('span').value !== 'later';
    for (const id of ['duration', 'attempts', 'span', 'at']) el(id).disabled = running;
  }
  window.syncWatchControls = syncWatchControls;

  function courtsText(courts) {
    return courts.length === 1 ? `${courts[0]} 号场` : `${courts.length} 个场地`;
  }

  // 「正在监控」: one row per sport whose watch is running or has ended this session. Pure.
  const SPORT_NAMES = { badminton: ['🏸', '羽毛球'], tennis: ['🎾', '网球'], table_tennis: ['🏓', '乒乓球'],
    indoor_basketball: ['🏀', '室内篮球'] };
  const PILLS = {waiting: ['', '等待开始'], scanning: ['', '监控中'], watching: ['', '监控中'], reserving: ['', '预约中'],
    refreshing_token: ['warn', '恢复登录中'], auth_paused: ['warn', '登录失效，暂停中'], backoff: ['warn', '稍后重试'],
    success: ['ok', '预约成功'], notify: ['ok', '已提醒'], completed: ['', '监控结束'], stopped: ['', '已停止'],
    exhausted: ['warn', '没约到'], uncertain: ['err', '结果待核实'], error: ['err', '异常停止']};
  function watchOverviewRows(states, today = shanghaiDay()) {
    return Object.entries(states || {}).filter(([, st]) => st && st.plan && st.phase && st.phase !== 'idle')
      .map(([sid, st]) => {
        const p = st.plan, t = p.targets[0], date = p.dates[0];
        const [emoji, name] = SPORT_NAMES[sid] || ['', sid];
        let [kind, pill] = PILLS[st.phase] || ['', st.phase];
        if (st.running && st.phase === 'waiting' && p.start_at) pill = `等待，${whenText(p.start_at, today)} 开始`;
        const mode = p.auto_reserve ? `自动预约 ${hours(t.min_duration)}` : `有连续 ${hours(t.min_duration)}空场就提醒`;
        const sub = [courtsText(p.courts), `每 ${p.poll_interval} 秒查一次`, `已查 ${st.scans || 0} 轮`,
          `发现空场 ${st.found_rounds || 0} 次`, mode].join(' · ');
        return {sid, running: !!st.running, kind, pill,
          title: `${emoji} ${name} ${dayLabel(date, today)}（${weekday(date)}）${t.start}–${t.end}`, sub,
          detail: st.running ? '' : (st.message || '')};
      });
  }
  window.watchOverviewRows = watchOverviewRows;

  function sportTabs() {
    return [...document.querySelectorAll('.sport-tab[data-sport-id]')].map(tab => tab.dataset.sportId);
  }
  function showSport(sid) {
    if (activeSportId() !== sid) document.querySelector(`.sport-tab[data-sport-id="${sid}"]`)?.click();
    setTimeout(poll, 0);
  }
  async function stopSport(sid) {
    const name = (SPORT_NAMES[sid] || ['', sid])[1];
    if (!confirm(`停止${name}监控？`)) return;
    try {
      const r = await fetch('/api/watch/stop', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({sport_id: sid})});
      if (!r.ok) throw Error();
      window.toast?.(`${name}监控已停止`, 'ok');
    } catch { window.toast?.('停止失败，请再试一次', 'err'); }
    poll();
  }
  let overviewKey = '';
  function paintOverview(states) {
    const card = document.getElementById('watch-overview');
    if (!card) return;
    const rows = watchOverviewRows(states);
    const active = activeSportId();
    const key = JSON.stringify([rows, active, sportTabs()]);
    if (key === overviewKey) return;   // rebuilt only on change, so a button is not swapped under the pointer
    overviewKey = key;
    card.querySelector('.watch-overview-empty').hidden = rows.length > 0;
    const list = card.querySelector('.watch-overview-rows');
    list.hidden = !rows.length;
    list.replaceChildren(...rows.map(row => {
      const el = document.createElement('div');
      el.className = 'rush-overview-row';
      const main = document.createElement('div');
      main.className = 'rush-overview-main';
      main.append(Object.assign(document.createElement('span'), {className: 'rush-overview-title', textContent: row.title}),
        Object.assign(document.createElement('span'), {className: `rush-overview-pill ${row.kind}`, textContent: row.pill}));
      for (const text of [row.sub, row.detail]) {
        if (text) main.append(Object.assign(document.createElement('span'), {className: 'rush-overview-sub', textContent: text}));
      }
      const acts = document.createElement('div');
      acts.className = 'rush-overview-actions';
      const look = Object.assign(document.createElement('button'), {type: 'button', className: 'ghost',
        textContent: row.sid === active ? '正在看' : '查看'});
      look.disabled = row.sid === active;
      look.addEventListener('click', () => showSport(row.sid));
      acts.append(look);
      if (row.running) {
        const stopBtn = Object.assign(document.createElement('button'), {type: 'button', className: 'ghost', textContent: '停止'});
        stopBtn.addEventListener('click', () => stopSport(row.sid));
        acts.append(stopBtn);
      }
      el.append(main, acts);
      return el;
    }));
    const pick = card.querySelector('.watch-sport-pick');
    pick.querySelectorAll('button').forEach(b => b.remove());
    for (const sid of sportTabs()) {
      const [emoji, name] = SPORT_NAMES[sid] || ['', sid];
      const btn = Object.assign(document.createElement('button'), {type: 'button', textContent: `${emoji} ${name}`});
      btn.classList.toggle('active', sid === active);
      btn.setAttribute('aria-pressed', sid === active ? 'true' : 'false');
      if (states?.[sid]?.running) btn.append(Object.assign(document.createElement('span'), {className: 'watch-sport-dot', textContent: '● 监控中'}));
      btn.addEventListener('click', () => showSport(sid));
      pick.append(btn);
    }
  }

  function summaryCard(state) {
    const card = el('summary');
    const p = state.plan;
    const found = state.found_rounds || 0;
    if (!p || state.phase === 'idle') {
      card.dataset.state = 'idle';
      card.textContent = '未启动';
      return;
    }
    const date = p.dates[0];
    const t = p.targets[0];
    const what = `${dayLabel(date)}（${weekday(date)}）${t.start}–${t.end}`;
    const mode = p.auto_reserve ? `自动预约 ${hours(t.min_duration)}` : `有连续 ${hours(t.min_duration)}空场就提醒`;
    const head = document.createElement('b');
    head.append(Object.assign(document.createElement('span'), {className: 'dot'}));
    const sub = document.createElement('span');
    sub.className = 'sub';
    let line;
    if (state.running && state.phase === 'auth_paused') {
      card.dataset.state = 'paused';
      head.append('登录失效，已暂停查询');
      line = ` ${what}`;
      const retry = state.auth_retry_in_sec;
      sub.textContent = `${retry != null ? `${retry} 秒后` : '稍后'}自动重试恢复 · 也可以在企业微信里刷新预约页 · 恢复后接着查`;
    } else if (state.running) {
      card.dataset.state = 'running';
      const waiting = state.phase === 'waiting' && p.start_at;
      head.append(waiting ? '等待定时开始' : '正在监控');
      line = ` ${what} · ${courtsText(p.courts)} · 每 ${p.poll_interval} 秒查一次`;
      const next = state.next_scan_at ? Math.max(0, Math.round((Date.parse(state.next_scan_at) - Date.now()) / 1000)) : null;
      const parts = waiting ? [`${whenText(p.start_at)} 开始`]
        : [next != null && state.phase !== 'scanning' ? `下次查询 ${next} 秒后` : '查询中…'];
      parts.push(`已查 ${state.scans} 轮`, `发现空场 ${found} 次`, mode);
      if (state.ends_at) parts.push(`到 ${whenText(state.ends_at)} 自动结束`);
      sub.textContent = parts.join(' · ');
    } else {
      card.dataset.state = ['success', 'completed', 'notify'].includes(state.phase) ? 'done'
        : ['uncertain', 'error', 'exhausted'].includes(state.phase) ? 'paused' : 'idle';
      head.append(labels[state.phase] || state.phase);
      line = ` ${what}`;
      sub.textContent = `已查 ${state.scans} 轮 · 发现空场 ${found} 次 · ${mode}`;
    }
    card.replaceChildren(head, line, sub);
  }

  function freeWindows(blocks, target) {
    const t0 = minutes(target.start), t1 = minutes(target.end);
    const starts = blocks.filter(b => String(b.status) === '1' && minutes(b.endTime) - minutes(b.time) === 30)
      .map(b => minutes(b.time)).filter(s => s >= t0 && s + 30 <= t1).sort((a, b) => a - b);
    const out = [];
    for (const s of starts) {
      const last = out[out.length - 1];
      if (last && last[1] === s) last[1] += 30; else out.push([s, s + 30]);
    }
    return out;
  }

  function resultRow(s, target) {
    const row = document.createElement('tr');
    const free = s.ok && target ? freeWindows(s.blocks, target) : [];
    const cells = [dayLabel(s.date), `${s.court}号场`,
      s.ok ? free.map(([a, b]) => `${hm(a)}–${hm(b)}`).join('、') || '—' : `查询失败：${s.error || '未知'}`,
      new Date(s.time).toLocaleTimeString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false})];
    cells.forEach((text, i) => {
      const cell = document.createElement('td');
      cell.textContent = text;
      if (i === 2) cell.className = !s.ok ? 'watch-err' : free.some(([a, b]) => b - a >= target.min_duration) ? 'watch-free' : '';
      row.append(cell);
    });
    return row;
  }

  function render(state) {
    const sportId = state.sport_id || activeSportId();
    window.watchRunningBySport[sportId] = !!state.running;
    const sportOk = typeof window.sportWatchEnabled === "function" ? window.sportWatchEnabled() : true;
    const thisSport = sportId === activeSportId();
    if (thisSport) {
      el('start').disabled = state.running || !window.courtConnectionReady || !sportOk;
      el('start').textContent = state.running ? '▶ 监控中…' : '▶ 开始监控';
      el('stop').disabled = !state.running;
    }
    window.SportScope?.applyCapabilityChrome?.();
    if (!thisSport) return;
    running = !!state.running;
    el('fields').disabled = running;
    // The paused card already says what is going on.
    if (state.message) el('message').textContent = state.phase === 'auth_paused' && state.running ? '' : state.message;
    if (!restoredBySport[sportId] && state.plan) {
      const p = state.plan;
      el('courts').value = p.courts.join(',');
      el('targets').value = p.targets.map(t => `${t.start}-${t.end}`).join('\n');
      if ([...el('duration').options].some(o => o.value === String(p.targets[0].min_duration))) el('duration').value = p.targets[0].min_duration;
      el('interval').value = p.poll_interval;
      el('span').value = p.start_at ? 'later' : p.continuous ? 'target'
        : [...el('span').options].some(o => o.value === String(p.duration_minutes)) ? String(p.duration_minutes) : '1440';
      if (p.start_at) el('at').value = new Date(Date.parse(p.start_at) + 8 * 3600e3).toISOString().slice(0, 16);
      el('attempts').value = p.max_attempts;
      el('reserve').checked = reserveAllowed() && p.auto_reserve;
      restoredBySport[sportId] = true;
    }
    syncWatchControls();
    summaryCard(state);
    el('matches').textContent = state.matches.length ? '找到：' + state.matches.slice(0, 5).map(m => `${dayLabel(m.date)} ${m.court}号场 ${m.start}–${m.end}`).join(' / ') : '';
    const target = state.plan?.targets?.[0];
    el('results').replaceChildren(...Object.values(state.snapshots).map(s => resultRow(s, target)));
  }
  async function poll() {
    if (busy || polling) return;
    polling=true;
    try {
      const sportId = activeSportId();
      const r = await fetch('/api/watch/state?sport_id=' + encodeURIComponent(sportId));
      if (!r.ok) throw Error('状态查询失败');
      render(await r.json());
      const all = await fetch('/api/watch/states');
      if (all.ok) {
        const states = await all.json();
        for (const [sid, st] of Object.entries(states)) {
          window.watchRunningBySport[sid] = !!st.running;
        }
        paintOverview(states);
      }
    }
    catch (e) { el('message').textContent = e.message; }
    finally {polling=false;}
  }

  document.querySelectorAll('[data-watch-reserve]').forEach(btn => btn.addEventListener('click', () => {
    el('reserve').checked = btn.dataset.watchReserve === '1';
    syncWatchControls();
  }));
  for (const id of ['duration', 'span']) el(id).addEventListener('change', syncWatchControls);
  for (const id of ['op-slot-start', 'op-slot-end']) document.getElementById(id)?.addEventListener('change', syncWatchControls);
  document.querySelectorAll('.control-day-tab[data-control-offset], .matrix-day-tab').forEach(btn =>
    btn.addEventListener('click', () => setTimeout(syncWatchControls, 0)));

  el('form').addEventListener('submit', async event => {
    event.preventDefault();
    const sportId = activeSportId();
    if (typeof window.sportWatchEnabled === "function" && !window.sportWatchEnabled()) {
      const cap = window.SportScope?.cap?.(sportId);
      el('message').textContent = cap?.stub_message || "该运动尚未接入，无法启动监控";
      return;
    }
    busy = true; el('start').disabled = true;
    try {
      window.syncWatchTargetsFromSlots?.();
      window.applyCourtScopeFromShell?.();
      const dates = selectedWatchDates();
      if (!dates.length) throw Error('请先选择今天 / 明天 / 后天');
      const {start, end} = slotRange();
      const duration = Number(el('duration').value);
      if (!start || !end || minutes(end) - minutes(start) < duration)
        throw Error(`目标时段 ${start}–${end} 不够 ${hours(duration)}，请放宽时段或缩短预约时长`);
      const nowHm = new Date(Date.now() + 8 * 3600e3).toISOString().slice(11, 16);
      if (dates[0] === shanghaiDay() && minutes(end) - duration < minutes(nowHm))
        throw Error('这个时段今天已经来不及了，请换个时段或日期');
      const span = el('span').value;
      if (span === 'later' && !el('at').value) throw Error('请选择开始时间');
      const autoReserve = reserveAllowed() && el('reserve').checked;
      if (autoReserve && !dryRun() && !confirm(
        `发现空场会直接提交订单（真实预约）：\n${dayLabel(dates[0])}（${weekday(dates[0])}）${start}–${end} 之间的 ${hours(duration)}\n` +
        `约到一次就结束，最多下单 ${el('attempts').value} 次。确定开始监控？`)) return;
      const plan = {
        sport_id: sportId,
        dates,
        courts:el('courts').value.split(/[,，\s]+/).filter(Boolean).map(Number),
        targets:[{start, end, min_duration: duration}],
        poll_interval:Number(el('interval').value),
        duration_minutes: /^\d+$/.test(span) ? Number(span) : 30,
        continuous: span === 'target' || span === 'later',
        max_attempts:Number(el('attempts').value),
        auto_reserve: autoReserve,
        start_at: span === 'later' ? `${el('at').value}:00+08:00` : null
      };
      const r = await fetch('/api/watch/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(plan)});
      const d = await r.json();
      if (!r.ok || !d.ok) throw Error(d.msg || (d.detail || []).map(x => `${x.loc.slice(1).join('.')}: ${x.msg}`).join('; ') || '启动失败');
      el('message').textContent = '监控已启动';
      restoredBySport[sportId] = false;
    } catch(e) { el('message').textContent = e.message; }
    finally { busy = false; el('start').disabled = false; poll(); }
  });
  el('stop').addEventListener('click', async () => {
    try {
      const sportId = activeSportId();
      const r = await fetch('/api/watch/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sport_id:sportId})});
      if (!r.ok) throw Error('停止失败');
      await poll();
    }
    catch(e) { el('message').textContent = e.message; }
  });
  syncWatchControls();
  poll(); setInterval(poll, 2000);
})();
