"""app.py — FastAPI 壳：只处理 HTTP / SSE。

    所有真正的业务逻辑都在 engine.py 里。本文件不应该知道
    CAPTCHA、HTTP payload、Windows 窗口、VID 过期策略等任何细节。
"""
from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel

from engine import engine, get_logs
from network.capture_protocol import (
    CAPTURE_AUTH_HEADER,
    parse_capture_payload,
    verify_capture_secret,
)
from utils import ROOT
from watcher import WatchPlan
from connection import router as connection_router


# ══════════════════════════════════════════════════════
# lifespan
# ══════════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app):
    await engine.start()
    try:
        yield
    finally:
        await engine.stop()


app = FastAPI(title="Court Reserver", lifespan=lifespan)

@app.exception_handler(Exception)
async def unhandled_exception(request, exc):
    # Keep API responses JSON so the dashboard never chokes on plain text 500s.
    import logging
    logging.getLogger("app").exception("unhandled: %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"ok": False, "msg": f"{type(exc).__name__}: {exc}"},
    )

app.include_router(connection_router)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])


@app.middleware("http")
async def local_origin(request, call_next):
    origin = request.headers.get("origin")
    if request.method == "POST" and origin:
        parsed = urlsplit(origin)
        if parsed.scheme != request.url.scheme or parsed.netloc != request.headers.get("host"):
            return JSONResponse({"ok": False, "msg": "Cross-origin request rejected"}, status_code=403)
    return await call_next(request)


@app.get("/api/watch/state")
def watch_state(sport_id: str = "badminton"):
    return engine.watcher.state_for(sport_id)


@app.get("/api/watch/states")
def watch_states():
    return engine.watcher.all_states()


@app.post("/api/watch/start")
async def watch_start(plan: WatchPlan):
    return await engine.watcher.start(plan)


class WatchStopIn(BaseModel):
    sport_id: str = "badminton"


@app.post("/api/watch/stop")
async def watch_stop(body: WatchStopIn = WatchStopIn()):
    await engine.watcher.stop(body.sport_id)
    return {"ok": True, "sport_id": body.sport_id}


@app.get("/watch.js")
def watch_script():
    return FileResponse(ROOT / "watch.js", media_type="text/javascript")


@app.get("/onboarding.js")
def onboarding_script():
    return FileResponse(ROOT / "onboarding.js", media_type="text/javascript")


@app.get("/matrix_target.js")
def matrix_target_script():
    return FileResponse(ROOT / "matrix_target.js", media_type="text/javascript")


@app.get("/sport_scope.js")
def sport_scope_script():
    return FileResponse(ROOT / "sport_scope.js", media_type="text/javascript")


@app.get("/control_ops.js")
def control_ops_script():
    return FileResponse(ROOT / "control_ops.js", media_type="text/javascript")


# ══════════════════════════════════════════════════════
# 静态页面
# ══════════════════════════════════════════════════════
@app.get("/")
def index():
    return FileResponse(ROOT / "index.html")


# ══════════════════════════════════════════════════════
# 健康检查（只读本机状态，不访问学校）
# ══════════════════════════════════════════════════════
@app.get("/api/health")
def health():
    import os

    from core.version import service_identity
    from utils import CONFIG_PATH

    data_dir = CONFIG_PATH.parent
    return JSONResponse(
        {
            "ok": True,
            **service_identity(),
            "config_loaded": bool(engine.config_mtime),
            "data_dir": str(data_dir),
            "data_dir_writable": os.access(data_dir, os.W_OK),
            "active_tasks": _active_tasks(),
            "can_shutdown": getattr(app.state, "request_shutdown", None) is not None,
        },
        headers={"Cache-Control": "no-store"},
    )


SPORT_LABELS = {"badminton": "羽毛球", "tennis": "网球"}


def _active_tasks() -> list[str]:
    tasks = [f"{SPORT_LABELS.get(sid, sid)}抢场" for sid, slot in engine.rush.slots.items() if slot.running]
    tasks += [f"{SPORT_LABELS.get(sid, sid)}监控" for sid, st in engine.watcher.all_states().items()
              if st.get("running")]
    if engine._reserve_inflight:
        tasks.append("手动预约")
    return tasks


class SafetyIn(BaseModel):
    dry_run: bool


