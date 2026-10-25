const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'court-js-check-'));
try {
  for (const html of ['index.html', 'bridge.html']) {
    let count = 0;
    for (const match of fs.readFileSync(html, 'utf8').matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)) {
      if (!match[1].trim()) continue;
      const file = path.join(folder, `${html}-${++count}.js`);
      fs.writeFileSync(file, match[1]);
      execFileSync(process.execPath, ['--check', file], {stdio:'inherit'});
    }
    console.log(`${html}: ${count} inline script(s), node --check passed`);
  }
  for (const file of ['connect.js', 'connect-inject.js', 'sport_scope.js', 'control_ops.js', 'watch.js', 'onboarding.js']) {
    execFileSync(process.execPath, ['--check', file], {stdio:'inherit'});
  }
} finally {
  if (path.dirname(folder) !== path.resolve(os.tmpdir()) || !path.basename(folder).startsWith('court-js-check-')) {
    throw Error('Unexpected temporary directory');
  }
  fs.rmSync(folder, {recursive:true, force:true});
}
