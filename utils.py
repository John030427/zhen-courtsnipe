"""utils.py — 稳定原子能力层

    所有函数都是"一次输入 → 一次输出"，不维护任何跨调用状态。
    Engine 决定何时调用、调几次、调用后状态怎么变。

依赖：
    pip install pywin32 httpx opencv-python pillow numpy
"""
from __future__ import annotations

import base64
import asyncio
import io
import json
import logging
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import cv2
import httpx
import numpy as np
from PIL import Image

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore


log = logging.getLogger("court")

_config_store = None


def _store():
    global _config_store
    if _config_store is None:
        from core.config_store import ConfigStore

        _config_store = ConfigStore()
    return _config_store


# ══════════════════════════════════════════════════════
# 路径 / 常量
# ══════════════════════════════════════════════════════
ROOT        = Path(__file__).parent
from app_paths import DATA_DIR  # noqa: E402
CONFIG_PATH = DATA_DIR / "config.toml"

BASE_URL  = "https://reservation.sustech.edu.cn"
URL_GEN   = BASE_URL + "/api/blade-base/captcha/generate/d"
URL_CHK   = BASE_URL + "/api/blade-base/captcha/check/d"
URL_SAVE  = BASE_URL + "/api/blade-app/qywx/saveOrder"
URL_QUERY = BASE_URL + "/api/blade-app/qywx/getOrderTimeConfigList"

MITM_HOST     = "127.0.0.1"
MITM_PORT     = 8080
MITM_UPSTREAM = os.environ.get("MITM_UPSTREAM", "").strip()

RESERVATION_HOST = "reservation.sustech.edu.cn"
WINDOW_KEYWORDS  = ("reservation.sustech.edu.cn", "reservation")

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": BASE_URL,
    "Referer": BASE_URL + "/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
}


@dataclass(frozen=True)
class Court:
    no: int
    id: str
    name: str


# ══════════════════════════════════════════════════════
# 配置文件读写（原子操作）
# ══════════════════════════════════════════════════════
def read_config_dict(path: Path = CONFIG_PATH) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def write_config_string(key: str, value: str, path: Path = CONFIG_PATH) -> None:
    section = _field_section(key)
    if path != CONFIG_PATH:
        text = path.read_text(encoding="utf-8")
        pattern = rf'(?m)^(\s*{re.escape(key)}\s*=\s*)"[^"]*"'
        new_text, n = re.subn(pattern, rf'\1"{value}"', text, count=1)
        if n == 0:
            raise RuntimeError(f"config.toml 未找到字符串字段 {key!r}")
        path.write_text(new_text, encoding="utf-8")
        return
    _store().write_string_field(section, key, value)


def write_config_int(key: str, value: int, path: Path = CONFIG_PATH) -> None:
    section = _field_section(key)
    if path != CONFIG_PATH:
        text = path.read_text(encoding="utf-8")
        pattern = rf'(?m)^(\s*{re.escape(key)}\s*=\s*)-?\d+'
        new_text, n = re.subn(pattern, rf'\g<1>{int(value)}', text, count=1)
        if n == 0:
            raise RuntimeError(f"config.toml 未找到整数字段 {key!r}")
        path.write_text(new_text, encoding="utf-8")
        return
    _store().write_int_field(section, key, int(value))




def _field_section(key: str) -> str:
    if key in ("token", "offset_days", "slot_start", "slot_end", "refresh_at"):
        if key == "token" or key == "offset_days":
            return "order"
        return "auto"
    if key == "mode":
        return "connection"
    raise RuntimeError(f"unknown config field {key!r}")


# ══════════════════════════════════════════════════════
# HTTP 客户端（进程内单例）
# ══════════════════════════════════════════════════════
_client: httpx.AsyncClient | None = None
_request_lock = asyncio.Lock()
_next_request = 0.0
_busy_count = 0
_business_busy_count = 0
_priority_queue = None
_priority_loop = None


async def wait_http_slot() -> None:
    """Shared HTTP pacing (1s minimum interval + global backoff)."""
    global _priority_queue, _priority_loop
    from core.request_priority import PriorityQueue, request_priority
    loop = asyncio.get_running_loop()
    if _priority_loop is not loop:
        _priority_loop, _priority_queue = loop, PriorityQueue()
    def claim():
        global _next_request
        _next_request = time.monotonic() + 1.0
    await _priority_queue.acquire(request_priority.get(), lambda: _next_request - time.monotonic(), claim)


async def _pace_request(request):
    from core.request_priority import request_priority
    from core.order_guard import OrderNotSent
    deadline = request.extensions.get("order_send_deadline")
    if deadline is None:
        await wait_http_slot()
    else:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OrderNotSent("订单或 VID 已超时，未发送")
        try:
            priority = request_priority.set(0)
            try:
                await asyncio.wait_for(wait_http_slot(), timeout=remaining)
            finally:
                request_priority.reset(priority)
        except asyncio.TimeoutError as exc:
            raise OrderNotSent("限流等待超过订单或 VID 截止时间，未发送") from exc
    guard = request.extensions.get("order_before_send")
    if guard:
        guard()
    request.extensions["sent_wall"] = time.time()
    timing = request.extensions.get("query_timing")
    if timing is not None:
        timing['sent_at'] = time.monotonic()


