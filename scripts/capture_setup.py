"""One-click capture setup (Windows): route only reservation.sustech.edu.cn to the local mitm.

Run through setup-capture.cmd / undo-capture.cmd with the workbench already running on 8766.
Every change asks first, is backed up under %LOCALAPPDATA%\\SustechReservation\\capture-setup
and recorded in state.json so --undo can reverse exactly what this script did.

Unlike the upstream setup.ps1 it never elevates, never kills other processes, never restarts
Clash, never overwrites an unrecognised Clash script, and trusts the CA for the current user only.
The pure helpers (script composition, config edit, thumbprint) are platform-neutral for tests.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import tomlkit

ROOT = Path(__file__).resolve().parents[1]
# Installed: Start menu entry; portable zip: 撤销自动获取配置.cmd. Both carry this name.
UNDO = "「撤销自动获取配置」" if (ROOT / "PACKAGED").exists() else "undo-capture.cmd"
START = "桌面「南科大场地预约」" if (ROOT / "PACKAGED").exists() else "start-workbench.cmd"
sys.path.insert(0, str(ROOT))
from app_paths import DATA_DIR  # noqa: E402  (repo folder in a checkout, %LOCALAPPDATA% in the package)
WORKBENCH = "http://127.0.0.1:8766"
HOST = "reservation.sustech.edu.cn"
PAC_URL = f"{WORKBENCH}/proxy.pac"
BEGIN = "// ==== SUSTech reservation capture BEGIN ===="
END = "// ==== SUSTech reservation capture END ===="
RENAMED_MAIN = "userMainBeforeSustech"
MAIN_RE = re.compile(r"function\s+main\s*\(")
INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
KNOWN_CLIENTS = {
    "clash-verge.exe": "Clash Verge Rev", "verge-mihomo.exe": "Clash Verge Rev",
    "clash for windows.exe": "Clash for Windows", "mihomo party.exe": "Mihomo Party",
    "mihomo-party.exe": "Mihomo Party", "v2rayn.exe": "v2rayN", "flclash.exe": "FlClash",
    "clash-nyanpasu.exe": "Clash Nyanpasu", "nekobox.exe": "NekoBox", "hiddify.exe": "Hiddify",
}


class SetupStop(Exception):
    """A condition this script will not handle on its own; the message says what to do."""


# ── pure helpers ────────────────────────────────────────────────────────────

def extension_source(path: Path = ROOT / "clash-extension.js") -> str:
    text = path.read_text(encoding="utf-8")
    text = "\n".join(line for line in text.splitlines() if not line.startswith("module.exports"))
    if "function applySustechMitmExtension" not in text:
        raise SetupStop("clash-extension.js 中找不到 applySustechMitmExtension，仓库文件可能不完整")
    return text.strip()


def strip_capture_block(script: str) -> tuple[str, bool]:
    """Remove this script's block and restore the user's main name. Returns (script, had_block)."""
    if BEGIN not in script:
        return script, False
    start = script.index(BEGIN)
    end = script.index(END, start) + len(END)
    rest = (script[:start].rstrip("\n") + "\n" + script[end:].lstrip("\n")).strip("\n")
    rest = rest.replace(f"function {RENAMED_MAIN}(", "function main(", 1)
    return (rest + "\n") if rest else "", True


def compose_verge_script(existing: str, extension_js: str, process_paths: list[str]) -> str:
    """Wrap the user's Clash Verge global script so it runs first, then add the capture rule."""
    base, _ = strip_capture_block(existing)
    if "BADMINTON_SETUP_BEGIN" in base or HOST in base:
        raise SetupStop("Clash 全局扩展脚本里已经有预约域名的规则（可能来自原项目的 setup.ps1 或手动添加）。"
                        "为避免出现两份规则，本脚本不改动；请先用原项目的 uninstall 或手动删除旧规则，或改用 agent 渠道。")
    mains = MAIN_RE.findall(base)
    if base.strip() and len(mains) != 1:
        raise SetupStop("Clash 全局扩展脚本不是常见的单个 function main(...) 写法，本脚本不会覆盖它。"
                        "请改用 agent 渠道，或按 README_zh.md 手动合并。")
    body = MAIN_RE.sub(f"function {RENAMED_MAIN}(", base, count=1) if base.strip() else ""
    call_user = f"  config = {RENAMED_MAIN}(config, profileName) || config;\n" if body else ""
    options = json.dumps({"processPaths": process_paths}, ensure_ascii=False) if process_paths else "{}"
    block = (f"{BEGIN}\n"
             "// 由「配置自动获取」脚本添加；撤销请运行撤销脚本。BEGIN 与 END 之间请整体保留或整体删除。\n"
             f"{extension_js}\n\n"
             "function main(config, profileName) {\n"
             f"{call_user}"
             f"  return applySustechMitmExtension(config, {options});\n"
             "}\n"
             f"{END}\n")
    return (body.rstrip("\n") + "\n\n" + block) if body else block