@app.post("/api/safety")
async def set_safety(body: SafetyIn, x_workbench_action: str | None = Header(default=None)):
    """Turn the opt-in test mode (dry run) on or off; real booking is the default."""
    from core.safety import dry_run_orders_enabled

    if x_workbench_action != "safety":
        raise HTTPException(status_code=403, detail="missing X-Workbench-Action")
    if not body.dry_run and _active_tasks():
        return JSONResponse({"ok": False, "msg": "有任务正在运行，请先停止再关闭测试模式"}, status_code=409)
    engine.config_store.update_sections({"safety": {"dry_run": body.dry_run}})
    await engine.apply_config_change()
    return {"ok": True, "dry_run": dry_run_orders_enabled()}


class ShutdownIn(BaseModel):
    force: bool = False


@app.post("/api/admin/shutdown")
async def shutdown_ep(
    body: ShutdownIn = ShutdownIn(),
    x_workbench_action: str | None = Header(default=None),
):
    # The custom header makes any cross-site call a CORS preflight this app never grants.
    if x_workbench_action != "shutdown":
        raise HTTPException(status_code=403, detail="missing X-Workbench-Action")
    active = _active_tasks()
    if active and not body.force:
        return JSONResponse({"ok": False, "active": active,
                             "msg": "有任务正在运行，确认后再退出"}, status_code=409)
    stop = getattr(app.state, "request_shutdown", None)
    if stop is None:
        return {"ok": False, "msg": "开发模式（直接用 uvicorn 运行）不支持从网页退出，请在命令行窗口按 Ctrl+C"}
    asyncio.get_running_loop().call_later(0.3, stop)
    return {"ok": True, "msg": "服务正在退出"}


# ══════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════
@app.get("/api/config")
def get_config():
    return engine.public_config()


@app.get("/api/dashboard")
def dashboard_ep():
    return engine.dashboard_state()


class ConfigIn(BaseModel):
    offset_days: int | None = None
    token: str | None = None


@app.post("/api/config")
async def set_config(d: ConfigIn):
    try:
        if d.offset_days is not None:
            engine.update_offset_days(d.offset_days)
        if d.token:
            engine.update_token(d.token)
    except Exception as e:
        return {"ok": False, "msg": str(e)}
    await engine.apply_config_change()
    return {"ok": True, "target_date": engine.target_date}


class AutoConfigIn(BaseModel):
    slot_start: str
    slot_end: str
    refresh_at: str
    court_priority: list[int] | None = None
    sport_id: str | None = None


@app.post("/api/auto/config")
async def set_auto_config(d: AutoConfigIn):
    try:
        engine.update_auto_config(
            d.slot_start,
            d.slot_end,
            d.refresh_at,
            court_priority=d.court_priority,
            sport_id=d.sport_id,
        )
    except Exception as e:
        return {"ok": False, "msg": str(e)}
    await engine.apply_config_change()
    return {"ok": True}


# ══════════════════════════════════════════════════════
# Token / mitm
# ══════════════════════════════════════════════════════
class TokenFetchIn(BaseModel):
    timeout: float | None = None


@app.post("/api/token/fetch")
async def token_fetch(d: TokenFetchIn):
    return await engine.fetch_token()


@app.get("/api/token/state")
def token_state_ep():
    return engine.tokens.public_view()


class CaptureIn(BaseModel):
    token: str
    userid: str = ""
    path: str
    host: str
    source: str = "mitm"
    capture_seq: int
    captured_at: float | None = None
    customer_id: str = ""
    name: str = ""
    tel: str = ""
    profile_revision: int = 0


@app.post("/api/internal/capture")
async def internal_capture(
    payload: CaptureIn,
    capture_key: str | None = Header(default=None, alias=CAPTURE_AUTH_HEADER),
):
    if not verify_capture_secret(capture_key, engine._capture_secret):
        raise HTTPException(status_code=403, detail="invalid capture key")
    event = parse_capture_payload(payload.model_dump())
    if event is None:
        raise HTTPException(status_code=400, detail="invalid capture payload")
    return await engine.ingest_capture_event(event)


@app.get("/api/mitm/state")
def mitm_state_ep():
    return engine.mitm_state()


@app.post("/api/mitm/restart")
async def mitm_restart_ep():
    ok = await engine.restart_mitm()
    return {"ok": ok}


@app.get("/api/capture/clash")
def clash_capture_ep():
    """Clash → mitm capture status and offline setup hints (does not touch Clash)."""
    return engine.clash_capture_state()


