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
assert.equal(ended.detail, '目标时段在查到的场地上一直是「不可约」：学校没放出这段（还没放号，或这段本来不开放），不是被别人抢完。');
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
assert.equal(chosen.sub, '7 号优先，共 10 个场地');
const [all] = rows({ badminton: { running: true, phase: 'scanning', courts: 10, first_courts: [1, 2, 3] } });
assert.equal(all.sub, '1、2、3 号优先，共 10 个场地');

console.log('Rush overview rows: waiting, ordering, not open, failed scans, uncertain, booked, plain and old endings read correctly.');
