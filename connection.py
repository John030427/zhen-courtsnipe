"""Explicit, short-lived pairing from the official page to localhost."""
import json
import secrets
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from core.validation_display import format_validation_display, validation_fields_from_auth
from engine import engine
from utils import ROOT, get_order_once

router = APIRouter()
pair_key = ""
expires = 0.0
last_sync = None
last_seen = 0.0


class SessionIn(BaseModel):
    key: str = Field(min_length=20, max_length=128)
    token: str = Field(min_length=1, max_length=2048)
    userid: str = Field(min_length=1, max_length=128)
    customer_id: str = Field(default="", max_length=128)
    name: str = Field(default="", max_length=128)
    tel: str = Field(default="", max_length=64)


class HeartbeatIn(BaseModel):
    key: str = Field(min_length=20, max_length=128)


def _check_key(key):
    if not pair_key or not secrets.compare_digest(key, pair_key) or time.monotonic() >= expires:
        raise HTTPException(403, '连接已过期，请重新生成脚本')


@router.post('/api/connect/heartbeat')
def heartbeat(data: HeartbeatIn):
    global last_seen
    _check_key(data.key)
    last_seen = time.monotonic()
    return {'ok': True}


def _connection_public_state() -> dict:
    auth = engine.tokens.public_view()
    ready = bool(auth.get("token_set") and auth.get("userid"))
    profile_ready = bool(auth.get("profile_ready"))
    return {
        "ready": ready,
        "profile_ready": profile_ready,
        "validated": auth.get("validated", False),
        "auth": auth,
        "last_sync": auth.get('last_sync_at'),
        "script_connected": bool(pair_key and time.monotonic() < expires and last_seen and time.monotonic() - last_seen < 45),
        "rush": engine.rush.state_dict(),
        "pairing": bool(pair_key and time.monotonic() < expires),
        "clash_capture": engine.clash_capture_state(),
        "auto_recovery": engine.auth_keeper.public_view(),
        "account_switch": {"mode": "follow_capture", "locked": not engine.can_switch_account()},
    }


@router.get('/api/connect/state')
def state():
    return _connection_public_state()


@router.post('/api/connect/script')
def script(request: Request):
    global pair_key, expires, last_seen
    pair_key = secrets.token_urlsafe(32)
    last_seen = 0.0
    expires = time.monotonic() + 3600
    source = (ROOT / 'connect-inject.js').read_text(encoding='utf-8')
    url = str(request.base_url).rstrip('/')
    return {'script': source.replace('__LOCAL_ORIGIN__', json.dumps(url)).replace('__PAIR_KEY__', json.dumps(pair_key)),
            'expires_minutes': 60}


@router.post('/api/connect/revoke')
def revoke():
    global pair_key, expires
    pair_key, expires = '', 0
    return {'ok': True}


@router.post('/api/connect/session')
async def session(data: SessionIn):
    global last_sync, last_seen
    _check_key(data.key)
    last_seen = time.monotonic()
    allow_switch = engine.can_switch_account()
    result = await engine.tokens.apply_page_session(
        token=data.token,
        userid=data.userid,
        customer_id=data.customer_id,
        name=data.name,
        tel=data.tel,
        allow_account_switch=allow_switch,
    )
    if not result.get("ok"):
        raise HTTPException(409 if "账号" in result.get("msg", "") else 400, result.get("msg", "同步失败"))

    engine.config_store.update_sections({"connection": {"mode": "page"}})
    last_sync = datetime.now(timezone.utc).isoformat()
    await engine.apply_config_change()
    if not engine.tokens.public_view().get('validated'):
        engine.tokens.start_validation()
    return {'ok': True, **_connection_public_state()}


@router.post('/api/connect/test')
async def test_connection():
    public = _connection_public_state()
    if not public['ready'] or not engine.courts:
        return {'ok': False, 'msg': '请先接入登录信息'}
    ok = await engine.tokens.validate_current()
    engine._sync_from_token_manager()
    auth = engine.tokens.public_view()
    fields = validation_fields_from_auth(auth)
    if ok:
        return {
            'ok': True,
            'msg': '登录有效，场地查询成功（未下单）',
            'auth': auth,
            **fields,
        }
    return {
        'ok': False,
        'msg': format_validation_display(auth),
        'auth': auth,
        **fields,
    }


class RecoveryIn(BaseModel):
    enabled: bool


@router.post('/api/connect/recovery')
async def set_recovery(data: RecoveryIn):
    engine.config_store.update_sections({"connection": {"auto_recover": data.enabled}})
    await engine.apply_config_change()
    if data.enabled:
        engine.auth_keeper.request_recovery()
    return {"ok": True, "auto_recovery": engine.auth_keeper.public_view()}


@router.post('/api/connect/recover')
async def recover_now():
    ok = await engine.auth_keeper.check()
    return {"ok": ok, "auto_recovery": engine.auth_keeper.public_view()}


@router.get('/connect.js')
def client():
    return FileResponse(ROOT / 'connect.js', media_type='text/javascript')


@router.get('/bridge')
def bridge():
    return FileResponse(ROOT / 'bridge.html', headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
