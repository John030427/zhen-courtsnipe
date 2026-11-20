// Rows of the 「今晚的抢场」 card, from /api/dashboard's rush.by_sport. Pure function, no page.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const context = { window: {}, document: {} };
vm.createContext(context);
vm.runInContext(fs.readFileSync('rush_overview.js', 'utf8'), context);
// Rows come from another vm realm; copy them so strict comparisons see plain values.
const rows = (bySport) => JSON.parse(JSON.stringify(context.window.rushOverviewRows(bySport, '2030-06-01')));

assert.deepEqual(rows({ badminton: { running: false, phase: 'idle' } }), []);

const [waiting, ended] = rows({
  badminton: { running: true, phase: 'waiting', date: '2030-06-03', windows: [['16:00', '18:00']], courts: 10, release_in_sec: 151 },
  tennis: { running: false, phase: 'exhausted', reason: 'target_not_open', date: '2030-06-03', windows: [['20:00', '22:00']],
    msg: '目标时段在查到的场地上一直是「不可约」：学校没开放这段，不是被别人抢完。扫描结束：12 轮 / 60 次查询' },
});
assert.match(waiting.title, /🏸 羽毛球 后天（周一） 16:00–18:00/);
assert.equal(waiting.pill, '等待放号 · 2:31');
assert.deepEqual(waiting.actions, ['stop']);
assert.equal(ended.pill, '没抢到');
assert.equal(ended.detail, '目标时段在查到的场地上一直是「不可约」：具体原因未确认，不能据此判断是否被别人抢走。');
assert.deepEqual(ended.actions, ['watch']);

const [uncertain] = rows({ tennis: { running: false, phase: 'degraded', outcome: 'uncertain', date: '2030-06-02',
  windows: [['20:00', '22:00']], last_order: { court: 7, msg: 'non-json', reply_hint: 'HTTP 502 · nginx' } } });
assert.equal(uncertain.pill, '结果待核实');
assert.match(uncertain.detail, /7 号场已提交，学校没有正常回复（HTTP 502 · nginx）/);
assert.match(uncertain.detail, /我的预约/);
assert.deepEqual(uncertain.actions, []);

const [booked] = rows({ badminton: { running: false, phase: 'success', outcome: 'booked', date: '2030-06-01',
  windows: [['14:00', '16:00']], last_order: { court: 5, ok: true } } });
assert.equal(booked.pill, '预约成功');
assert.match(booked.title, /今天（周六）/);

const [plainEnd] = rows({ tennis: { running: false, phase: 'exhausted', reason: 'scan_window_ended', msg: '扫描结束：3 轮' } });
assert.equal(plainEnd.detail, '扫描时间内没有出现能约的空场。');
assert.deepEqual(plainEnd.actions, []);

const [lost] = rows({ tennis: { running: false, phase: 'exhausted', reason: 'scan_window_ended', matches: 2,
  last_order: { court: 3, ok: false, msg: '场地已被预约' } } });
assert.equal(lost.detail, '找到过 2 次空场，但都没下成；最后一次：场地已被预约。详情看运行日志。');

const [down] = rows({ tennis: { running: false, phase: 'exhausted', reason: 'scan_window_ended', queries: 12, query_errors: 9 } });
assert.match(down.detail, /^9\/12 次查询失败，没能确认有没有空场/);

const [ordering] = rows({ badminton: { running: true, phase: 'ordering', courts: 10 } });
assert.equal(ordering.pill, '下单中');
assert.deepEqual(ordering.actions, []);

const [offline] = rows({ tennis: { running: false, phase: 'degraded', outcome: 'uncertain',
  last_order: { court: 2, msg: '网络异常，请核对官方订单' } } });
assert.match(offline.detail, /2 号场已提交，本机没收到学校的回复（网络异常）/);

// Last night's ending is dropped the next day; an order still to be checked stays.
assert.deepEqual(rows({ tennis: { running: false, phase: 'exhausted', reason: 'target_full', finished_on: '2030-05-31' } }), []);
assert.equal(rows({ tennis: { running: false, phase: 'degraded', outcome: 'uncertain', finished_on: '2030-05-31' } }).length, 1);
assert.equal(rows({ tennis: { running: false, phase: 'exhausted', reason: 'target_full', finished_on: '2030-06-01' } }).length, 1);

