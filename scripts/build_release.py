"""Build the portable Windows package: SustechReservation-<version>-win-x64.zip.

Runs on Linux or Windows; every input is pinned:
  1. CPython from the official NuGet package (python.org is not reachable from every build
     host); version and sha256 in release/runtime.json.
  2. Dependencies from release/requirements-win.lock (exact versions + sha256), installed for
     win_amd64 / CPython 3.13 with uv into runtime/Lib/site-packages.
  3. App files from an explicit whitelist, read from a git commit (not the working tree).
  4. PACKAGED marker, VERSION, entry .cmd files, quick start, notices.
  5. Secret and forbidden-file scan; any hit fails the build before the zip is written.
  6. MANIFEST.txt, THIRD_PARTY_LICENSES.txt, the zip and SHA256SUMS.txt.
  7. With --installer: SustechReservation-Setup-<version>.exe from release/installer.nsi (NSIS
     makensis, Linux or Windows): per-user install, desktop / Start menu shortcuts, uninstaller.

A zip built here is not proof it runs on Windows; .github/workflows/windows-package.yml does
that check on a Windows runner.
"""
from __future__ import annotations

import argparse
import email.parser
import hashlib
import json
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "release"
PACKAGE_NAME = "SustechReservation"

APP_FILES = [
    "app.py", "engine.py", "utils.py", "connection.py", "watcher.py", "mitm_addon.py",
    "serve.py", "launcher.py", "app_paths.py",
    "index.html", "bridge.html", "connect.js", "connect-inject.js", "watch.js", "sport_scope.js",
    "matrix_target.js", "control_ops.js", "clash-extension.js", "onboarding.js",
    "fast_db.npz", "config.example.toml", "scripts/capture_setup.py",
]
APP_PACKAGES = ["core", "models", "network"]  # *.py only
DOC_FILES = {"docs/QUICKSTART.md": "快速开始.md", "docs/CAPTURE_SETUP.md": "docs/自动获取配置.md",
             "docs/LOCAL_AGENT_CAPTURE_PROMPT.md": "docs/本地agent提示词.md"}

# Pruned from the CPython runtime: build headers, pip, IDLE/Tk, the stdlib test suite.
RUNTIME_PRUNE = ("include/", "libs/", "Lib/site-packages/", "Lib/ensurepip/", "Lib/idlelib/",
                 "Lib/tkinter/", "Lib/turtledemo/", "Lib/test/")

ENTRY_FILES = {
    "启动工作台.cmd": '@echo off\r\nstart "" "%~dp0runtime\\pythonw.exe" "%~dp0app\\launcher.py"\r\n',
    "停止工作台.cmd": '@echo off\r\nstart "" "%~dp0runtime\\pythonw.exe" "%~dp0app\\launcher.py" --stop\r\n',
    "配置自动获取.cmd": ('@echo off\r\nchcp 65001 >nul\r\n'
                       '"%~dp0runtime\\python.exe" "%~dp0app\\scripts\\capture_setup.py"\r\npause\r\n'),
    "撤销自动获取配置.cmd": ('@echo off\r\nchcp 65001 >nul\r\n'
                      '"%~dp0runtime\\python.exe" "%~dp0app\\scripts\\capture_setup.py" --undo\r\npause\r\n'),
}

def windowless_entry(stop: bool = False) -> str:
    body = '''Option Explicit
Dim shell, files, root, python, entry, quote
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
python = files.BuildPath(root, "runtime\\pythonw.exe")
entry = files.BuildPath(root, "app\\launcher.py")
If Not files.FileExists(python) Or Not files.FileExists(entry) Then
    MsgBox "Extract the complete package before starting the workbench.", 16, "Zhen CourtSnipe"
    WScript.Quit 1
End If
shell.CurrentDirectory = root
quote = Chr(34)
shell.Run quote & python & quote & " " & quote & entry & quote & "ARGS", 0, False
'''
    return body.replace('ARGS', ' --stop' if stop else '').replace('\n', '\r\n')


WINDOWLESS_ENTRY_FILES = {
    "start-workbench.vbs": windowless_entry(),
    "stop-workbench.vbs": windowless_entry(stop=True),
}