# School clock vs this computer, from HTTP Date headers (1 s resolution). The server stamped each
# reply at some moment between our send and receive, which bounds the offset; intersecting the
# bounds of successive replies narrows it. Only measured and reported, never used to shift timing.
_clock_bounds: tuple[float, float] | None = None


def _observe_server_clock(response) -> None:
    global _clock_bounds
    sent = response.request.extensions.get("sent_wall")
    stamp = response.headers.get("Date")
    if sent is None or not stamp:
        return
    try:
        server = parsedate_to_datetime(stamp)
    except (TypeError, ValueError, OverflowError):
        return
    if server.tzinfo is None:
        return
    lo, hi = server.timestamp() - time.time(), server.timestamp() + 1 - sent
    if _clock_bounds is not None:
        narrowed = max(lo, _clock_bounds[0]), min(hi, _clock_bounds[1])
        # Bounds that no longer overlap mean a clock was adjusted; start over from this reply.
        _clock_bounds = narrowed if narrowed[0] <= narrowed[1] else (lo, hi)
    else:
        _clock_bounds = (lo, hi)


def server_clock_offset() -> tuple[float, float] | None:
    """(offset, uncertainty) in seconds; a positive offset means the school's clock is ahead."""
    if _clock_bounds is None:
        return None
    lo, hi = _clock_bounds
    return (lo + hi) / 2, (hi - lo) / 2


def reset_server_clock() -> None:
    global _clock_bounds
    _clock_bounds = None


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return None
            seconds = date.timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


async def _check_response(response):
    global _next_request, _busy_count, _business_busy_count
    from core.audit import record
    await response.aread()
    timing = response.request.extensions.get("query_timing")
    if timing is not None:
        timing['response_at'] = time.monotonic()
    _observe_server_clock(response)
    try:
        payload = response.json()
        message = str(payload.get("msg") or payload.get("message") or "") if isinstance(payload, dict) else ""
    except ValueError:
        message = ""
    message = message.lower()
    retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
    limited = any(word in message for word in ("频繁", "请求过于", "限流", "too many", "frequent", "rate limit"))
    # "系统繁忙" also comes back as HTTP 500; it gets the short wait too (429/503 and explicit limits
    # are handled above), so a struggling server is not hit every second.
    business_busy = any(word in message for word in ("繁忙", "busy"))
    if response.status_code in (429, 503) or limited:
        _busy_count = min(_busy_count + 1, 5)
        backoff = 15 * 2 ** _busy_count
        reason = "http_rate_limit" if response.status_code == 429 else (
            "http_unavailable" if response.status_code == 503 else "rate_limit_message")
        attempt = _busy_count
    elif business_busy:
        _business_busy_count = min(_business_busy_count + 1, 3)
        backoff = 2 ** _business_busy_count
        reason, attempt = "business_busy", _business_busy_count
    elif retry_after is not None and retry_after > 0:
        backoff, reason, attempt = 0, "retry_after", 0
    else:
        if response.is_success:
            _busy_count = _business_busy_count = 0
        return

    backoff = max(backoff, retry_after or 0)
    now = time.monotonic()
    # A later response must never shorten an already active server backoff.
    _next_request = max(_next_request, now + backoff)
    effective_wait = max(0, _next_request - now)
    path = response.request.url.path
    if path not in {httpx.URL(url).path for url in (URL_QUERY, URL_SAVE, URL_GEN, URL_CHK)}:
        path = "other"
    context = {k: v for k, v in response.request.extensions.get("backoff_context", {}).items()
               if k in ("run_id", "sport_id", "source")}
    record("http.backoff", **context, path=path, reason=reason,
           http_status=response.status_code, backoff_attempt=attempt,
           retry_after_sec=retry_after, backoff_sec=backoff,
           effective_wait_sec=round(effective_wait, 3))
    log.warning("HTTP backoff path=%s reason=%s status=%s wait=%.1fs",
                path, reason, response.status_code, effective_wait)
    # Keep the existing SSE callback contract; never forward raw school messages.
    hint = "学校业务繁忙，短暂等待" if reason == "business_busy" else "学校限流或暂不可用，等待后继续"
    if effective_wait > 0:
        pub = _rate_limit_publisher
        if pub is not None:
            try:
                await pub(effective_wait, response.status_code, hint)
            except Exception:
                pass


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(headers=HEADERS, timeout=6.0, trust_env=False,
                                   event_hooks={"request": [_pace_request], "response": [_check_response]})
    return _client


async def close_http_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


_rate_limit_publisher = None


def set_rate_limit_publisher(callback) -> None:
    global _rate_limit_publisher
    _rate_limit_publisher = callback


# ══════════════════════════════════════════════════════
# 纯函数小工具
# ══════════════════════════════════════════════════════
def norm_hm(t: str) -> str:
    if not t or ":" not in t:
        return t
    h, m = t.split(":", 1)
    return f"{h.zfill(2)}:{m}" if h.isdigit() else t


def is_auth_fail(j: dict) -> bool:
    if not isinstance(j, dict):
        return False
    msg = str(j.get("msg") or j.get("message") or "")
    if any(word in msg for word in ("鉴权失败", "登录失效", "登录过期", "未登录")):
        return True
    low = msg.lower()
    if "token" in low and any(word in low for word in ("失效", "无效", "过期", "expired", "invalid")):
        return True
    if str(j.get("code")) in ("401", "403"):
        return True
    return False


