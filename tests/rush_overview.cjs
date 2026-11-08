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
assert.equal(ended.detail, '目标时段在查到的场地上一直是「不可约」：学校没开放这段，不是被别人抢完。');
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

const [lost] = rows({ tennis: { running: false, phase: 'exhausted', reason: 'scan_window_ended', matches: 2 } });
assert.match(lost.detail, /找到过 2 次空场，但都没下成/);

const [chosen] = rows({ badminton: { running: true, phase: 'scanning', courts: 10, first_courts: [7] } });
assert.equal(chosen.sub, '7 号优先，共 10 个场地');
const [all] = rows({ badminton: { running: true, phase: 'scanning', courts: 10, first_courts: [1, 2, 3] } });
assert.equal(all.sub, '1、2、3 号优先，共 10 个场地');

console.log('Rush overview rows: waiting, not open, uncertain, booked and plain endings read correctly.');