def set_capture_mode(text: str, mode: str) -> tuple[str, str | None]:
    """Return (new TOML text, previous mode); every other line stays as it was."""
    doc = tomlkit.parse(text)
    token = doc.get("token")
    if token is None:
        doc["token"] = tomlkit.table()
        token = doc["token"]
    previous = token.get("capture_mode")
    token["capture_mode"] = mode
    return tomlkit.dumps(doc), (str(previous) if previous is not None else None)


def cert_thumbprint(cert_text: str) -> str:
    """SHA-1 thumbprint of a PEM (or raw DER) certificate, as Windows shows it."""
    match = re.search(r"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", cert_text, re.S)
    der = base64.b64decode("".join(match.group(1).split())) if match else cert_text.encode("latin-1")
    return hashlib.sha1(der).hexdigest().upper()


def verge_setting(text: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}\s*:\s*['\"]?([^'\"\s#]+)", text, re.M)
    return match.group(1) if match else None


# ── environment ─────────────────────────────────────────────────────────────

def state_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "SustechReservation" / "capture-setup"


def load_state() -> dict:
    path = state_dir() / "state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_state(state: dict) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    (state_dir() / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def backup(path: Path) -> Path:
    state_dir().mkdir(parents=True, exist_ok=True)
    target = state_dir() / f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}"
    target.write_bytes(path.read_bytes())
    return target


def workbench(path: str, *, method: str = "GET", timeout: float = 10.0, body: dict | None = None) -> dict | str:
    # Loopback only, and never through a system/Clash proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = json.dumps(body or {}).encode() if method == "POST" else None
    request = urllib.request.Request(WORKBENCH + path, method=method, data=data,
                                     headers={"Content-Type": "application/json"})
    with opener.open(request, timeout=timeout) as reply:
        body = reply.read().decode("utf-8")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


def running_clients() -> list[str]:
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    names = {line.split('","')[0].strip('"').lower() for line in out.splitlines() if line}
    return sorted({label for exe, label in KNOWN_CLIENTS.items() if exe in names})


def verge_root() -> Path | None:
    candidates = [os.environ.get("CLASH_VERGE_CONFIG_DIR")]
    for env in ("APPDATA", "LOCALAPPDATA"):
        if os.environ.get(env):
            candidates.append(os.path.join(os.environ[env], "io.github.clash-verge-rev.clash-verge-rev"))
    for candidate in candidates:
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    return None


def read_proxy_settings() -> dict:
    import winreg

    values = {}
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS) as key:
        for name in ("ProxyEnable", "ProxyServer", "AutoConfigURL"):
            try:
                values[name] = winreg.QueryValueEx(key, name)[0]
            except FileNotFoundError:
                values[name] = None
    return values


