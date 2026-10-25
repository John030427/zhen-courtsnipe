async (page) => {
  const base = new URL(page.url()).origin;
  if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(base)) throw Error('Use a local test server');
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  let category = 'none', phase = 'waiting', calls = 0;
  const when = '2030-06-01T19:58:00+08:00';
  const labels = {auth:'学校鉴权失败', query:'学校查询失败', network:'学校连接异常', none:'学校只读验证通过'};
  const schoolMsgs = {auth:'token expired', query:'not open', network:'', none:''};
  const codes = {auth:'401', query:'', network:'', none:''};
  const auth = () => ({token_set:true, userid:'demo', token_preview:'demo...', profile_ready:true,
    state:category==='none'?'valid':category==='auth'?'expired':'unknown', validated:category==='none',
    message:labels[category], last_validation_at:when, last_sync_at:when,
    validation:{error_kind:category, auth_fail:category==='auth', school_msg:schoolMsgs[category], code:codes[category]},
    validating:false});
  const rush = () => ({running:phase!=='degraded', phase, connection_mode:'page',
    revalidate_in_sec:120, release_in_sec:240,
    msg:labels[category]+'；订单已停止。去官方页切换场地同步 Token',
    plan:{date:'2030-06-03', release_at:'2030-06-01T20:00:06+08:00'}, applied:[]});
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    let data = {};
    if(url.pathname==='/api/connect/state') data={ready:true,profile_ready:true,script_connected:true,
      pairing:true,last_sync:when,auth:auth(),rush:rush()};
    else if(url.pathname==='/api/connect/test') {
      calls++;
      const a = auth();
      const codeText = codes[category] || '无';
      const school = schoolMsgs[category];
      const msg = category==='none' ? labels.none
        : `${labels[category]}${school ? ' · 学校：'+school : ''} · code：${codeText}`;
      data={ok:category==='none',msg,error_kind:category,auth_fail:category==='auth',
        school_msg:school, code:codes[category]||null, code_display:codeText, auth:a};
    }
    else if(url.pathname==='/api/config') data={order:{offset_days:2,token_set:true,token_preview:'demo...',target_date:'2030-06-03'},
      auth:auth(),auto:{slot_start:'21:00',slot_end:'22:00',refresh_at:'20:00:06'},safety:{dry_run:true},
      sports:{badminton:{id:'badminton',label:'羽毛球',matrix:true,watch:true,rush:true},
        tennis:{id:'tennis',label:'网球',matrix:false,watch:false,rush:false,stub_message:'网球尚未接入'}}};
    else if(url.pathname==='/api/dashboard') data={order_date:'2030-06-03',safety:{dry_run:true},token:auth(),rush:rush()};
    else if(url.pathname==='/api/auto/state') data=rush();
    else if(url.pathname==='/api/vid/state') data={count:0,running:false,stats:[]};
    else if(url.pathname==='/api/watch/state') data={running:false,phase:'idle',scans:0,attempts:0,matches:[],snapshots:{},events:[]};
    else if(url.pathname==='/api/logs') data={logs:[]};
    else if(url.pathname==='/api/matrix/state') data={by_date:{},current:null};
    else if(url.pathname==='/api/matrix/scan') data={ok:false,msg:'尚无可用场次',error_kind:'query'};
    else if(url.pathname==='/api/stream' || url.pathname.includes('/sse')) return route.fulfill({status:200,contentType:'text/event-stream',body:': mock\n\n'});
    await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(data)});
  });
  await page.setViewportSize({width:1440,height:1000});
  // The first-visit guide would cover the page; it is walked through separately at the end.
  await page.addInitScript(() => { try { localStorage.setItem('onboardingDone', '1'); } catch {} });
  await page.goto(base);
  const chipTitle = (label, extra='') => page.waitForFunction(([l, x]) => {
    const t = document.getElementById('chip-valid')?.title || '';
    return t.includes(l) && t.includes(x);
  }, [label, extra]);
  await page.locator('#chip-valid[data-state="ok"]').waitFor();
  await chipTitle('学校只读验证通过');
  await page.locator('#watch-summary').filter({hasText:'未启动'}).waitFor();
  if((await page.locator('#watch-message').textContent())?.includes('undefined')) throw Error('Unexpected Watch error');
  await page.locator('#connect-rush-detail').filter({hasText:'距复验 2分0秒'}).waitFor();
  for(const name of ['script','token','valid']) {
    if(await page.locator('#connect-step-'+name).getAttribute('data-state')!=='ok') throw Error('Wrong step: '+name);
  }
  await page.screenshot({path:'output/playwright/desktop-verified.png',fullPage:true});
  for(const kind of ['auth','query','network']) {
    category=kind; phase='degraded';
    await page.reload();
    await chipTitle(labels[kind], 'code：');
    const chipState = await page.locator('#chip-valid').getAttribute('data-state');
    if (chipState !== (kind === 'auth' ? 'error' : 'warn')) throw Error('Wrong validation chip state: ' + chipState);
    await page.locator('.app-nav [data-nav="settings"]').click();
    await page.getByRole('button',{name:'只读复验',exact:true}).click();
    await page.locator('#connect-test-state').filter({hasText:labels[kind]}).waitFor();
    await page.locator('#connect-test-state').filter({hasText:'code：'}).waitFor();
    await page.getByRole('button',{name:'去官方页切换场地同步 Token',exact:true}).click();
    if(!(await page.locator('#connect-rush').getAttribute('class')||'').includes('degraded')) throw Error('Missing degraded alert');
    if(!await page.locator('#connect-official-feedback').isVisible()) throw Error('Missing CTA feedback');
  }
  if(calls!==3) throw Error('Read-only checks not dispatched');
  await page.setViewportSize({width:390,height:844});
  await page.reload();
  await chipTitle('学校连接异常');
  const overflow = await page.evaluate(()=>document.documentElement.scrollWidth > innerWidth);
  if(overflow) throw Error('Mobile page overflows');
  await page.screenshot({path:'output/playwright/mobile-degraded.png',fullPage:true});
  // First-visit guide: reopened from 设置, every step has a visible card, and it closes cleanly.
  await page.locator('.app-nav [data-nav="settings"]').click();
  await page.locator('#btn-onboarding').click();
  const steps = await page.locator('.tour-dots span').count();
  for(let i=0;i<steps;i++) {
    const box = await page.locator('.tour-card').boundingBox();
    if(!box || box.y < 0 || box.y + box.height > 844 + 1) throw Error('Guide card off screen at step '+i);
    await page.locator('.tour-next').click();
  }
  if(await page.locator('.tour-card').count()) throw Error('Guide did not close');
  if(errors.length) throw Error(errors.join('\n'));
  console.log('Desktop/mobile: three steps, timestamps, countdown, three failure categories, read-only validation, degraded CTA, first-visit guide, no JS errors or mobile overflow.');
}
