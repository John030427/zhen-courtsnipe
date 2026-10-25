"""Double-click entry for the workbench.

    pythonw launcher.py                 start it once (or reuse the running one) and open the browser
    pythonw launcher.py --stop          ask the running workbench to exit
    pythonw launcher.py --stop --quiet  the same without dialogs (installer); exit 3 = tasks running
    pythonw launcher.py --import-from D copy config.toml / watch-orders.json from an older folder

Readiness means /api/health answered as this app, not a fixed sleep. It never kills another
process, never binds anything but 127.0.0.1, and does not start tasks or touch the school API.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from app_paths import APP_ROOT, DATA_DIR, LOG_DIR, ensure_data_dir, import_from

APP_ID = "sustech-reservation"
TITLE = "南科大场地预约工作台"
START_TIMEOUT = 60.0
TASKS_RUNNING = 3  # --stop --quiet: refused because a rush / watch is running (pass --force to stop it)
LOG_ROTATE_BYTES = 5 * 1024 * 1024
# Windows process creation flags (spelled out so the module also imports elsewhere).
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000


def app_port() -> int:
    return int(os.environ.get("SUSTECH_APP_PORT") or 8766)


def base_url() -> str:
    return f"http://127.0.0.1:{app_port()}"


def _request(path: str, *, body: dict | None = None, headers: dict | None = None,
             timeout: float = 2.0) -> tuple[int, dict | None]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base_url() + path, data=data, method="POST" if data else "GET",
                                     headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with opener.open(request, timeout=timeout) as reply:
            status, raw = reply.status, reply.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    try:
        return status, json.loads(raw)
    except (ValueError, TypeError):
        return status, None


def health() -> dict | None:
    """This app's health payload, or None if nothing (or something else) answers."""
    try:
        status, payload = _request("/api/health", timeout=1.5)
    except OSError:
        return None
    return payload if status == 200 and isinstance(payload, dict) and payload.get("app") == APP_ID else None


def port_in_use() -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", app_port())) == 0


def notify(text: str, *, ask: bool = False) -> bool:
    # SUSTECH_NO_DIALOG=1 (CI smoke tests) prints instead of blocking on a dialog; questions get "no".
    if sys.platform == "win32" and not os.environ.get("SUSTECH_NO_DIALOG"):
        import ctypes

        flags = (0x4 | 0x30) if ask else 0x40  # MB_YESNO|MB_ICONWARNING or MB_ICONINFORMATION
        return ctypes.windll.user32.MessageBoxW(None, text, TITLE, flags | 0x10000) == 6  # IDYES; topmost
    print(text, flush=True)
    if not ask or os.environ.get("SUSTECH_NO_DIALOG"):
        return False
    return input("[y/N] ").strip().lower() == "y"


