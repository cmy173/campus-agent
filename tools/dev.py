"""个人使用启动器：python tools/dev.py --setup，然后 python tools/dev.py。

默认使用 var/personal.db；不清空、不 seed。Ctrl+C 关闭本次启动的进程。
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "apps" / "api"
WEB = ROOT / "apps" / "web"
PYTHON = API / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def free_port(port):
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            raise SystemExit(f"端口 {port} 已被占用。请关闭旧服务或调整启动参数；启动器不会终止其他进程。")


def choose_port(preferred, reserved):
    """自动模式避开已有服务；不复用未知服务，不终止占用者。"""
    with socket.socket() as sock:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            if preferred in reserved:
                raise OSError("reserved")
            sock.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
    if port in reserved:
        return choose_port(preferred, reserved)
    print(f"端口 {preferred} 不可用，本次改用 {port}。", flush=True)
    return port


def project_web_processes():
    """初始化前检查本项目的 Node 进程，避免 npm ci 删除正被加载的原生模块。"""
    if os.name != "nt":
        return []
    command = 'Get-CimInstance Win32_Process -Filter "Name=\'node.exe\'" | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress'
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise SystemExit("无法检查运行中的网页服务，请关闭本项目的开发终端后再初始化。")
    rows = json.loads(result.stdout) if result.stdout.strip() else []
    if isinstance(rows, dict):
        rows = [rows]
    prefix = str(WEB).casefold().replace("/", "\\")
    return [row["ProcessId"] for row in rows
            if prefix in (row.get("CommandLine") or "").casefold().replace("/", "\\")]


def main():
    os.environ.setdefault("PYTHONUTF8", "1")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", action="store_true", help="初始化依赖（需要网络）")
    parser.add_argument("--check", action="store_true", help="只检查环境，不修改数据库")
    parser.add_argument("--platforms", nargs="*", choices=("xzzd", "zykt"), default=["xzzd"], help="打开受控登录窗口；传空列表可只启动网页")
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--web-port", type=int, default=5173)
    parser.add_argument("--smoke-test", action="store_true", help="启动并检查网页代理后自动关闭本次服务；配合 --platforms 不打开浏览器")
    parser.add_argument("--open-browser", action="store_true", help="服务就绪后打开网页")
    parser.add_argument("--stop-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--hidden", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--auto-ports", action="store_true", help="端口被占用时自动选择空闲端口")
    parser.add_argument("--state-file", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    child_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" and args.hidden else 0

    def spawn(command, **kwargs):
        return subprocess.Popen(command, creationflags=child_flags,
                                stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr, **kwargs)

    def run(command, **kwargs):
        subprocess.run(command, creationflags=child_flags, stdin=subprocess.DEVNULL,
                       stdout=sys.stdout, stderr=sys.stderr, check=True, **kwargs)

    def stopping():
        return args.stop_file is not None and args.stop_file.exists()
    if args.setup:
        blockers = project_web_processes()
        if blockers:
            raise SystemExit(f"本项目网页服务仍在运行（进程 {', '.join(map(str, blockers))}），会锁住依赖文件。请先停止该网页服务再初始化；尚未执行安装。")
        if not PYTHON.exists():
            run([sys.executable, "-m", "venv", str(API / ".venv")])
        print("正在安装 Python 依赖…", flush=True)
        build_locked = API / "build-requirements-lock.txt"
        if build_locked.exists():
            run([str(PYTHON), "-m", "pip", "install", "--disable-pip-version-check", "-r", build_locked.name], cwd=API)
        locked = API / "requirements-lock.txt"
        python_requirements = locked.name if locked.exists() else "requirements.txt"
        wheels = API / "requirements-wheels-win-py312.txt"
        wheel_host = os.name == "nt" and sys.version_info[:2] == (3, 12) and __import__("platform").machine().lower() in {"amd64", "x86_64"}
        if wheel_host and wheels.exists():
            run([str(PYTHON), "-m", "pip", "install", "--disable-pip-version-check",
                 "--no-deps", "--require-hashes", "-r", wheels.name], cwd=API)
            run([str(PYTHON), "-m", "pip", "install", "--disable-pip-version-check",
                 "--no-deps", "--no-build-isolation", "-e", "../../packages/learning[parse]"], cwd=API)
        else:
            run([str(PYTHON), "-m", "pip", "install", "--disable-pip-version-check",
                 *(["--no-build-isolation"] if build_locked.exists() else []),
                 "-r", python_requirements, "playwright"], cwd=API)
        run([str(PYTHON), "-m", "pip", "check"], cwd=API)
        npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
        if not npm:
            raise SystemExit("请先安装 Node.js（含 npm）")
        print("正在安装网页依赖；请等待完成…", flush=True)
        run([npm, "ci"], cwd=WEB)
        print("正在安装锁定的 Agent Harness 运行时…", flush=True)
        run([npm, "ci"], cwd=ROOT / "apps" / "agent-runtime")
        print("依赖初始化完成；默认使用已安装的 Edge，无需下载额外浏览器。")
        return
    if not PYTHON.exists():
        raise SystemExit("缺少本地 Python 环境，请先运行 python tools/dev.py --setup")
    node = shutil.which("node")
    vite = WEB / "node_modules" / "vite" / "bin" / "vite.js"
    if not node or not vite.exists():
        raise SystemExit("缺少 Node.js 或网页依赖，请先运行 python tools/dev.py --setup")
    modules = ["fastapi", "sqlalchemy", "alembic", "learning", "pypdf", "requests", "tzdata"]
    if args.platforms:
        modules.append("playwright")
    probe = "import importlib.util,sys; names=" + repr(modules) + "; missing=[n for n in names if importlib.util.find_spec(n) is None]; print('missing:', ', '.join(missing) or 'none'); sys.exit(bool(missing))"
    result = subprocess.run([str(PYTHON), "-c", probe], cwd=API, creationflags=child_flags,
                            stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr)
    if result.returncode:
        raise SystemExit("请运行 python tools/dev.py --setup 补齐依赖")
    if args.check:
        print("环境检查通过。个人模式不导入样例；真实同步需要在受控窗口登录。")
        return
    platforms = list(dict.fromkeys(args.platforms))
    requested = [("api", args.api_port), ("web", args.web_port)] + [(p, 8765 if p == "xzzd" else 8766) for p in platforms]
    ports = [port for _, port in requested]
    if len(set(ports)) != len(ports):
        raise SystemExit("服务端口不能重复")
    selected = {}
    for name, port in requested:
        if args.auto_ports:
            selected[name] = choose_port(port, set(selected.values()))
        else:
            free_port(port)
            selected[name] = port
    args.api_port, args.web_port = selected["api"], selected["web"]
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    (ROOT / "var").mkdir(exist_ok=True)
    env.setdefault("STUDY_DATABASE_URL", "sqlite:///" + (ROOT / "var" / "personal.db").as_posix())
    env.setdefault("STUDY_FILES_DIR", str(ROOT / "var" / "personal-files"))
    run([str(PYTHON), "-m", "alembic", "upgrade", "head"], cwd=API, env=env)
    run([str(PYTHON), "-m", "app.bootstrap"], cwd=API, env=env)
    env["STUDY_WORKER_INPROC"] = "1"
    env["VITE_API_MODE"] = "real"
    env["VITE_PROXY_TARGET"] = f"http://127.0.0.1:{args.api_port}"
    env["CAMPUS_BRIDGE_URL"] = f"http://127.0.0.1:{args.api_port}"
    env["CAMPUS_WEB_ORIGINS"] = f"http://127.0.0.1:{args.web_port},http://localhost:{args.web_port}"
    # 请求走同源 Vite 代理，避免遗留环境变量导致跨源失败。
    env["VITE_API_BASE_URL"] = ""
    for platform in platforms:
        env[f"{platform.upper()}_BRIDGE_URL"] = f"http://127.0.0.1:{selected[platform]}"
    processes = []
    connector_processes = {}
    try:
        processes.append(spawn([str(PYTHON), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(args.api_port)], cwd=API, env=env))
        for _ in range(100):
            if stopping():
                return
            if processes[0].poll() is not None:
                raise SystemExit("后端启动失败，请查看上方错误")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{args.api_port}/api/v1/health", timeout=1):
                    break
            except OSError:
                time.sleep(.2)
        else:
            raise SystemExit("后端启动超时")
        for platform in platforms:
            port = str(selected[platform])
            if stopping():
                return
            processes.append(spawn([str(PYTHON), "-m", "connectors.browser.session_runner", "serve", "--platform", platform,
                                               "--port", port, "--b-api", f"http://127.0.0.1:{args.api_port}"], cwd=ROOT, env=env))
            connector_processes[platform] = processes[-1]
        for platform, process in connector_processes.items():
            print(f"正在等待 {platform} 连接器启动（尚不要求登录）…", flush=True)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if stopping():
                    return
                if process.poll() is not None:
                    raise SystemExit(f"{platform} 连接器启动失败，请查看上方错误；未报告整体就绪。")
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{selected[platform]}/health", timeout=1) as response:
                        health = json.load(response)
                    if health.get("ok") and health.get("result", {}).get("platform") == platform:
                        print(f"{platform} 连接器启动检查通过。", flush=True)
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(.2)
            else:
                raise SystemExit(f"{platform} 连接器启动超时，请查看浏览器与连接器日志。")
        # 刷新数据库里可能残留的“服务不可达”，而不是让用户手动启动后再猜状态。
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for platform in platforms:
            if stopping():
                return
            print(f"正在检查 {platform} 平台连接…", flush=True)
            request = urllib.request.Request(f"http://127.0.0.1:{args.api_port}/api/v1/connections/conn_{platform}/check",
                                             data=b"{}", method="POST", headers={"Content-Type": "application/json"})
            try:
                with direct.open(request, timeout=35) as response:
                    connection = json.load(response)
                print(f"{platform} 连接状态：{connection.get('status')}；若需登录，请在受控窗口登录后检查连接。", flush=True)
            except (OSError, ValueError):
                print(f"{platform} 自动连接检查暂未完成；服务已启动，可在网页重试检查。", flush=True)
        processes.append(spawn([node, str(vite), "--port", str(args.web_port), "--strictPort"], cwd=WEB, env=env))
        print(f"网页：http://127.0.0.1:{args.web_port}/；登录后：课程与资源 → 导入课程 → 同步中心 → 立即同步。", flush=True)
        if args.smoke_test or args.open_browser:
            for _ in range(100):
                if stopping():
                    return
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{args.web_port}/api/v1/health", timeout=1) as response:
                        if response.status == 200:
                            print("启动与网页代理检查通过。", flush=True)
                            break
                except OSError:
                    time.sleep(.2)
            else:
                raise SystemExit("网页代理检查超时")
            if args.smoke_test:
                return
            if args.state_file:
                args.state_file.write_text(json.dumps({"web_url": f"http://127.0.0.1:{args.web_port}/",
                                                      "ports": selected}), encoding="utf-8")
            webbrowser.open(f"http://127.0.0.1:{args.web_port}/")
        while all(p.poll() is None for p in processes):
            if stopping():
                return
            time.sleep(.5)
        raise SystemExit("某个服务退出，请查看上方错误；正在关闭本次启动的服务。")
    except KeyboardInterrupt:
        print("正在关闭本次启动的服务…")
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"依赖安装或启动步骤失败（退出码 {exc.returncode}），请查看上方完整日志。若提示 EPERM / EBUSY，先关闭本项目网页服务再初始化。") from None