const [chosen] = rows({ badminton: { running: true, phase: 'scanning', courts: 10, first_courts: [7] } });
assert.equal(chosen.sub, '7 号场优先，共 10 个场地');
const [all] = rows({ badminton: { running: true, phase: 'scanning', courts: 10, first_courts: [1, 2, 3] } });
assert.equal(all.sub, '1、2、3 号场优先，共 10 个场地');

// Several orders in one run: every booked court is named, a double booking and an open order are flagged.
const [two] = rows({ badminton: { running: false, phase: 'success', outcome: 'booked', booked_courts: [8, 9], doubled: true,
  last_order: { court: 9, ok: true } } });
assert.match(two.detail, /^8、9 号场已约上。同一时段约上了不止一个场/);
const [mixed] = rows({ badminton: { running: false, phase: 'success', outcome: 'booked', booked_courts: [9], open_courts: [8],
  last_order: { court: 9, ok: true } } });
assert.equal(mixed.detail, '9 号场已约上。另有 8 号场的订单结果没法确认，也请核对。');
const [open2] = rows({ tennis: { running: false, phase: 'degraded', outcome: 'uncertain', open_courts: [7, 6],
  last_order: { court: 6, msg: 'non-json' } } });
assert.match(open2.detail, /^7、6 号场已提交，学校没有正常回复。当前无法确认是否约上/);

// The replay: time from release, court, what happened; the closing step is left to the row text.
const [replayed] = rows({ tennis: { running: false, phase: 'exhausted', reason: 'scan_window_ended', timeline: [
  { t: -11, kind: 'peek', court: 7, text: '放号前看了一眼：时段在，没被占' },
  { t: 0.1, kind: 'order', court: 7, text: '订单已发出，学校没正常回复（HTTP 500）' },
  { t: 1.4, kind: 'verify', court: 7, text: '核对：还空着，这一单没约上' },
  { t: 60, kind: 'end', court: null, text: '扫描结束' }] } });
assert.deepEqual(replayed.replay, ['放号前 11 秒　7 号场　放号前看了一眼：时段在，没被占',
  '+0.1 秒　7 号场　订单已发出，学校没正常回复（HTTP 500）', '+1.4 秒　7 号场　核对：还空着，这一单没约上']);
assert.deepEqual(rows({ tennis: { running: true, phase: 'waiting' } })[0].replay, []);

console.log('Rush overview rows: waiting, ordering, not open, failed scans, uncertain, booked, plain and old endings, several orders and the replay read correctly.');

// Setting up both sports in the card: what /api/auto/start receives, and what stops a joint start.
const plan = (sid, setup) => JSON.parse(JSON.stringify(context.window.rushPlanFromSetup(sid, setup, '20:00:00', '2030-06-01')));
assert.deepEqual(plan('badminton', { offset: 2, court: 0, start: '16:00', end: '18:00' }),
  { sport_id: 'badminton', date: '2030-06-03', slot_start: '16:00', slot_end: '18:00', release_at: '20:00:00', court_priority: [] });
assert.deepEqual(plan('tennis', { offset: 1, court: 7, start: '20:00', end: '22:00' }).court_priority, [7]);
assert.equal(plan('tennis', {}).date, '2030-06-03');  // default: 后天 20:00–22:00, every court
const problem = (...plans) => context.window.rushSetupProblem(plans);
assert.equal(problem(plan('badminton', { start: '16:00', end: '18:00' }), plan('tennis', { start: '20:00', end: '22:00' })), '');
// Same day and hours for both sports is allowed; the confirmation warns that both may be booked.
const same = [plan('badminton', { start: '20:00', end: '22:00' }), plan('tennis', { start: '20:00', end: '22:00' })];
assert.equal(problem(...same), '');
assert.match(context.window.rushOverlapNote(same), /可能都约上/);
assert.equal(context.window.rushOverlapNote([same[0], plan('tennis', { offset: 1, start: '20:00', end: '22:00' })]), '');
assert.equal(problem(plan('badminton', { offset: 1, start: '20:00', end: '22:00' }), plan('tennis', { start: '20:00', end: '22:00' })), '');
assert.match(problem(plan('tennis', { start: '20:00', end: '20:00' })), /结束时间要晚于开始时间/);
console.log('Rush setup: plans and joint-start checks read correctly.');

// Before the first status reply the page must not claim test mode (a real order would follow).
assert.match(fs.readFileSync('index.html', 'utf8'), /\nlet safetyDryRun = false;/);
console.log('Page assumes real booking until the workbench reports its mode.');