def delete_auto_config_url() -> None:
    """Undo fallback when the Settings toggle was left on; the user flips the toggle itself."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS, 0, winreg.KEY_SET_VALUE) as key:
        try:
            winreg.DeleteValue(key, "AutoConfigURL")
        except FileNotFoundError:
            pass


def open_proxy_settings(url: str | None = None) -> None:
    if url:
        subprocess.run(["clip"], input=url.encode("utf-16-le"), check=False)
    os.startfile("ms-settings:network-proxy")  # type: ignore[attr-defined]


def certutil(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["certutil", "-user", *args], capture_output=True, text=True)


def interpreter_paths() -> list[str]:
    """The real interpreter(s) the workbench reaches the school from. This script runs under
    python.exe, but the launcher starts the server, and with it mitmdump, under pythonw.exe."""
    base = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    found = [base] + [base.with_name(n) for n in ("python.exe", "pythonw.exe") if base.with_name(n) != base]
    return [str(path) for path in found if path == base or path.exists()]


# ── interaction ─────────────────────────────────────────────────────────────

def say(text: str = "") -> None:
    print(text, flush=True)


def ask(question: str) -> bool:
    while True:
        answer = input(f"{question} [Y/N] ").strip().lower()
        if answer in ("y", "yes", "是"):
            return True
        if answer in ("n", "no", "否", ""):
            return False


def choose(question: str, options: dict[str, str]) -> str:
    for key, label in options.items():
        say(f"  {key}. {label}")
    while True:
        answer = input(f"{question} ").strip().upper()
        if answer in options:
            return answer


# ── steps ───────────────────────────────────────────────────────────────────

def survey() -> dict:
    info: dict = {}
    try:
        workbench("/api/config")
        info["workbench"] = True
        capture = workbench("/api/capture/clash")
        info["capture"] = capture if isinstance(capture, dict) else {}
    except OSError:
        info["workbench"] = False
        info["capture"] = {}
    info["clients"] = running_clients()
    root = verge_root()
    info["verge_root"] = str(root) if root else None
    if root:
        def read(name: str) -> str:
            path = root / name
            return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""

        verge = read("verge.yaml")
        # clash-verge.yaml is the generated runtime config; config.yaml is the base.
        clash = read("clash-verge.yaml") or read("config.yaml")
        info["verge_tun"] = verge_setting(verge, "enable_tun_mode") == "true"
        info["verge_sysproxy"] = verge_setting(verge, "enable_system_proxy") == "true"
        info["clash_mode"] = verge_setting(clash, "mode")
        info["fake_ip_filter_mode"] = verge_setting(clash, "fake-ip-filter-mode")
    try:
        info["proxy"] = read_proxy_settings()
    except (ImportError, OSError):
        info["proxy"] = {}
    cert = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.cer"
    info["ca_file"] = cert.exists()
    return info


def print_survey(info: dict) -> None:
    proxy = info.get("proxy") or {}
    rows = [
        ("工作台（127.0.0.1:8766）", "运行中" if info["workbench"] else "未运行"),
        ("抓取模式", "已开启" if info["capture"].get("enabled") else "未开启（当前为页面脚本模式）"),
        ("代理软件", "、".join(info["clients"]) or "没有发现"),
        ("Clash Verge 配置目录", info.get("verge_root") or "没有发现"),
        ("Clash 模式 / TUN / 系统代理", f"{info.get('clash_mode') or '-'} / {'开' if info.get('verge_tun') else '关'}"
                                       f" / {'开' if info.get('verge_sysproxy') else '关'}" if info.get("verge_root") else "-"),
        ("Windows 手动代理", f"{'开' if proxy.get('ProxyEnable') else '关'} {proxy.get('ProxyServer') or ''}".strip()),
        ("Windows 设置脚本", proxy.get("AutoConfigURL") or "未设置"),
        ("mitmproxy 证书文件", "已生成" if info["ca_file"] else "未生成"),
    ]
    say("\n本机情况（只读检查，没有做任何修改）：")
    for name, value in rows:
        say(f"  · {name}：{value}")


def enable_capture(state: dict) -> None:
    config = DATA_DIR / "config.toml"
    if not config.exists():
        raise SetupStop(f"找不到 config.toml，请先运行一次工作台（{START}）")
    text = config.read_text(encoding="utf-8")
    new_text, previous = set_capture_mode(text, "mitm")
    if previous != "mitm":
        say(f"\n需要把 config.toml 的 [token] capture_mode 从 {previous or '默认'} 改为 mitm（其他内容不动）。")
        if not ask("同意修改吗？"):
            raise SetupStop("已取消，没有做任何修改")
        saved = backup(config)
        config.write_text(new_text, encoding="utf-8")
        state.update(config_backup=str(saved), capture_mode_before=previous)
        save_state(state)
        say(f"  已修改，备份在 {saved}")
        time.sleep(2)
    restart = workbench("/api/mitm/restart", method="POST", timeout=30)
    mitm = workbench("/api/mitm/state")
    if not (isinstance(restart, dict) and restart.get("ok") and mitm.get("proc_alive")):
        if mitm.get("port_conflict"):
            raise SetupStop("8080 端口被其他程序占用，本脚本不会结束它。请关闭占用 8080 的程序后重试。")
        raise SetupStop("本机抓取代理（mitmdump）没有启动成功，请看工作台运行日志")
    say("  本机抓取代理已在 127.0.0.1:8080 运行")


def trust_certificate(state: dict) -> None:
    cert = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.cer"
    for _ in range(40):
        if cert.exists():
            break
        time.sleep(0.5)
    if not cert.exists():
        raise SetupStop("没有找到 mitmproxy 证书文件，抓取代理可能没有正常启动")
    thumb = cert_thumbprint(cert.read_text(encoding="latin-1"))
    if certutil("-store", "Root", thumb).returncode == 0:
        say(f"  证书已在当前用户的信任列表中（指纹 {thumb[:8]}…），跳过")
        return
    say("\n需要让企业微信信任本机抓取代理的证书：")
    say("  · 只装到「当前用户」，不需要管理员权限；Windows 会弹出安全警告，请点「是」")
    say("  · 这是这台电脑上 mitmproxy 自己生成的证书，私钥只在 %USERPROFILE%\\.mitmproxy，请勿分享该文件夹")
    say(f"  · 撤销：运行 {UNDO}，或 certutil -user -delstore Root {thumb}")
    if not ask("同意安装证书吗？"):
        raise SetupStop("已取消安装证书；没有证书无法抓取。可以改用页面脚本方式")
    if certutil("-addstore", "Root", str(cert)).returncode != 0:
        raise SetupStop("证书没有安装（可能在弹窗里选了「否」）")
    state.update(ca_thumbprint=thumb)
    save_state(state)
    say("  证书已安装。请完全退出企业微信（托盘图标右键退出）再重新打开。")


def route_clash_verge(info: dict, state: dict) -> None:
    root = Path(info["verge_root"])
    if info.get("clash_mode") and info["clash_mode"] != "rule":
        raise SetupStop(f"Clash 当前是 {info['clash_mode']} 模式，规则只在「规则（rule）」模式下生效。"
                        "请自己在 Clash Verge 切到规则模式后重新运行；本脚本不替你切换。")
    if not (info.get("verge_tun") or info.get("verge_sysproxy")):
        raise SetupStop("Clash Verge 的「系统代理」和「TUN」都没开，企业微信的流量不经过 Clash。"
                        "请自己打开其中一个，或改用「Windows 设置脚本」路线。")
    paths: list[str] = []
    if info.get("verge_tun"):
        if (info.get("fake_ip_filter_mode") or "blacklist") != "blacklist":
            raise SetupStop("TUN 模式下 DNS 不是 blacklist 过滤，需要人工组合规则，请改用 agent 渠道")
        paths = interpreter_paths()
        say(f"\nTUN 已开启：会让工作台自己的 Python（{'、'.join(paths)}）访问学校时直连，避免回环。")
        say("  如果 Clash 连接面板里看到的进程路径和这个不同，请改用 agent 渠道。")
    script = root / "profiles" / "Script.js"
    existing = script.read_text(encoding="utf-8") if script.exists() else ""
    new_script = compose_verge_script(existing, extension_source(), paths)
    say(f"\n将修改 Clash Verge 全局扩展脚本：{script}")
    say("  · 原有脚本完整保留并先执行，之后只追加一条预约域名 → 127.0.0.1:8080 的规则")
    say("  · 不会重启 Clash，改完需要你在 Clash Verge 里点一次刷新/重新加载（或重启 Clash Verge）")
    if not ask("同意修改吗？"):
        raise SetupStop("已取消，没有修改 Clash")
    script.parent.mkdir(parents=True, exist_ok=True)
    if "verge_script" not in state:  # keep the pre-setup backup on re-runs
        if script.exists():
            state.update(verge_backup=str(backup(script)))
        else:
            state.update(verge_created=True)
    script.write_text(new_script, encoding="utf-8")
    state.update(verge_script=str(script))
    save_state(state)
    say("  已写入。请打开 Clash Verge →「订阅」页点刷新，或重启 Clash Verge。")


def route_windows_pac(info: dict, state: dict) -> None:
    proxy = info.get("proxy") or {}
    if proxy.get("AutoConfigURL") == PAC_URL:
        say("\nWindows「使用设置脚本」已经指向工作台，跳过")
        return
    if proxy.get("ProxyEnable") or proxy.get("AutoConfigURL"):
        raise SetupStop("Windows 已经设置了别的代理或设置脚本（可能是其他 VPN/代理软件），本脚本不会覆盖。"
                        "请先确认那个设置还要不要，或改用页面脚本方式。")
    pac = workbench("/proxy.pac")
    if "PROXY 127.0.0.1:8080" not in str(pac):
        raise SetupStop("工作台的 /proxy.pac 没有指向本机抓取代理，请确认抓取模式已开启并已更新到新版工作台")
    say(f"\n需要在 Windows 代理设置里打开「使用设置脚本」，地址填 {PAC_URL}")
    say("  · 只有预约网站经过本机抓取代理，其他网站和企业微信聊天仍然直连")
    say(f"  · 撤销：运行 {UNDO}，或在同一页面关掉「使用设置脚本」")
    if not ask("现在打开 Windows 代理设置页面，并把地址复制到剪贴板吗？"):
        raise SetupStop("已取消，没有修改 Windows 代理设置")
    open_proxy_settings(PAC_URL)
    say("  在打开的页面：「使用设置脚本」→ 设置 → 打开开关 → 脚本地址里粘贴（Ctrl+V）→ 保存")
    input("  完成后回到这里按回车…")
    if (read_proxy_settings().get("AutoConfigURL") or "") != PAC_URL:
        raise SetupStop("没有检测到设置脚本地址，请确认已在设置页保存，然后重新运行本脚本")
    state.update(pac_set=True)
    save_state(state)
    say("  已生效。请完全退出企业微信再重新打开。")
    say(f"  注意：企业微信内置浏览器是否遵守这项设置尚未验证；如果下面一直等不到，就是不遵守，请运行 {UNDO}。")


def offer_auto_recovery(state: dict) -> None:
    """Without this, an expired login still needs a manual visit to the official page."""
    status = workbench("/api/connect/state")
    if isinstance(status, dict) and (status.get("auto_recovery") or {}).get("enabled"):
        say("\n「登录失效时自动刷新预约页并重新获取」已开启")
        return
    say("\n建议开启「登录失效时自动刷新预约页并重新获取」：")
    say("  · 学校判定登录失效时，工作台刷新企业微信里的预约页，抓到新登录后先只读验证")
    say("  · 需要预约页一直开着；最多每 3 分钟尝试一次，下单过程中不会刷新")
    if not ask("要开启吗？"):
        return
    reply = workbench("/api/connect/recovery", method="POST", body={"enabled": True})
    if isinstance(reply, dict) and reply.get("ok"):
        state.update(recovery_enabled=True)
        save_state(state)
        say("  已开启")


def wait_for_capture(seconds: int = 300) -> bool:
    say("\n现在请在企业微信里打开南科大场地预约页面（已经开着就刷新一下）。正在等待抓取（最多 5 分钟，Ctrl+C 结束）…")
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        capture = workbench("/api/capture/clash")
        if isinstance(capture, dict) and capture.get("captured"):
            say("  ✓ 已抓到登录信息")
            return True
        time.sleep(3)
    say("  ✗ 5 分钟内没有抓到。请确认企业微信已重启、Clash 已重新加载，然后重新运行本脚本检查。")
    return False


def setup() -> None:
    say(f"南科大场地预约 · 一键配置自动获取登录\n每一步修改前都会先问你；随时可以运行 {UNDO} 撤销。")
    info = survey()
    print_survey(info)
    if not info["workbench"]:
        raise SetupStop(f"请先启动工作台（{START}），再运行本脚本")
    has_verge = "Clash Verge Rev" in info["clients"] or bool(info.get("verge_root"))
    others = [c for c in info["clients"] if c != "Clash Verge Rev"]
    if others:
        say(f"\n发现其他代理软件：{'、'.join(others)}。本脚本只会自动改 Clash Verge Rev 和「Windows 设置脚本」；"
            "这些软件的规则要加在它们自己的设置里，请用开始菜单「其他代理软件配置说明」（安装目录 docs\\本地agent提示词.md）交给本地 agent，或用页面脚本。")
    options = {}
    if has_verge:
        options["A"] = "Clash Verge Rev：在全局扩展脚本里追加预约域名规则（推荐，你已安装）"
    options["B"] = "Windows 设置脚本：不需要 Clash" + ("" if has_verge else "（推荐）")
    options["C"] = "不改代理，改用页面脚本方式（工作台 设置 → 首次接入）"
    options["Q"] = "退出"
    say("\n请选择：")
    route = choose("输入字母：", options)
    if route in ("C", "Q"):
        say("没有做任何修改。页面脚本方式：企业微信按 Ctrl+Shift+Alt+D 开启调试模式 → 预约页右键「开发者工具」→ Console 粘贴连接脚本。")
        return
    state = load_state()
    enable_capture(state)
    trust_certificate(state)
    if route == "A":
        route_clash_verge(info, state)
    else:
        route_windows_pac(info, state)
    offer_auto_recovery(state)
    if wait_for_capture() and ask("要做一次学校只读验证吗？（只查询一个场地，不下单）"):
        result = workbench("/api/connect/test", method="POST", timeout=30)
        say(f"  {'✓' if result.get('ok') else '✗'} {result.get('msg', '')}")
    say(f"\n完成。修改记录在 {state_dir() / 'state.json'}，撤销请运行 {UNDO}。")


def undo() -> None:
    state = load_state()
    if not state:
        say("没有找到本脚本做过的修改记录，无需撤销。")
        return
    say("将撤销「配置自动获取」做过的修改：")
    if state.get("verge_script"):
        say(f"  · 从 Clash Verge 全局扩展脚本中移除预约规则：{state['verge_script']}")
    if state.get("pac_set"):
        say("  · 关闭 Windows「使用设置脚本」（仅当它仍指向工作台）")
    if state.get("ca_thumbprint"):
        say(f"  · 从当前用户信任列表删除 mitmproxy 证书 {state['ca_thumbprint'][:8]}…")
    if state.get("recovery_enabled"):
        say("  · 关闭「登录失效时自动刷新预约页并重新获取」")
    if state.get("config_backup"):
        say(f"  · 把 config.toml 的 capture_mode 改回 {state.get('capture_mode_before') or '默认（删除该项）'}")
    if not ask("确定撤销吗？"):
        return
    if state.get("verge_script"):
        script = Path(state["verge_script"])
        if script.exists():
            rest, had = strip_capture_block(script.read_text(encoding="utf-8"))
            if had and not rest.strip() and state.get("verge_created"):
                script.unlink()
            elif had:
                script.write_text(rest, encoding="utf-8")
        say("  已移除 Clash 规则，请在 Clash Verge 里刷新一次")
    if state.get("recovery_enabled"):
        try:
            workbench("/api/connect/recovery", method="POST", body={"enabled": False})
            say("  已关闭自动恢复")
        except OSError:
            say("  工作台没在运行，自动恢复开关请稍后在「设置」里关闭")
    if state.get("pac_set"):
        try:
            if read_proxy_settings().get("AutoConfigURL") == PAC_URL:
                open_proxy_settings()
                input("  请在打开的设置页关掉「使用设置脚本」并保存，然后按回车…")
                if read_proxy_settings().get("AutoConfigURL") == PAC_URL:
                    delete_auto_config_url()
            say("  已关闭 Windows 设置脚本")
        except (ImportError, OSError) as exc:
            say(f"  Windows 代理设置未能修改（{exc}），请手动关闭「使用设置脚本」")
    if state.get("ca_thumbprint"):
        say("  删除证书时 Windows 会再弹一次确认，请点「是」")
        removed = certutil("-delstore", "Root", state["ca_thumbprint"]).returncode == 0
        say("  已删除证书" if removed else f"  证书没有删除，可稍后运行 certutil -user -delstore Root {state['ca_thumbprint']}")
    if state.get("config_backup"):
        config = DATA_DIR / "config.toml"
        text = config.read_text(encoding="utf-8")
        before = state.get("capture_mode_before")
        if before is None:
            doc = tomlkit.parse(text)
            doc.get("token", {}).pop("capture_mode", None)
            config.write_text(tomlkit.dumps(doc), encoding="utf-8")
        else:
            config.write_text(set_capture_mode(text, before)[0], encoding="utf-8")
        say("  已恢复 capture_mode")
    done = state_dir() / f"state.undone-{datetime.now():%Y%m%d-%H%M%S}.json"
    (state_dir() / "state.json").rename(done)
    say("撤销完成。请完全退出并重新打开企业微信。")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--undo", action="store_true", help="撤销本脚本做过的修改")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        say("本脚本只在 Windows 上运行。")
        return 2
    try:
        undo() if args.undo else setup()
    except SetupStop as stop:
        say(f"\n已停止：{stop}")
        return 1
    except KeyboardInterrupt:
        say(f"\n已中断。已经完成的修改记录在 state.json，可运行 {UNDO} 撤销。")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