NOTICE = """南科大场地预约工作台 · 发行说明

· 本工具仅供合资格同学在自己的电脑、用自己的企业微信账号使用；不包含任何人的账号、Token 或个人资料。
· 源码基于 fynngwu/badminton_scripts 修改，上游未声明开源许可，请勿公开发布或二次分发。
· 第三方组件及其许可见 THIRD_PARTY_LICENSES.txt；Python 运行时来自 Python Software Foundation 官方发行。
· 个人数据（配置、订单记录、日志）保存在 %LOCALAPPDATA%\\SustechReservation\\data，不在本文件夹内。
"""

FORBIDDEN_NAME = re.compile(
    r"(^|/)(config\.toml|watch-orders\.json|watch-orders\.tmp|scripts\.py|HANDOFF[^/]*|CURRENT_STATE[^/]*"
    r"|\.local-capture|\.mitmproxy|\.git|\.claude|output|artifacts|tests?)(/|$)"
    r"|\.(har|log|bak[^/]*|pem|p12|pfx)$|mitmproxy-ca", re.I)
SECRET_PATTERNS = {
    "jwt": re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |ENCRYPTED )?PRIVATE KEY-----"),
    "phone": re.compile(r"(?<![\d.])1[3-9]\d{9}(?![\d.])"),
    "id card": re.compile(r"(?<!\d)\d{17}[\dXx](?![\dXx])"),
    "credential value": re.compile(
        r"(?i)\b(token|userid|user_id|customer_id|customer_name|customer_tel|tel|secret|password)\b"
        r"\s*[=:]\s*[\"']([^\"'\s{}$]{6,})[\"']"),
}
PLACEHOLDER = re.compile(r"^(YOUR_[A-Z_]+|fixture[-\w]*|demo[-\w]*|test[-\w]*|x+)$", re.I)


def log(text: str) -> None:
    print(text, flush=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True).stdout


def git_bytes(ref: str, path: str) -> bytes:
    return subprocess.run(["git", "-C", str(ROOT), "show", f"{ref}:{path}"], capture_output=True, check=True).stdout


# ── stages ──────────────────────────────────────────────────────────────────

def fetch_runtime(cache: Path, dest: Path) -> dict:
    spec = json.loads((RELEASE / "runtime.json").read_text(encoding="utf-8"))
    nupkg = cache / f"python.{spec['version']}.nupkg"
    if not nupkg.exists() or sha256(nupkg) != spec["sha256"]:
        cache.mkdir(parents=True, exist_ok=True)
        log(f"下载 CPython {spec['version']}（NuGet 官方包）…")
        with urllib.request.urlopen(spec["url"], timeout=120) as reply:
            nupkg.write_bytes(reply.read())
    actual = sha256(nupkg)
    if actual != spec["sha256"]:
        raise SystemExit(f"CPython 包校验失败：{actual} != {spec['sha256']}")
    with zipfile.ZipFile(nupkg) as archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if not name.startswith("tools/") or name.endswith("/"):
                continue
            rel = name[len("tools/"):]
            if rel.startswith(RUNTIME_PRUNE) or "/__pycache__/" in rel:
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(info))
    (dest / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)
    return spec


def install_dependencies(site_packages: Path) -> None:
    uv = shutil.which("uv") or str(Path(sys.executable).with_name("uv"))
    log("安装依赖（win_amd64 / CPython 3.13，按锁文件校验哈希）…")
    subprocess.run([uv, "pip", "install", "--quiet", "--no-deps", "--require-hashes", "--no-cache",
                    "--python-platform", "x86_64-pc-windows-msvc", "--python-version", "3.13",
                    "--target", str(site_packages), "-r", str(RELEASE / "requirements-win.lock")], check=True)
    # Console-script launchers point at the build machine's interpreter; the app runs modules instead.
    for leftover in ("bin", "Scripts"):
        shutil.rmtree(site_packages / leftover, ignore_errors=True)
    for cache_dir in site_packages.rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)


def copy_app(ref: str, dest: Path) -> list[str]:
    tracked = set(git("ls-tree", "-r", "--name-only", ref).splitlines())
    paths = list(APP_FILES)
    for package in APP_PACKAGES:
        paths += sorted(p for p in tracked if p.startswith(f"{package}/") and p.endswith(".py"))
    missing = [p for p in paths if p not in tracked]
    if missing:
        raise SystemExit(f"白名单文件不在 {ref} 中：{missing}")
    for path in paths:
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(git_bytes(ref, path))
    return paths


