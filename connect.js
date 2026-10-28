(() => {
  const el = id => document.getElementById('connect-'+id);
  let code='', busy=false, testing=false, polling=false, recoverySaving=false;
  const stamp = value => value ? new Date(value).toLocaleString('zh-CN',{hour12:false}) : '尚无记录';
  const duration = seconds => {
    if (seconds == null || Number.isNaN(seconds)) return '未知';
    const s = Math.max(0, Math.floor(seconds));
    return `${Math.floor(s / 60)}分${s % 60}秒`;
  };
  const kindLabels = {
    auth: '学校鉴权失败',
    query: '学校查询失败',
    network: '学校连接异常',
    config: '验证配置不完整',
    internal: '本机验证异常',
    unverified: '尚未验证',
  };
  function formatAuthValidation(auth) {
    if (!auth) return '等待登录信息';
    if (auth.validated) return auth.message || '学校只读验证通过';
    const v = auth.validation || {};
    const kind = v.error_kind || '';
    if (!kind || kind === 'none') return auth.message || '等待登录信息';
    const head = kindLabels[kind] || auth.message || '学校查询失败';
    const school = (v.school_msg || '').trim();
    const codeVal = v.code;
    const codeText = codeVal != null && codeVal !== '' ? String(codeVal) : '无';
    const parts = [head];
    if (school) parts.push(`学校：${school}`);
    parts.push(`code：${codeText}`);
    return parts.join(' · ');
  }
  window.formatAuthValidation = formatAuthValidation;
  const clock = value => value ? new Date(value).toLocaleTimeString('zh-CN',{hour12:false,hour:'2-digit',minute:'2-digit'}) : '';
  // Short chip text; the full validation message stays in the chip tooltip.
  function validationChip(s, auth) {
    const kind = (auth.validation || {}).error_kind || '';
    if (!s.ready) return ['idle', '未接入'];
    if (auth.validating) return ['busy', '验证中…'];
    if (auth.validated) return ['ok', `通过${auth.last_validation_at ? ' · ' + clock(auth.last_validation_at) : ''}`];
    if (kind === 'auth') return ['error', '鉴权失败'];
    if (kind && kind !== 'none') return ['warn', {query:'查询失败', network:'连接异常', config:'配置不完整', internal:'本机异常'}[kind] || '验证失败'];
    return ['warn', '未验证'];
  }
  function step(id, state, text) {
    el('step-'+id).dataset.state=state;
    el(id === 'valid' ? 'valid-detail' : id+'-detail').textContent=text;
  }
  function formatClashCapture(cc) {
    if (!cc || !cc.enabled) return '自动获取登录：未配置（推荐运行「配置自动获取」一键脚本，登录过期可自动续上）';
    const base = 'Clash 代理捕获：' + (cc.label || '未就绪');
    if (cc.status === 'rejected') {
      return base + ' · ' + (cc.last_reject_reason || '工作台未采用这次抓到的登录信息') + ' · ' + stamp(cc.last_reject_at);
    }
    if (cc.status === 'captured' && cc.last_capture_at) {
      return base + ' · ' + stamp(cc.last_capture_at) + ' · 与官方页脚本互补';
    }
    if (cc.status === 'listening') {
      return base + ' · 在企业微信里刷新一次预约页即可抓到';
    }
    if (cc.port_conflict) return base + ' · 8080 被其他进程占用';
    return base + ' · 需 Clash 规则指向 127.0.0.1:8080 且 mitm CA 已信任';
  }
  el('official').onclick=()=>{el('official-feedback').hidden=false;};
  el('auto-recovery').onchange=async()=>{
    recoverySaving=true; el('auto-recovery').disabled=true;
    try { await request('/api/connect/recovery',{enabled:el('auto-recovery').checked}); }
    catch(e) { el('recovery-state').textContent=e.message; }
    finally { recoverySaving=false;el('auto-recovery').disabled=false;await poll(); }
  };
  async function request(url, body) {
    const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body || {})});
    const data=await r.json(); if(!r.ok) throw Error(data.detail || '请求失败'); return data;
  }
  el('copy').onclick=async()=>{
    if(busy) return; busy=true; el('copy').disabled=true;
    try {
      if(!code){ const d=await request('/api/connect/script');code=d.script;el('code').value=code; }
      try { await navigator.clipboard.writeText(code); el('feedback').textContent='已复制。在企业微信按 Ctrl+Shift+Alt+D 开启调试模式，预约页右键“开发者工具”→ Console 粘贴运行。'; }
      catch { el('instructions').open=true;el('code').focus();el('code').select();el('feedback').textContent='浏览器未允许自动复制，脚本已选中，请按 Ctrl+C。'; }
      el('instructions').open=true;
    } catch(e) {el('feedback').textContent=e.message;}
    finally{busy=false;el('copy').disabled=false;}
  };
  el('help').onclick=()=>{el('instructions').open=!el('instructions').open;};
  el('revoke').onclick=async()=>{
    try{await request('/api/connect/revoke');code='';el('code').value='';el('feedback').textContent='配对已停用。已保存的本机登录信息仍保留。';await poll();}
    catch(e){el('feedback').textContent=e.message;}
  };
  el('test').onclick=async()=>{
    if(testing) return; testing=true;
    el('test').disabled=true;el('test-state').textContent='正在查询一个场地…';
    try{const d=await request('/api/connect/test');el('test-state').textContent=d.msg || formatAuthValidation(d.auth);}
    catch(e){el('test-state').textContent=e.message;}
    finally{testing=false;await poll();}
  };
  async function poll(){
    if(polling) return;
    polling=true;
    try{
      const r=await fetch('/api/connect/state'); if(!r.ok) throw Error('本机服务不可用');const s=await r.json();
      window.courtConnectionReady=s.ready;
      const auth=s.auth||{};
      el('account-switch').textContent=s.account_switch?.locked
        ?'账号已锁定：任务运行期间只同步当前账号。任务结束后，在目标账号的官方预约页刷新并进入具体场地即可切换。'
        :'自动切换账号：Clash 保持规则模式，在目标企业微信账号中重新打开或刷新预约页并进入具体场地即可同步。多个账号同时打开时，以最近采集到的账号为准；请只保留要使用的预约页。';
      const recovery=s.auto_recovery||{};
      if(!recoverySaving) el('auto-recovery').checked=!!recovery.enabled;
      el('recovery-state').textContent=(recovery.enabled?'自动恢复已启用':'自动恢复未启用')+(recovery.message?' · '+recovery.message:'');
      const validation=auth.validation||{};
      const scriptDetail=s.script_connected?'在线 · 正在自动接收页面请求':s.pairing?'未收到心跳 · 请连接官方页':'未配对 / 已过期';
      step('script',s.script_connected?'ok':'pending',scriptDetail);
      // With Clash auto capture the page script is not part of the route; only show it while it is in use.
      document.getElementById('chip-page').hidden=!!s.clash_capture?.enabled && !s.script_connected && !s.pairing;
      window.setStatusChip?.('chip-page', s.script_connected?'ok':s.pairing?'busy':'idle',
        s.script_connected?'在线':s.pairing?'等待心跳…':'未连接', scriptDetail);
      step('token',s.ready?(auth.persist_error?'error':'ok'):'pending',s.ready?`${auth.token_preview} · ${auth.persist_error?'保存失败：'+auth.persist_error:'本机已保存'} · ${s.last_sync?stamp(s.last_sync):'历史同步时间未知'}`:'尚未收到 Token');
      step('valid',auth.validated?'ok':validation.error_kind&&validation.error_kind!=='none'?'error':'pending',`${auth.validating?'验证中 · ':''}${stamp(auth.last_validation_at)}`);
      const validationText = formatAuthValidation(auth);
      const probeStatus = auth.ttl_probe_status || '未知';
      // Only an estimated lifetime is worth showing; "unknown"/"probing" placeholders were noise.
      let ttlObs = '';
      if (probeStatus === '已估出' && auth.estimated_ttl_sec != null) {
        ttlObs = ` · 观测 TTL ≈ ${duration(auth.estimated_ttl_sec)}`;
      }
      const syncAge = auth.sync_age_sec != null ? ` · 已同步 ${duration(auth.sync_age_sec)}` : '';
      const failAt = auth.auth_fail_at ? ` · 失效 ${stamp(auth.auth_fail_at)}` : '';
      const [validState, validShort] = validationChip(s, auth);
      window.setStatusChip?.('chip-valid', validState, validShort, `${validationText}${ttlObs}${syncAge}${failAt}`);
      el('validation').dataset.error=String(!auth.validated && !!validation.error_kind && validation.error_kind!=='none');
      el('validation').dataset.errorKind=validation.error_kind||'';
      const rush=s.rush||{}, degraded=rush.phase==='degraded';
      el('rush').hidden=rush.connection_mode!=='page' || !(rush.running || degraded);
      el('rush').classList.toggle('degraded',degraded);
      el('rush-heading').textContent=degraded?'DEGRADED · Rush 已停止下单':'Rush · 页面同步复验';
      // Auto capture picks up a plain page refresh; a pasted page script is lost on refresh, so it needs a venue switch.
      const viaCapture=!!s.clash_capture?.enabled;
      el('official').textContent=viaCapture?'去企业微信刷新预约页同步登录':'去官方页切换场地同步 Token';
      el('official-feedback').textContent=viaCapture?'在企业微信里刷新一次预约页，工作台会自动抓到新的登录并验证。'
        :'切换到已登录的企业微信预约页并选择另一场地；若脚本心跳已断开，请重新运行连接脚本。';
      const countdown=(seconds)=>`${Math.floor((seconds||0)/60)}分${(seconds||0)%60}秒`;
      el('rush-detail').textContent=degraded?rush.msg:`预约 ${rush.plan?.date||'-'} · 放号 ${stamp(rush.plan?.release_at)} · ${rush.revalidate_in_sec>0?'距复验 '+countdown(rush.revalidate_in_sec):'复验窗口已到'} · 距放号 ${countdown(rush.release_in_sec)}`;
      const authLabel={unknown:'待验证',refreshing:'刷新中',valid:'已验证',expired:'已过期',failed:'刷新失败'}[auth.state]||auth.state||'未知';
      el('badge').textContent=s.ready?(auth.validated?'登录已验证':`已接入（${authLabel}）`):s.pairing?'等待企业微信':'未连接';
      const preview=auth.token_preview||'';
      el('state').textContent=s.ready?`${s.profile_ready?'预约资料完整':'预约资料待补齐'} · ${s.script_connected?'同步通道在线':'同步通道离线，已保存 Token 仍保留'}`:'尚未接入账号';
      const clashEl=document.getElementById('connect-clash');
      if(clashEl) clashEl.textContent=formatClashCapture(s.clash_capture||{});
      el('test').disabled=!s.ready || testing || auth.validating;
      const active=typeof window.courtWatchRunning==='function'?window.courtWatchRunning():!!window.courtWatchRunning;
      document.getElementById('watch-start').disabled=!s.ready || active;
      const reserveEl=document.getElementById('watch-reserve');
      if(reserveEl){
        const sportId=window.courtActiveSportId?.()||'badminton';
        const notifyOnly=!!window.SportScope?.cap?.(sportId)?.notify_only;
        if(notifyOnly){
          reserveEl.disabled=true;
          reserveEl.checked=false;
        }else{
          reserveEl.disabled=!s.profile_ready;
          if(!s.profile_ready) reserveEl.checked=false;
        }
      }
      if(code && !s.pairing){code='';el('code').value='';}
    }catch(e){window.courtConnectionReady=false;el('state').textContent=e.message;
      window.setStatusChip?.('chip-page','error','服务未响应',e.message);el('test').disabled=true;step('script','error','本机服务不可用');document.getElementById('watch-start').disabled=true;}
    finally{polling=false;}
  }
  poll();setInterval(poll,2500);
})();