def classify_order_reply(r: dict) -> tuple[str, str]:
    """saveOrder 返回分类：ok / busy / error"""
    if r.get("uncertain"):
        return "error", "结果待核实，已停止提交"
    if r.get("success"):
        return "ok", "✅ 预约成功"
    msg = str(r.get("msg", "") or "")
    for kw in ("繁忙", "稍后", "系统忙", "请求过于", "频繁",
               "frequent", "too many", "try again", "busy"):
        if kw in msg.lower() or kw in msg:
            return "busy", "⚠ 系统繁忙"
    if not msg or msg == "non-json":
        return "error", "❌ 响应异常"
    return "error", "❌ 下单失败"


def slot_covers(avail: dict, start: str, end: str) -> bool:
    h1, m1 = map(int, start.split(":"))
    h2, m2 = map(int, end.split(":"))
    t, e = h1 * 60 + m1, h2 * 60 + m2
    while t < e:
        if avail.get(f"{t // 60:02d}:{t % 60:02d}") != "1":
            return False
        t += 30
    return True


# ══════════════════════════════════════════════════════
# 验证码 ROTATE 求解
# ══════════════════════════════════════════════════════
_db_path = ROOT / "fast_db.npz"
if _db_path.exists():
    _DB = np.load(_db_path, allow_pickle=False)
    _BG_DESCS = _DB["bg_descs"]
    _REF_FFTS = _DB["ref_ffts"]
    _ANGLE_BINS = int(_DB["angle_bins"][0])
    _RADIAL_BINS = int(_DB["radial_bins"][0])
    _DIR_SIGN = int(_DB["direction_sign"][0])
else:
    _BG_DESCS = np.zeros((1, 8), dtype=np.float32)
    _REF_FFTS = np.zeros((1, 8), dtype=np.complex64)
    _ANGLE_BINS = 36
    _RADIAL_BINS = 8
    _DIR_SIGN = 1


class CaptchaParseError(RuntimeError):
    """generate/d captcha payload could not be normalized to image fields."""


def parse_rotate_captcha(raw) -> dict:
    """Normalize captcha from generate/d to a dict with backgroundImage/templateImage."""
    if isinstance(raw, dict):
        captcha = raw
    elif isinstance(raw, str):
        try:
            captcha = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.warning(
                "captcha JSON 解析失败: %s len=%d",
                type(exc).__name__,
                len(raw),
            )
            raise CaptchaParseError(f"captcha JSON 无效: {type(exc).__name__}") from exc
        if not isinstance(captcha, dict):
            log.warning(
                "captcha JSON 顶层类型异常: %s",
                type(captcha).__name__,
            )
            raise CaptchaParseError(
                f"captcha JSON 顶层应为 dict，实际 {type(captcha).__name__}"
            )
    else:
        log.warning("captcha 类型异常: %s", type(raw).__name__)
        raise CaptchaParseError(f"captcha 类型异常: {type(raw).__name__}")

    if "backgroundImage" not in captcha or "templateImage" not in captcha:
        raise CaptchaParseError("captcha 缺少必需的图片字段")
    return captcha