class InstanceLock:
    """Cross-process lock so two double-clicks cannot both start a server."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def acquire(self) -> bool:
        self.handle = open(self.path, "a+")
        try:
            if sys.platform == "win32":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.handle.close()
            self.handle = None
            return False

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle, fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def wait_for_health(timeout: float, proc: subprocess.Popen | None = None) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = health()
        if found:
            return found
        if proc is not None and proc.poll() is not None:
            return None
        time.sleep(0.5)
    return None


def log_tail(lines: int = 12) -> str:
    path = LOG_DIR / "server.log"
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def server_executable() -> str:
    executable = Path(sys.executable)
    if sys.platform == 'win32' and executable.name.lower() == 'pythonw.exe':
        console = executable.with_name('python.exe')
        if console.is_file():
            return str(console)
    return str(executable)


def spawn_server() -> subprocess.Popen:
    log_path = LOG_DIR / "server.log"
    if log_path.exists() and log_path.stat().st_size > LOG_ROTATE_BYTES:
        log_path.replace(log_path.with_suffix(".log.1"))
    log = open(log_path, "a", encoding="utf-8")
    env = {**os.environ, "SUSTECH_APP_PORT": str(app_port()), "PYTHONUTF8": "1"}
    kwargs: dict = {"cwd": str(APP_ROOT), "stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT,
                    "env": env}
    # Keep outbound traffic on the python.exe path used by existing capture bypass rules.
    # The Windows creation flags below keep the server windowless even with python.exe.
    cmd = [server_executable(), str(APP_ROOT / "serve.py")]
    try:
        if sys.platform != "win32":
            return subprocess.Popen(cmd, start_new_session=True, **kwargs)
        # Windows may forbid leaving the launcher's job. Fall back only on access denial.
        # DETACHED_PROCESS disables CREATE_NO_WINDOW. A venv redirector can then
        # create a visible console when it starts the underlying interpreter.
        flags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        try:
            return subprocess.Popen(cmd, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kwargs)
        except PermissionError:
            return subprocess.Popen(cmd, creationflags=flags, **kwargs)
    finally:
        # Popen duplicated the handle for the child; the launcher no longer needs its copy.
        log.close()


def default_open(url: str) -> None:
    if not os.environ.get("SUSTECH_NO_BROWSER"):
        webbrowser.open(url)
    print(f"OPEN {url}", flush=True)


def start(open_browser=default_open) -> int:
    ensure_data_dir()
    if health():
        open_browser(base_url() + "/")
        return 0
    lock = InstanceLock(DATA_DIR / "launcher.lock")
    if not lock.acquire():
        # Another double-click is already starting it; wait for that one.
        if wait_for_health(START_TIMEOUT):
            open_browser(base_url() + "/")
            return 0
        notify(f"工作台没有在 {int(START_TIMEOUT)} 秒内启动。\n日志：{LOG_DIR / 'server.log'}")
        return 1
    try:
        if health():
            open_browser(base_url() + "/")
            return 0
        if port_in_use():
            notify(f"端口 {app_port()} 被其他程序占用，工作台无法启动。\n"
                   "请关闭占用该端口的程序后再试；本工具不会自动结束其他程序。")
            return 1
        proc = spawn_server()
        if wait_for_health(START_TIMEOUT, proc):
            open_browser(base_url() + "/")
            return 0
        reason = "服务启动后退出了" if proc.poll() is not None else f"{int(START_TIMEOUT)} 秒内没有就绪"
        notify(f"工作台启动失败：{reason}。\n日志：{LOG_DIR / 'server.log'}\n\n{log_tail()}")
        return 1
    finally:
        lock.release()


def stop(quiet: bool = False, force: bool = False) -> int:
    # quiet: the installer / uninstaller calls this and shows its own messages, so no dialogs here.
    say = (lambda text: None) if quiet else notify
    if not health():
        say("工作台没有在运行。")
        return 0
    headers = {"X-Workbench-Action": "shutdown"}
    status, reply = _request("/api/admin/shutdown", body={"force": force}, headers=headers, timeout=5)
    if status == 409:
        if quiet:
            return TASKS_RUNNING
        tasks = "、".join((reply or {}).get("active") or [])
        if not notify(f"以下任务正在运行：{tasks}\n退出服务会停止这些任务。确定退出吗？", ask=True):
            return 1
        status, reply = _request("/api/admin/shutdown", body={"force": True}, headers=headers, timeout=5)
    if status != 200 or not (reply or {}).get("ok"):
        say(f"工作台没有退出：{(reply or {}).get('msg') or status}")
        return 1
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and health():
        time.sleep(0.5)
    if quiet:
        return 1 if health() else 0
    notify("工作台已退出。" if not health() else "工作台还在退出中，请稍后再看。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=TITLE)
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="with --stop: no dialogs, exit 3 if tasks are running")
    parser.add_argument("--force", action="store_true", help="with --stop --quiet: stop running tasks too")
    parser.add_argument("--import-from", type=Path, metavar="FOLDER")
    args = parser.parse_args(argv)
    if args.import_from:
        copied = import_from(args.import_from)
        notify(f"已导入：{'、'.join(copied)}" if copied else "没有导入任何文件（目标里已有同名文件，或旧目录里没有）。")
        return 0
    return stop(quiet=args.quiet, force=args.force) if args.stop else start()


if __name__ == "__main__":
    sys.exit(main())