@app.get("/proxy.pac")
def proxy_pac():
    """Windows "使用设置脚本" target for machines without Clash.

    Only the reservation host goes to the local mitm, falling back to DIRECT if it
    is down; everything else is DIRECT. With capture disabled the script routes
    nothing, so switching capture_mode off also undoes the routing.
    """
    from utils import MITM_HOST, MITM_PORT, RESERVATION_HOST

    if engine.mitm_capture_enabled():
        rule = (f'  if (host.toLowerCase() == "{RESERVATION_HOST}") '
                f'return "PROXY {MITM_HOST}:{MITM_PORT}; DIRECT";\n')
    else:
        rule = ""
    body = f"function FindProxyForURL(url, host) {{\n{rule}  return \"DIRECT\";\n}}\n"
    return Response(body, media_type="application/x-ns-proxy-autoconfig",
                    headers={"Cache-Control": "no-store"})


# ══════════════════════════════════════════════════════
# 日志
# ══════════════════════════════════════════════════════
@app.get("/api/logs")
def logs_ep(since: int = 0):
    return {"logs": get_logs(since)}


# ══════════════════════════════════════════════════════
# VID 池
# ══════════════════════════════════════════════════════
@app.post("/api/vid/start")
async def vid_start():
    return await engine.start_vid_pool()


@app.post("/api/vid/stop")
async def vid_stop():
    if engine.rush.running:
        return {"ok": False, "msg": "VID 池由 Rush 管理，请先停止对应抢场任务"}
    await engine.stop_vid_pool()
    return {"ok": True}


@app.get("/api/vid/state")
def vid_state():
    return engine.vid_state()


# ══════════════════════════════════════════════════════
# 刷新
# ══════════════════════════════════════════════════════
@app.post("/api/refresh")
async def refresh_ep():
    return await engine.start_refresh()


class MatrixScanIn(BaseModel):
    date: str | None = None
    sport_id: str = "badminton"
    concurrency: int = 3


@app.post("/api/matrix/scan")
async def matrix_scan_ep(body: MatrixScanIn):
    return await engine.scan_matrix(
        body.date,
        sport_id=body.sport_id,
        concurrency=body.concurrency,
    )


@app.get("/api/matrix/state")
def matrix_state_ep(sport_id: str = "badminton"):
    return engine.matrix_public_state(sport_id)


@app.get("/api/refresh/seq")
def refresh_seq():
    return {"seq": engine.bus.seq}


# ══════════════════════════════════════════════════════
# 手动预约
# ══════════════════════════════════════════════════════
class ReserveIn(BaseModel):
    court_no: int
    start: str
    end: str
    date: str | None = None
    sport_id: str = "badminton"


@app.post("/api/reserve")
async def reserve_ep(d: ReserveIn):
    return await engine.reserve(
        d.court_no, d.start, d.end, d.date, sport_id=d.sport_id,
    )


# ══════════════════════════════════════════════════════
# 全自动
# ══════════════════════════════════════════════════════
class AutoStartIn(BaseModel):
    sport_id: str = "badminton"
    date: str | None = None
    slot_start: str | None = None
    slot_end: str | None = None
    release_at: str | None = None
    court_priority: list[int] | None = None


@app.post("/api/auto/start")
async def auto_start_ep(body: AutoStartIn = AutoStartIn()):
    return await engine.start_auto(sport_id=body.sport_id, settings=body.model_dump(exclude_none=True))


@app.post("/api/auto/stop")
async def auto_stop_ep(sport_id: str | None = None):
    if sport_id is not None and sport_id not in ("badminton", "tennis"):
        return {"ok": False, "msg": "不支持的运动"}
    await engine.stop_auto(sport_id)
    return {"ok": True}


@app.get("/api/auto/state")
def auto_state_ep(sport_id: str | None = None):
    return engine.auto_state(sport_id)


# ══════════════════════════════════════════════════════
# SSE
# ══════════════════════════════════════════════════════
@app.get("/api/stream")
async def stream_ep(since: int = 0):
    q, hist = engine.bus.subscribe(since)

    async def gen():
        last = since
        try:
            for e in hist:
                yield f"event: {e['_topic']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n"
                last = e["_seq"]
            while True:
                try:
                    e = await asyncio.wait_for(q.get(), timeout=20)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if e["_seq"] <= last:
                    continue
                last = e["_seq"]
                yield (f"event: {e['_topic']}\n"
                       f"data: {json.dumps(e, ensure_ascii=False)}\n\n")
        except asyncio.CancelledError:
            pass
        finally:
            engine.bus.unsubscribe(q)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    # Same entry as the launcher: 127.0.0.1:8766 so the capture IPC port matches.
    import serve

    serve.main()