class _CaptchaSolver:
    W, H, TRAVEL = 242, 180, 242
    BG_W, BG_H   = 48, 36

    def __init__(self, timeout: float = 3.5):
        self.timeout = timeout
        self.timing: dict = {}

    async def solve(self) -> str:
        t0 = time.perf_counter()
        cid, cap = await self._generate()
        t1 = time.perf_counter()

        start, mono = datetime.now(), time.monotonic()
        move_x = await __import__("asyncio").to_thread(self._recognize, cap)
        t2 = time.perf_counter()

        track = await __import__("asyncio").to_thread(self._make_track_sync, move_x, mono)
        t3 = time.perf_counter()

        stop = datetime.now()
        vid = await self._check(cid, track, start, stop)
        t4 = time.perf_counter()

        self.timing = {
            "gen_ms":   round((t1 - t0) * 1000, 1),
            "rec_ms":   round((t2 - t1) * 1000, 1),
            "trk_ms":   round((t3 - t2) * 1000, 1),
            "chk_ms":   round((t4 - t3) * 1000, 1),
            "total_ms": round((t4 - t0) * 1000, 1),
            "move_x":   move_x,
        }
        return vid

    async def _generate(self):
        started = time.monotonic()
        r = await get_client().post(URL_GEN, json={"type": "ROTATE"},
                                    timeout=self.timeout)
        r.raise_for_status()
        failure_stage = "response_json"
        shape = {}
        try:
            j = r.json()
            failure_stage = "response_shape"
            if not isinstance(j, dict) or "id" not in j or "captcha" not in j:
                raise CaptchaParseError(f"generate 字段异常: {type(j).__name__}")
            failure_stage = "captcha_payload"
            raw = j["captcha"]
            shape = {"captcha_type": type(raw).__name__,
                     "captcha_length": len(raw) if isinstance(raw, (str, dict, list)) else None}
            captcha = parse_rotate_captcha(j["captcha"])
        except (ValueError, CaptchaParseError) as exc:
            from core.audit import record
            content_type = r.headers.get('content-type', '')
            record('vid.generate_failure', source='captcha.generate',
                   error_kind=type(exc).__name__,
                   http_status=r.status_code if isinstance(r.status_code, int) else None,
                   content_type=content_type[:80] if isinstance(content_type, str) else None,
                   response_bytes=len(r.content), elapsed_ms=round((time.monotonic()-started)*1000),
                   failure_stage=failure_stage,
                   json_error=type(exc.__cause__).__name__ if exc.__cause__ else type(exc).__name__,
                   **shape)
            raise CaptchaParseError(f"generate 响应解析失败: {type(exc).__name__}") from exc
        return j["id"], captcha

    async def _check(self, cid, track, start, stop):
        payload = {
            "id": cid,
            "data": {
                "bgImageWidth":  self.W,
                "bgImageHeight": self.H,
                "startTime": start.strftime("%Y-%m-%d %H:%M:%S"),
                "stopTime":  stop.strftime("%Y-%m-%d %H:%M:%S"),
                "trackList": track,
            },
        }
        r = await get_client().post(URL_CHK, json=payload, timeout=self.timeout)
        r.raise_for_status()
        j = r.json()
        if not j.get("success"):
            raise RuntimeError(j.get("msg", "验证码校验失败"))
        vid = j.get("data")
        if not vid:
            raise RuntimeError("验证码通过但 data 为空")
        return vid

    def _recognize(self, cap) -> int:
        bg   = self._prep_bg(self._decode(cap["backgroundImage"]))
        tpl  = self._prep_tpl(self._decode(cap["templateImage"]))
        desc = self._desc(self._gray(bg))
        best = int(np.argmax(_BG_DESCS @ desc))
        sig  = self._sig(self._gray(tpl))
        corr = np.fft.irfft(_REF_FFTS[best] * np.conj(np.fft.rfft(sig)),
                            n=_ANGLE_BINS)
        idx  = int(np.argmax(corr))
        if idx >= _ANGLE_BINS // 2:
            idx -= _ANGLE_BINS
        angle = (_DIR_SIGN * idx * 360.0 / _ANGLE_BINS) % 360.0
        return int(angle / 360.0 * self.TRAVEL + 0.5)

    @staticmethod
    def _decode(uri):
        im = Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1])))
        im.load()
        return im

    def _prep_bg(self, im):
        im = im.convert("RGB")
        w = round(im.width * self.H / im.height)
        im = im.resize((w, self.H), Image.Resampling.LANCZOS)
        left = max(0, (w - self.W) // 2)
        return im.crop((left, 0, left + self.W, self.H)).convert("RGBA")

    def _prep_tpl(self, im):
        im = im.convert("RGBA")
        w = max(1, round(im.width * self.H / im.height))
        im = im.resize((w, self.H), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (self.H, self.H), (255, 255, 255, 0))
        canvas.alpha_composite(im, ((self.H - im.width) // 2, 0))
        return canvas

    @staticmethod
    def _gray(im):
        arr = np.asarray(im)
        rgb = arr[:, :, :3].astype(np.float32)
        if arr.shape[2] == 4:
            a = arr[:, :, 3:4].astype(np.float32) / 255.0
            rgb = rgb * a + 255.0 * (1 - a)
        return cv2.cvtColor(rgb.astype(np.uint8), cv2.COLOR_RGB2GRAY)

    def _desc(self, gray):
        small = cv2.resize(gray, (self.BG_W, self.BG_H),
                           interpolation=cv2.INTER_AREA)
        x = small.astype(np.float32).ravel()
        x -= x.mean()
        n = np.linalg.norm(x)
        return x / n if n > 1e-8 else x

    @staticmethod
    def _sig(gray):
        h, w = gray.shape
        polar = cv2.warpPolar(
            gray.astype(np.float32), (_RADIAL_BINS, _ANGLE_BINS),
            (w / 2.0, h / 2.0), min(h, w) * 0.48,
            cv2.WARP_POLAR_LINEAR | cv2.INTER_LINEAR,
        )
        r0, r1 = int(_RADIAL_BINS * 0.30), int(_RADIAL_BINS * 0.92)
        grad = cv2.Sobel(polar[:, r0:r1], cv2.CV_32F, 1, 0, ksize=3)
        sig = np.mean(np.abs(grad), axis=1)
        sig -= sig.mean()
        std = sig.std()
        return (sig / std).astype(np.float32) if std > 1e-6 else sig.astype(np.float32)

    @staticmethod
    def _make_track_sync(move_x, mono):
        import random as _random
        def t(): return int((time.monotonic() - mono) * 1000)
        out = [{"x": 0, "y": _random.randint(-2, 2), "type": "down", "t": t()}]
        x = 0
        while x < move_x:
            x = min(x + (2 if _random.random() < 0.8 else 1), move_x)
            time.sleep(0.001)
            out.append({"x": x, "y": _random.randint(-2, 2), "type": "move", "t": t()})
        out.append({"x": move_x, "y": _random.randint(-2, 2), "type": "up", "t": t()})
        return out


async def get_vid_once(timeout: float = 3.5) -> tuple[str, dict]:
    """单次：generate → recognize → track → check，返回 (vid, timing)。"""
    solver = _CaptchaSolver(timeout)
    vid = await solver.solve()
    return vid, solver.timing


# ══════════════════════════════════════════════════════
# 预约 API
# ══════════════════════════════════════════════════════
async def get_order_once(court: Court, date: str, token: str, user_id: str,
                         timeout: float = 4.0) -> dict:
    """单次查询某个场地的时段配置。

    返回：
        {"ok": bool, "auth_fail": bool, "avail": dict, "blocks": list, "error": str}
    """
    from core.audit import redact
    probe = {'groundId': court.id, 'startDate': date, 'endDate': date}
    timing = {'started_at': time.monotonic()}
    returned_at = None

    def finish(result):
        done = time.monotonic()
        sent = timing.get('sent_at')
        response = timing.get('response_at', returned_at or done)
        result['timing'] = {
            'queue_ms': round((sent - timing['started_at']) * 1000, 2) if sent is not None else None,
            'request_ms': round((response - sent) * 1000, 2) if sent is not None else None,
            'processing_ms': round((done - response) * 1000, 2) if sent is not None else None,
            'total_ms': round((done - timing['started_at']) * 1000, 2),
        }
        return result

    def failure(message, kind='query', **extra):
        return finish({'ok': False, 'auth_fail': kind == 'auth', 'error_kind': kind,
                       'error': redact(message, token), **probe, **extra})

    try:
        r = await get_client().get(URL_QUERY, params={
            "groundId": court.id, "startDate": date, "endDate": date,
            "userid": user_id, "token": token,
        }, timeout=timeout, extensions={'query_timing': timing})
        returned_at = time.monotonic()
    except Exception as e:
        log.warning("查询 %s 网络异常: %s", court.name, type(e).__name__)
        return failure('网络异常：' + type(e).__name__, 'network')

    if r.status_code != 200:
        try:
            reply = r.json()
        except Exception:
            reply = {}
        reply = reply if isinstance(reply, dict) else {}
        message = redact(reply.get('msg') or reply.get('message') or '', token)
        auth = r.status_code in (401, 403) or is_auth_fail(reply)
        return failure(f'HTTP {r.status_code}' + (': ' + message if message else ''),
                       'auth' if auth else 'query', http_status=r.status_code,
                       school_msg=message, code=redact(reply.get('code'), token))

    try:
        j = r.json()
    except Exception:
        return failure('学校返回非 JSON 响应', http_status=r.status_code)

    try:
        from core.venue_config_probe import log_order_time_config_from_server

        log_order_time_config_from_server(
            token=token,
            userid=user_id,
            ground_id=court.id,
            start_date=date,
            end_date=date,
            payload=j if isinstance(j, dict) else None,
            http_status=r.status_code,
        )
    except Exception:
        pass

    if not isinstance(j, dict):
        return failure('学校返回结构异常', http_status=r.status_code)

    if not j.get("success"):
        msg = redact(j.get("msg") or j.get("message") or '', token)
        auth = is_auth_fail(j)
        log.warning("查询 %s %s: %s",
                    court.name, "鉴权失败" if auth else "success=false", msg)
        return failure(msg or f"success=false (code={j.get('code')})", 'auth' if auth else 'query',
                       school_msg=msg, code=redact(j.get('code'), token), http_status=r.status_code)

    data = j.get('data')
    configs = data.get('configList') if isinstance(data, dict) else None
    if not isinstance(configs, list) or any(not isinstance(c, dict) or
        not isinstance(c.get('timeBlockList', []), list) or
        any(not isinstance(b, dict) for b in c.get('timeBlockList', [])) for c in configs):
        return failure('学校时段数据结构异常', http_status=r.status_code)

    avail: dict[str, str] = {}
    blocks: list[dict] = []
    for cfg in configs:
        if cfg.get("date") != date:
            continue
        for blk in cfg.get("timeBlockList", []):
            if any(not isinstance(blk.get(key, ''), str) for key in ('time', 'endTime')):
                return failure('学校时段时间格式异常', http_status=r.status_code)
            t = norm_hm(blk.get("time", ""))
            e = norm_hm(blk.get("endTime", ""))
            avail[t] = blk.get("status")
            # Other bookers' name/code/phone stay out of results, events and the UI.
            blocks.append({
                "time": t, "endTime": e,
                "status": blk.get("status"),
                "type":         blk.get("type"),
                "fee":          blk.get("fee"),
            })
    blocks.sort(key=lambda b: b["time"])
    return finish({"ok": True, "auth_fail": False, "avail": avail, "blocks": blocks,
            "school_msg": redact(j.get('msg') or j.get('message') or '', token),
            "code": redact(j.get('code'), token), "http_status": r.status_code,
            **probe})


async def save_order_once(
    *,
    vid: str, court: Court, date: str, start: str, end: str, token: str,
    user_id: str, customer_id: str, customer_name: str, customer_tel: str,
    gym_id: str, gym_name: str,
    _booking_internal: bool = False,
    _before_send=None,
    _send_deadline: float | None = None,
    _audit_context: dict | None = None,
    _vid_issued_mono: float | None = None,
) -> dict:
    """单次 POST saveOrder。只返回原始响应 dict，不发事件、不落盘。"""
    from core.safety import DRY_RUN_ORDER_RESPONSE, dry_run_orders_enabled
    from core.audit import record, redact
    from models.auth import token_preview

    if dry_run_orders_enabled():
        log.info("saveOrder 已阻止（dry-run） %s %s-%s", court.name, start, end)
        return dict(DRY_RUN_ORDER_RESPONSE)
    if not _booking_internal or _before_send is None:
        log.warning("saveOrder 绕过 BookingService，已拒绝 %s", court.name)
        return {
            "success": False,
            "msg": "必须通过 BookingService 下单",
            "dry_run": True,
        }

    st = f"{date} {start}:00"
    et = f"{date} {end}:00"
    now = datetime.now()
    order_dt = datetime.strptime(st, "%Y-%m-%d %H:%M:%S").replace(
        hour=now.hour, minute=now.minute, second=0, microsecond=0)
    order_time = order_dt.strftime("%Y-%m-%d %H:%M:%S")

    payload = {
        "customerEmail": "", "customerId": customer_id,
        "customerName": customer_name, "customerTel": customer_tel,
        "endTime": et, "groundId": court.id, "groundName": court.name,
        "groundType": "0", "gymId": gym_id, "gymName": gym_name,
        "id": vid, "isIllegal": "0", "messagePushType": "0",
        "orderDate": order_time, "startTime": st,
        "tmpEndTime": et, "tmpOrderDate": order_time, "tmpStartTime": st,
        "userNum": "1",
    }

    t0 = time.monotonic()
    sent_at = None
    context = {k: v for k, v in (_audit_context or {}).items()
               if k in ("run_id", "sport_id", "source", "auth_revision")}
    context.update(groundId=court.id, gymId=gym_id, court=court.no,
                   date=date, startTime=start, endTime=end, vid_preview=token_preview(vid))

    def guarded_send():
        nonlocal sent_at
        _before_send()
        sent_at = time.monotonic()
        record("order.send", token=token, **context, sent=True,
               queue_ms=round((sent_at - t0) * 1000),
               vid_age_ms=round((sent_at - _vid_issued_mono) * 1000) if _vid_issued_mono else None)

    record("order.queued", token=token, **context, sent=False)

    try:
        from core.order_guard import OrderNotSent
        _before_send()
        r = await get_client().post(
            URL_SAVE, params={"userid": user_id, "token": token},
            json=payload, timeout=6.0,
            extensions={"order_before_send": guarded_send, "order_send_deadline": _send_deadline,
                        "backoff_context": context},
        )
        try:
            result = r.json()
        except Exception:
            result = {"success": False, "msg": "non-json", "uncertain": True}
        if not isinstance(result, dict):
            result = {"success": False, "msg": "invalid-response", "uncertain": True}
        message = redact(result.get("msg") or result.get("message") or "", token, vid,
                         user_id, customer_id, customer_name, customer_tel)
        auth_fail = r.status_code in (401, 403) or is_auth_fail(result)
        uncertain = bool(result.get("uncertain")) or r.status_code >= 500 or "系统异常" in message
        result = {"success": result.get("success") is True and r.is_success and not uncertain and not auth_fail,
                  "msg": message, "code": redact(result.get("code"), token, vid),
                  "http_status": r.status_code, "uncertain": uncertain,
                  "auth_fail": auth_fail, "sent": True,
                  "error_kind": "uncertain" if uncertain else "auth" if auth_fail else "business"}
    except OrderNotSent as e:
        result = {"success": False, "blocked": True, "sent": False,
                  "dry_run": e.dry_run, "msg": str(e)}
    except Exception as e:
        dt_ms = (time.monotonic() - t0) * 1000
        log.warning("下单 %s 异常 (+%.0fms): %s", court.name, dt_ms, type(e).__name__)
        result = {"success": False, "msg": "网络异常，请核对官方订单", "uncertain": True}

    result.setdefault("sent", sent_at is not None)
    record("order.result", token=token, **context, ok=bool(result.get("success")),
           school_msg=result.get("msg"), code=result.get("code"), http_status=result.get("http_status"),
           auth_fail=bool(result.get("auth_fail")), uncertain=bool(result.get("uncertain")),
           sent=result.get("sent", sent_at is not None), dry_run=bool(result.get("dry_run")),
           elapsed_ms=round((time.monotonic() - t0) * 1000))
    log.info("下单结果 court=%s gymId=%s HTTP=%s success=%s uncertain=%s msg=%s",
             court.no, gym_id, result.get("http_status"), bool(result.get("success")),
             bool(result.get("uncertain")), result.get("msg", ""))
    return result


# ══════════════════════════════════════════════════════
# Windows 窗口操作（refresh reservation 窗口）
# ══════════════════════════════════════════════════════
_cached_hwnd: int = 0


def _find_hwnd() -> int:
    import win32gui
    found: list[int] = []

    def _cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd) or ""
            if title and any(k in title for k in WINDOW_KEYWORDS):
                found.append(hwnd)
        return True

    win32gui.EnumWindows(_cb, None)
    return found[0] if found else 0


def _valid_hwnd(hwnd: int) -> bool:
    if not hwnd:
        return False
    try:
        import win32gui
        if not win32gui.IsWindow(hwnd):
            return False
        title = win32gui.GetWindowText(hwnd) or ""
        return any(k in title for k in WINDOW_KEYWORDS)
    except Exception:
        return False


def _ensure_foreground(hwnd: int, timeout: float = 0.6) -> bool:
    """轮询直到 hwnd 真正成为前台窗口，或超时。"""
    import win32gui
    import win32process
    import win32api

    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            if win32gui.GetForegroundWindow() == hwnd:
                return True
        except Exception:
            pass

        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        try:
            if win32gui.GetForegroundWindow() == hwnd:
                return True
        except Exception:
            pass

        try:
            fg = win32gui.GetForegroundWindow()
            if fg and fg != hwnd:
                fg_tid = win32process.GetWindowThreadProcessId(fg)[0]
                my_tid = win32api.GetCurrentThreadId()
                if fg_tid != my_tid:
                    win32process.AttachThreadInput(fg_tid, my_tid, True)
                    try:
                        win32gui.BringWindowToTop(hwnd)
                        win32gui.SetForegroundWindow(hwnd)
                    finally:
                        win32process.AttachThreadInput(fg_tid, my_tid, False)
        except Exception:
            pass

        try:
            if win32gui.GetForegroundWindow() == hwnd:
                return True
        except Exception:
            pass

        if time.monotonic() >= deadline:
            return False
        time.sleep(0.03)


def _mouse_click(x: int, y: int) -> None:
    import win32api
    import win32con

    win32api.SetCursorPos((x, y))
    time.sleep(0.03)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.03)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def _send_ctrl_r() -> None:
    import win32api
    import win32con

    VK_R = 0x52
    win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    time.sleep(0.02)
    win32api.keybd_event(VK_R, 0, 0, 0)
    time.sleep(0.02)
    win32api.keybd_event(VK_R, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(0.02)
    win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)


def refresh_window(
    pre_click_delay: float = 0.10,
    post_click_delay: float = 0.10,
    fg_timeout: float = 0.6,
    double_click: bool = True,
) -> bool:
    """找到 reservation 窗口 → 上前台 → 点击 → Ctrl+R。"""
    global _cached_hwnd

    try:
        import win32gui
        import win32con
        import win32api
    except ImportError:
        print("[refresh] 需要 pywin32：pip install pywin32")
        return False

    hwnd = _cached_hwnd if _valid_hwnd(_cached_hwnd) else 0
    if not hwnd:
        hwnd = _find_hwnd()
        if hwnd:
            _cached_hwnd = hwnd

    if not hwnd:
        print("[refresh] 未找到预约窗口（企业微信是否已打开 reservation？）")
        _cached_hwnd = 0
        return False

    try:
        old_pos = win32api.GetCursorPos()
    except Exception:
        old_pos = None

    ok = False
    try:
        print(f"[refresh] 找到预约窗口: {win32gui.GetWindowText(hwnd)}")

        try:
            if win32gui.GetWindowPlacement(hwnd)[1] == win32con.SW_SHOWMINIMIZED:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.15)
        except Exception:
            pass

        if not _ensure_foreground(hwnd, timeout=fg_timeout):
            print("[refresh] ⚠ 未能在前台锁定窗口，仍然尝试点击…")

        if pre_click_delay > 0:
            time.sleep(pre_click_delay)

        cx = cy = None
        try:
            rect = win32gui.GetWindowRect(hwnd)
            w = rect[2] - rect[0]
            h = rect[3] - rect[1]
            cx = rect[0] + w // 2
            cy = rect[1] + max(100, int(h * 0.20))
        except Exception as e:
            print(f"[refresh] 计算点击坐标失败（忽略）: {e}")

        if cx is not None and cy is not None:
            try:
                _mouse_click(cx, cy)
                if double_click:
                    time.sleep(0.08)
                    _mouse_click(cx, cy)
            except Exception as e:
                print(f"[refresh] click 失败（忽略）: {e}")

        if post_click_delay > 0:
            time.sleep(post_click_delay)

        if not _ensure_foreground(hwnd, timeout=0.4):
            print("[refresh] ⚠ 发送 Ctrl+R 前窗口不在前台，仍尝试发送…")
            if cx is not None and cy is not None:
                try:
                    _mouse_click(cx, cy)
                    time.sleep(0.08)
                except Exception:
                    pass

        _send_ctrl_r()
        ok = True
    except Exception as e:
        print(f"[refresh] error: {e}")
        _cached_hwnd = 0
    finally:
        if old_pos is not None:
            try:
                win32api.SetCursorPos(old_pos)
            except Exception:
                pass

    if ok:
        print("[refresh] 已发送 Ctrl+R")
    return ok