def render_markdown(text: str, title: str) -> str:
    try:
        import markdown

        body = markdown.markdown(text, extensions=["tables", "fenced_code"])
    except ImportError:
        import html

        body = f"<pre>{html.escape(text)}</pre>"
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{title}</title><style>body{{font:16px/1.7 'Microsoft YaHei',system-ui,sans-serif;"
            "max-width:820px;margin:32px auto;padding:0 16px;color:#1f2328}"
            "code,kbd{background:#f2f4f7;padding:1px 5px;border-radius:4px}"
            "table{border-collapse:collapse}td,th{border:1px solid #d8dde3;padding:6px 10px}"
            "h1,h2{line-height:1.3}</style></head><body>" + body + "</body></html>")


def write_docs(ref: str, pkg: Path) -> None:
    for source, target in DOC_FILES.items():
        text = git_bytes(ref, source).decode("utf-8")
        (pkg / target).parent.mkdir(parents=True, exist_ok=True)
        (pkg / target).write_text(text, encoding="utf-8")
    quick = (pkg / "快速开始.md").read_text(encoding="utf-8")
    (pkg / "使用说明.html").write_text(render_markdown(quick, "使用说明"), encoding="utf-8")
    (pkg / "NOTICE.txt").write_text(NOTICE.replace("\n", "\r\n"), encoding="utf-8-sig")


def write_licenses(pkg: Path) -> list[dict]:
    site = pkg / "runtime" / "Lib" / "site-packages"
    rows, parts = [], []
    python_license = (pkg / "runtime" / "LICENSE.txt").read_text(encoding="utf-8", errors="replace")
    parts.append("=" * 72 + "\nCPython (Python Software Foundation)\n" + "=" * 72 + "\n" + python_license)
    for dist in sorted(site.glob("*.dist-info"), key=lambda p: p.name.lower()):
        meta = email.parser.Parser().parsestr((dist / "METADATA").read_text(encoding="utf-8", errors="replace"))
        license_name = (meta.get("License-Expression") or "").strip() or next(
            (c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License ::")),
            (meta.get("License") or "").strip().splitlines()[0] if meta.get("License") else "未声明")
        rows.append({"name": meta["Name"], "version": meta["Version"], "license": license_name})
        texts = [p for p in dist.rglob("*") if p.is_file() and re.search(r"(LICEN[CS]E|COPYING|NOTICE)", p.name, re.I)]
        body = "\n\n".join(p.read_text(encoding="utf-8", errors="replace") for p in sorted(texts)) or "(包内未附带许可证全文)"
        parts.append("=" * 72 + f"\n{meta['Name']} {meta['Version']} — {license_name}\n" + "=" * 72 + "\n" + body)
    (pkg / "THIRD_PARTY_LICENSES.txt").write_text("\n\n".join(parts), encoding="utf-8")
    return rows


def scan(pkg: Path) -> list[str]:
    problems = []
    for path in sorted(pkg.rglob("*")):
        rel = path.relative_to(pkg).as_posix()
        if FORBIDDEN_NAME.search(rel) and not rel.startswith("runtime/"):
            problems.append(f"禁止打包的文件：{rel}")
        if not path.is_file() or rel.startswith("runtime/") or rel == "THIRD_PARTY_LICENSES.txt":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary assets (fast_db.npz)
        for kind, pattern in SECRET_PATTERNS.items():
            for match in pattern.finditer(text):
                value = match.group(match.lastindex or 0)
                if kind == "credential value" and PLACEHOLDER.match(value):
                    continue
                line = text.count("\n", 0, match.start()) + 1
                problems.append(f"疑似{kind}：{rel}:{line}")
    config = (pkg / "app" / "config.example.toml").read_text(encoding="utf-8")
    for required in ('dry_run = false', 'capture_mode = "page"', 'token = ""'):
        if required not in config:
            problems.append(f"配置模板缺少默认值：{required}")
    return problems


def write_manifest(pkg: Path, version: str) -> None:
    lines = [f"# {PACKAGE_NAME} {version}", "# path\tbytes\tsha256"]
    for path in sorted(p for p in pkg.rglob("*") if p.is_file()):
        rel = path.relative_to(pkg).as_posix()
        if rel != "MANIFEST.txt":
            lines.append(f"{rel}\t{path.stat().st_size}\t{sha256(path)}")
    (pkg / "MANIFEST.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_zip(pkg: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(pkg.rglob("*")):
            if path.is_file():
                archive.write(path, f"{PACKAGE_NAME}/{path.relative_to(pkg).as_posix()}")


def find_makensis() -> str:
    found = shutil.which("makensis")
    for candidate in (r"C:\Program Files (x86)\NSIS\makensis.exe", r"C:\Program Files\NSIS\makensis.exe"):
        if not found and Path(candidate).exists():
            found = candidate
    if not found:
        raise SystemExit("找不到 makensis：请安装 NSIS（Ubuntu: apt install nsis；Windows: choco install nsis）")
    return found


def make_installer(pkg: Path, out: Path, version: str) -> Path:
    setup = out / f"{PACKAGE_NAME}-Setup-{version}.exe"
    log("生成安装程序（NSIS）…")
    subprocess.run([find_makensis(), "-V2", "-INPUTCHARSET", "UTF8", f"-DVERSION={version}", f"-DSRC={pkg}",
                    f"-DOUTFILE={setup}", f"-DICON={RELEASE / 'icon.ico'}", str(RELEASE / "installer.nsi")],
                   check=True)
    return setup


def build(ref: str, out: Path, cache: Path, installer: bool = False) -> Path:
    commit = git("rev-parse", "--short", ref).strip()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    version = f"{stamp}-{commit}"
    pkg = out / PACKAGE_NAME
    if pkg.exists():
        shutil.rmtree(pkg)
    pkg.mkdir(parents=True)
    spec = fetch_runtime(cache, pkg / "runtime")
    install_dependencies(pkg / "runtime" / "Lib" / "site-packages")
    copied = copy_app(ref, pkg / "app")
    (pkg / "app" / "PACKAGED").write_text("release package: data lives under %LOCALAPPDATA%\n", encoding="utf-8")
    (pkg / "app" / "VERSION").write_text(version + "\n", encoding="utf-8")
    for name, body in ENTRY_FILES.items():
        (pkg / name).write_bytes(body.encode("ascii"))
    for name, body in WINDOWLESS_ENTRY_FILES.items():
        (pkg / name).write_bytes(body.encode("ascii"))
    write_docs(ref, pkg)
    licenses = write_licenses(pkg)
    problems = scan(pkg)
    if problems:
        raise SystemExit("发现不应打包的内容，已停止：\n  " + "\n  ".join(problems))
    (pkg / "BUILD-INFO.json").write_text(json.dumps({
        "version": version, "commit": git("rev-parse", ref).strip(), "python": spec["version"],
        "python_sha256": spec["sha256"], "app_files": len(copied), "dependencies": licenses,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    write_manifest(pkg, version)
    zip_path = out / f"{PACKAGE_NAME}-{version}-win-x64.zip"
    make_zip(pkg, zip_path)
    outputs = [zip_path] + ([make_installer(pkg, out, version)] if installer else [])
    (out / "SHA256SUMS.txt").write_text("".join(f"{sha256(f)}  {f.name}\n" for f in outputs), encoding="utf-8")
    size = sum(p.stat().st_size for p in pkg.rglob("*") if p.is_file())
    log(f"完成：{zip_path}（压缩后 {zip_path.stat().st_size / 1e6:.1f} MB，解压后 {size / 1e6:.1f} MB）")
    if installer:
        log(f"安装程序：{outputs[1]}（{outputs[1].stat().st_size / 1e6:.1f} MB）")
    return zip_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the portable Windows package")
    parser.add_argument("--ref", default="HEAD", help="git commit to package (default HEAD)")
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    parser.add_argument("--cache", type=Path, default=ROOT / "dist" / ".cache")
    parser.add_argument("--installer", action="store_true", help="also build the Setup.exe (needs NSIS makensis)")
    args = parser.parse_args(argv)
    build(args.ref, args.out, args.cache, installer=args.installer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
