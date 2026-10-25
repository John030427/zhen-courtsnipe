(() => {
  const local = __LOCAL_ORIGIN__, key = __PAIR_KEY__;
  if (location.origin !== 'https://reservation.sustech.edu.cn') {
    alert('请在企业微信的南科大预约页面中运行'); return;
  }
  window.__courtConnect?.stop();
  const originalFetch = window.fetch, originalOpen = XMLHttpRequest.prototype.open;
  let popup, timer, stopped = false, last = '', profileToken = '', profileBusy = false, liveSeen = false;
  const session = {token:'', userid:'', customer_id:'', name:'', tel:''};
  const box = document.createElement('div');
  box.style.cssText = 'position:fixed;right:16px;bottom:20px;z-index:2147483647;background:white;color:#17221d;padding:16px;border:2px solid #16845c;border-radius:8px;font:14px sans-serif;max-width:300px';
  const status = document.createElement('p'); status.textContent = '本机预约工具：等待连接';
  const connect = document.createElement('button'); connect.textContent = '连接本机控制台';
  const stop = document.createElement('button'); stop.textContent = '停止同步'; stop.style.marginLeft='8px';
  box.append(status,connect,stop); document.body.append(box);
  function send() {
    if (!popup || popup.closed || !session.token || !session.userid || stopped) return;
    const serialized = JSON.stringify(session);
    if (serialized === last) return;
    popup.postMessage({type:'court-session', key, ...session}, local);
  }
  function readProfile(data) {
    if (!data?.success || !data.data || typeof data.data !== 'object') return;
    const p = data.data;
    if (p.code && p.id) {
      session.userid=String(p.code); session.customer_id=String(p.id);
      session.name=String(p.name || ''); session.tel=String(p.tel || '');
      // A profile response is not an auth refresh; never replace the observed token.
      send();
    }
  }
  async function profile() {
    if (!session.token || !session.userid || profileBusy || profileToken === session.token) return;
    profileBusy=true; profileToken=session.token;
    const requestedToken=session.token;
    const requestedUser=session.userid;
    const url = new URL('/api/blade-app/qywx/getUserInfoByUserid', location.origin);
    url.searchParams.set('userid',session.userid); url.searchParams.set('token',requestedToken);
    try {
      const response=await originalFetch.call(window,url.href,{credentials:'same-origin'});
      const data=await response.json();
      if (session.token === requestedToken && session.userid === requestedUser && String(data?.data?.code) === requestedUser) readProfile(data);
    } catch { status.textContent='已监听登录信息，用户资料待获取'; }
    finally { profileBusy=false; if(session.token !== requestedToken) void profile(); }
  }
  function observe(value, live = true) {
    try {
      const url=new URL(value,location.href);
      if (url.origin !== location.origin || !url.pathname.startsWith('/api/blade-app/qywx/')) return;
      const token=url.searchParams.get('token'), userid=url.searchParams.get('userid');
      if (token && userid) {
        if (live) liveSeen = true;
        if (session.userid && session.userid !== userid) { session.customer_id='';session.name='';session.tel=''; }
        session.token=token;session.userid=userid; send(); void profile();
        return true;
      }
    } catch {}
  }
  function wrappedFetch(input,...args) {
    observe(typeof input==='string' || input instanceof URL ? String(input) : input.url);
    return originalFetch.call(this,input,...args);
  }
  function wrappedOpen(method,url,...args) { observe(String(url)); return originalOpen.call(this,method,url,...args); }
  window.fetch=wrappedFetch; XMLHttpRequest.prototype.open=wrappedOpen;
  function receive(event) {
    if (event.origin!==local || event.source!==popup || event.data?.key!==key) return;
    if (event.data.type==='court-ready') { last=''; send(); }
    if (event.data.type==='court-ping') popup.postMessage({type:'court-pong',key},local);
    if (event.data.type==='court-ack') { last=event.data.session; status.textContent='已同步到本机，保持此页面和连接窗口打开'; }
    if (event.data.type==='court-error') status.textContent='同步失败：'+event.data.message;
  }
  window.addEventListener('message',receive);
  connect.onclick=() => {
    popup=window.open(local+'/bridge#'+key,'court-local-bridge','popup,width=450,height=380');
    if (!popup) { status.textContent='连接窗口被阻止，请允许本次弹窗后再点击'; return; }
    status.textContent='等待登录请求；可在预约页面切换一次场馆';
    if (!liveSeen) {
      for (const entry of [...performance.getEntriesByType('resource')].reverse()) {
        if (observe(entry.name, false)) break;
      }
    }
    clearInterval(timer); timer=setInterval(send,3000);
  };
  function cleanup() {
    stopped=true; clearInterval(timer); window.removeEventListener('message',receive);
    if(window.fetch===wrappedFetch) window.fetch=originalFetch;
    if(XMLHttpRequest.prototype.open===wrappedOpen) XMLHttpRequest.prototype.open=originalOpen;
    box.remove(); if(popup && !popup.closed) popup.close();
  }
  stop.onclick=cleanup; window.__courtConnect={stop:cleanup};
  console.info('同步脚本已就绪，请点击预约页面右下角的“连接本机控制台”。');
})();