# ══════════════════════════════════════════════════════
# mitmproxy 抓 token（供 mitm_addon.py 调用）
# ══════════════════════════════════════════════════════
_last_token_seen = ""
_last_token_seen_at = 0.0


def capture_token_from_flow(
    flow,
    host: str = RESERVATION_HOST,
    config_path: Path = CONFIG_PATH,
) -> None:
    """MITM hook: notify main process via authenticated loopback IPC (no TOML write)."""
    global _last_token_seen, _last_token_seen_at

    try:
        if flow.request.pretty_host != host:
            return
    except Exception:
        return

    from network.capture_protocol import (
        build_capture_payload,
        is_allowed_capture_path,
        post_capture_sync,
    )

    path = flow.request.path.split("?", 1)[0]
    if not is_allowed_capture_path(path):
        return

    token = flow.request.query.get("token")
    userid = flow.request.query.get("userid") or ""
    if not token:
        return

    now = time.monotonic()
    if token == _last_token_seen and now - _last_token_seen_at < 1.0:
        return
    _last_token_seen = token
    _last_token_seen_at = now

    ts = time.strftime("%H:%M:%S")
    payload = build_capture_payload(
        token=str(token),
        userid=str(userid or ""),
        path=path,
        host=host,
        source="mitm",
    )
    ok, ipc_detail = post_capture_sync(payload)
    if ok:
        print(f"[{ts}] [TOKEN] IPC ok {str(token)[:8]}... {path}")
    else:
        reason = f" ({ipc_detail})" if ipc_detail else ""
        what = "IPC rejected" if ipc_detail.startswith("工作台未采用") else "IPC failed"
        print(f"[{ts}] [TOKEN] {what} {str(token)[:8]}... {path}{reason}")

