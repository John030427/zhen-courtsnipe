const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('connect.js', 'utf8');
const start = source.indexOf('  function rushRecoveryGuidance(');
const end = source.indexOf("  el('official').onclick", start);
assert.ok(start >= 0 && end > start);
const context = {};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
const guidance = (reason, extra = {}) => context.rushRecoveryGuidance({phase:'degraded', reason, ...extra}, true);
for (const reason of ['result_uncertain', 'order_blocked', 'order_conflict', 'internal_error', 'vid_pool_failed']) {
  const view = guidance(reason);
  assert.doesNotMatch(view.button, /刷新登录|同步登录/);
  assert.match(view.feedback, /核对|运行日志/);
}
assert.match(guidance('result_uncertain').feedback, /不会清除订单待核实记录/);
assert.match(guidance('', {outcome:'uncertain'}).heading, /订单结果待核实/);
for (const reason of ['auth_expired', 'auth_refresh_failed', 'auth_check_timeout']) {
  assert.match(guidance(reason).button, /刷新登录/);
}
assert.match(context.rushRecoveryGuidance({phase:'waiting'}, false).button, /同步登录/);
console.log('Recovery guidance checks passed: order failures and login failures have distinct actions.');
