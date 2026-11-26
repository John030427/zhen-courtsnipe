// 「正在监控」 rows, from /api/watch/states: every sport's watch in one card. Pure function, no page.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

// watch.js wires up its page on load; any element it asks for is an inert stand-in here.
const inert = () => new Proxy(function () {}, { get: (_t, k) => (k === Symbol.toPrimitive ? () => '' : inert()),
  apply: () => inert() });
const context = { window: {}, document: inert(), setInterval: () => 0, setTimeout: () => 0, fetch: async () => ({ ok: false }),
  console, Date, JSON, Math, Object, Array, Number, String, Error, Promise, RegExp };
vm.createContext(context);
vm.runInContext(fs.readFileSync('watch.js', 'utf8'), context);
const rows = (states) => JSON.parse(JSON.stringify(context.window.watchOverviewRows(states, '2030-06-01')));

const plan = (extra = {}) => ({ dates: ['2030-06-02'], courts: Array.from({ length: 15 }, (_, i) => i + 1),
  targets: [{ start: '14:00', end: '16:00', min_duration: 120 }], poll_interval: 20, auto_reserve: true, ...extra });

assert.deepEqual(rows({ badminton: { phase: 'idle', running: false, plan: null } }), []);

const [bad, tt] = rows({
  badminton: { running: true, phase: 'waiting', plan: plan({ start_at: '2030-06-01T19:00:00+08:00', courts: [9, 8] }), scans: 0 },
  tennis: { running: false, phase: 'idle', plan: null },
  table_tennis: { running: true, phase: 'watching', plan: plan({ auto_reserve: false }), scans: 12, found_rounds: 1 },
});
assert.equal(bad.title, '🏸 羽毛球 明天（周日）14:00–16:00');
assert.equal(bad.pill, '等待，19:00 开始');
assert.equal(bad.running, true);
assert.equal(tt.title, '🏓 乒乓球 明天（周日）14:00–16:00');
assert.equal(tt.pill, '监控中');
assert.equal(tt.sub, '15 个场地 · 每 20 秒查一次 · 已查 12 轮 · 发现空场 1 次 · 有连续 2 小时空场就提醒');

const [done, paused] = rows({
  indoor_basketball: { running: false, phase: 'success', plan: plan(), scans: 57, message: '预约成功，监控结束' },
  badminton: { running: true, phase: 'auth_paused', plan: plan(), scans: 3 },
});
assert.deepEqual([done.kind, done.pill, done.running, done.detail], ['ok', '预约成功', false, '预约成功，监控结束']);
assert.match(done.title, /🏀 室内篮球/);
assert.deepEqual([paused.kind, paused.pill], ['warn', '登录失效，暂停中']);

console.log('Watch overview: every sport\'s watch reads correctly.');