def kill_port_holder(host: str, port: int) -> list[int]:
    """强杀所有监听 host:port 的进程，返回被杀的 PID 列表。

    Windows: netstat -ano 找 PID → taskkill /F
    Linux/Mac: lsof -ti 找 PID → SIGKILL
    """
    pids: set[int] = set()

    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=5,
            ).stdout
        except Exception as e:
            log.warning("[kill-port] netstat 失败: %s", e)
            return []
        needle = f":{port}"
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 5 or parts[3] != "LISTENING":
                continue
            if not parts[1].endswith(needle):
                continue
            try:
                pids.add(int(parts[4]))
            except ValueError:
                pass
    else:
        try:
            out = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            for tok in out.split():
                try:
                    pids.add(int(tok))
                except ValueError:
                    pass
        except Exception as e:
            log.warning("[kill-port] lsof 失败: %s", e)
            return []

    killed: list[int] = []
    for pid in pids:
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True, timeout=5,
                )
            else:
                os.kill(pid, signal.SIGKILL)
            killed.append(pid)
        except Exception as e:
            log.warning("[kill-port] 结束 PID %d 失败: %s", pid, e)
    return killed

# ══════════════════════════════════════════════════════
# mitmdump 底层封装
# ══════════════════════════════════════════════════════
def port_listening(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def build_mitm_cmd() -> list[str] | None:
    script = str(ROOT / "mitm_addon.py")
    args: list[str] = [
        "--listen-host", MITM_HOST,
        "--listen-port", str(MITM_PORT),
        "-s", script,
    ]
    if MITM_UPSTREAM:
        args += ["--mode", f"upstream:{MITM_UPSTREAM}"]

    exe_name = "mitmdump.exe" if sys.platform == "win32" else "mitmdump"

    local = Path(sys.executable).with_name(exe_name)
    if local.exists():
        return [str(local)] + args

    # mitm_addon imports this app's modules, so prefer this interpreter's mitmproxy over
    # whatever mitmdump happens to be on PATH (the release package ships no mitmdump.exe).
    try:
        import mitmproxy  # noqa: F401
        return [sys.executable, "-m", "mitmproxy.tools.main", "dump"] + args
    except Exception:
        pass

    exe = shutil.which("mitmdump")
    return [exe] + args if exe else None


# 白名单：只有命中这些关键字的行才进 log
_KEEP_RE = re.compile(
    r"\[TOKEN\]"
    r"|\[VENUE\]"
    r"|\[QYWX\]"
    r"|listening at|Proxy server"
    r"|error|Error|ERROR"
    r"|warn|Warn|WARNING|WARN"
    r"|Traceback|Exception"
)

# 黑名单：即便命中白名单，也要丢弃的行（噪音）
_DROP_RE = re.compile(
    r"TLS handshake failed"
    r"|Client TLS"
    r"|unknown ca"
    r"|does not trust the proxy"
)


def _decode_mitm(raw: bytes) -> str:
    """mitmdump 子进程在 Windows 上默认用 cp936(GBK) 写 stdout，
    这里按 UTF-8 → GBK → 兜底 的顺序解码，避免中文变问号。"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _mitm_reader(pipe) -> None:
    try:
        for raw in iter(pipe.readline, b""):
            if not raw:
                break
            txt = _decode_mitm(raw).rstrip()
            if not txt:
                continue
            if _DROP_RE.search(txt):        # 先杀噪音
                continue
            if _KEEP_RE.search(txt):        # 再走白名单
                log.info("[mitm] %s", txt)
    except Exception as e:
        log.warning("[mitm] reader 异常: %s", e)
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def spawn_mitmdump(extra_env: dict[str, str] | None = None) -> subprocess.Popen | None:
    cmd = build_mitm_cmd()
    if cmd is None:
        log.error("[mitm] 未找到 mitmdump —— 请先 `pip install mitmproxy`")
        return None

    log.info("[mitm] 启动命令: %s", cmd[0])
    log.info("[mitm] 参数: %s", " ".join(cmd[1:]))

    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    try:
        proc = subprocess.Popen(
            cmd, cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=creationflags, bufsize=0,
            env=env,
        )
    except FileNotFoundError as e:
        log.error("[mitm] 可执行文件不存在: %s", e)
        return None
    except Exception as e:
        log.error("[mitm] 启动失败: %s", e)
        return None

    threading.Thread(target=_mitm_reader, args=(proc.stdout,), daemon=True).start()
    _bind_to_this_process(proc)
    return proc


_JOB_HANDLES: list = []


def _bind_to_this_process(proc: subprocess.Popen) -> None:
    """Windows: put mitmdump in a kill-on-close job held by this process.

    If the workbench is ended without a clean shutdown (Task Manager, crash), Windows closes
    the job handle and ends mitmdump too, so port 8080 is not left held by an orphan.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject")
        if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(int(proc._handle))):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject")
        _JOB_HANDLES.append(job)  # stays open for this process's lifetime
    except Exception as e:  # never block capture on this safeguard
        log.warning("[mitm] 未能绑定子进程生命周期（不影响抓取）: %s", e)