// A start whose reply was lost is read back, and only the task this request made counts.
const readback = (plan, state) => context.window.rushStartReadback(plan, state);
const mine = { date: '2030-06-03', slot_start: '16:00', slot_end: '18:00', request_id: 'badminton-a1' };
const task = (running, id, extra = {}) => ({ running, plan: { date: '2030-06-03', windows: [{ start: '16:00', end: '18:00' }], request_id: id }, ...extra });
assert.equal(readback(mine, task(true, 'badminton-a1')), 'started');
assert.equal(readback(mine, task(false, 'badminton-a1', { outcome: 'booked' })), 'finished');  // ran and already booked
assert.equal(readback(mine, task(true, 'badminton-other')), 'unconfirmed');  // same day and time, another start
assert.equal(readback(mine, task(false, '')), 'unconfirmed');
assert.equal(readback(mine, null), 'unknown');
assert.equal(readback({ ...mine, request_id: '' }, task(true, '')), 'unknown');
// Only a 2xx reply with a true/false "ok" is final; a 502 with JSON, or a body without "ok", is read back.
const answered = (status, body) => context.window.rushStartAnswered(status, body);
assert.equal(answered(200, { ok: true }), true);
assert.equal(answered(200, { ok: false, msg: '已在运行' }), true);
assert.equal(answered(502, { detail: 'Bad Gateway' }), false);
assert.equal(answered(502, { ok: false }), false);
assert.equal(answered(200, { detail: 'x' }), false);
assert.equal(answered(200, null), false);
console.log('Lost start replies are read back.');

// Court order in the card: every court 1 → N, back to front, one court first, or a typed order.
const order = (sid, court, text = '') => plan(sid, { court, order: text }).court_priority;
assert.deepEqual(order('badminton', 'all'), []);
assert.deepEqual(order('badminton', 0), []);  // setups saved before this change
assert.deepEqual(order('badminton', 'reverse'), [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]);
assert.deepEqual(order('tennis', 'reverse'), [7, 6, 5, 4, 3, 2, 1]);
assert.deepEqual(order('badminton', '9'), [9]);
assert.deepEqual(order('badminton', 'priority', '9，8 10,9,11,x'), [9, 8, 10]);
assert.match(problem(plan('badminton', { court: 'priority', order: ' , ' })), /请先点场地号/);
const words = (sid, list) => context.window.rushCourtOrderText(sid, list);
assert.equal(words('badminton', []), '全部 10 个，从 1 号依次到 10 号');
assert.equal(words('badminton', [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]), '全部 10 个，从 10 号往前到 1 号');
assert.equal(words('badminton', [9]), '9 号场优先，没空接着试其他场');
assert.equal(words('badminton', [9, 8, 10]), '按 9 → 8 → 10 优先，没空接着试其他场');
console.log('Card court order: all, back to front, one court first and typed order read correctly.');

// New sports in the card: court counts, and basketball courts by the school's own names.
context.window.courtName = (sid, n) => sid === 'indoor_basketball'
  ? ({ 1: '1号场A场', 2: '1号场B场', 3: '2号场（半场）', 4: '3号场（半场）', 5: '1号场（全场）' })[n] : `${n}号场`;
assert.deepEqual(order('table_tennis', 'reverse'), [15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1]);
assert.equal(words('indoor_basketball', [1, 3]), '按 1号场A场 → 2号场（半场） 优先，没空接着试其他场');
assert.equal(words('indoor_basketball', [2]), '1号场B场优先，没空接着试其他场');
assert.equal(words('indoor_basketball', []), '全部 5 个，按列表顺序');
const [hoop] = rows({ indoor_basketball: { running: false, phase: 'success', outcome: 'booked', booked_courts: [1],
  last_order: { court: 1, ok: true }, timeline: [{ t: 1.2, kind: 'order', court: 1, text: '下单成功' }] } });
assert.match(hoop.title, /^🏀 室内篮球/);
assert.equal(hoop.detail, '1号场A场已约上。');
assert.deepEqual(hoop.replay, ['+1.2 秒　1号场A场　下单成功']);
const [pong] = rows({ table_tennis: { running: false, phase: 'success', outcome: 'booked', booked_courts: [12], last_order: { court: 12 } } });
assert.equal(pong.detail, '12 号场已约上。');
console.log('New sports: table tennis and basketball courts read correctly.');
assert.equal(words('indoor_basketball', [5]), '1号场（全场）优先，没空接着试其他场');
