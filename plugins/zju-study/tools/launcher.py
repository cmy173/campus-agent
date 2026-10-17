"""项目目录旁的 Windows GUI 启动器；EXE 不包含项目和后端依赖。"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
import webbrowser

from windows_job import WindowsJob

ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]


class Runner:
    def __init__(self, root):
        self.root = root
        self.process = self.job = self.output = None
        self.stop_file = root / "var" / f"launcher-stop-{uuid.uuid4().hex}"
        self.log_file = root / "var" / "launcher.log"
        self.state_file = root / "var" / f"launcher-state-{uuid.uuid4().hex}.json"
        self.requested_stop = False

    def start(self, platforms, *, setup=False, smoke=False, api_port=8000, web_port=5173):
        if self.process and self.process.poll() is None:
            return
        self.release()
        script = self.root / "tools" / "dev.py"
        if not script.is_file():
            raise RuntimeError("请把 EXE 放在项目根目录（与 tools、apps 文件夹同级）。")
        python = self.root / "apps" / "api" / ".venv" / "Scripts" / "python.exe"
        if python.is_file():
            command = [str(python)]
        elif setup:
            system_python = shutil.which("python") or shutil.which("py")
            if not system_python:
                raise RuntimeError("首次初始化需要安装 Python 3；再点击“初始化依赖”。")
            command = [system_python]
        else:
            raise RuntimeError("还没有后端环境，请先点击“初始化依赖”。")
        command += [str(script)]
        if setup:
            command += ["--setup", "--hidden"]
        else:
            command += ["--platforms", *platforms, "--api-port", str(api_port), "--web-port", str(web_port),
                        "--hidden", "--auto-ports", "--stop-file", str(self.stop_file), "--state-file", str(self.state_file)]
            command += ["--smoke-test"] if smoke else ["--open-browser"]
        self.log_file.parent.mkdir(exist_ok=True)
        self.requested_stop = False
        self.output = self.log_file.open("ab", buffering=0)
        self.output.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} {'初始化' if setup else '启动'} ---\n".encode("utf-8"))
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        self.job = WindowsJob()
        try:
            self.process = subprocess.Popen(command, cwd=self.root, env=env, stdin=subprocess.DEVNULL,
                                            stdout=self.output, stderr=subprocess.STDOUT,
                                            creationflags=subprocess.CREATE_NO_WINDOW)
            self.job.attach(self.process)
        except Exception:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                self.process.wait(timeout=5)
            self.release()
            raise

    def stop(self):
        self.requested_stop = True
        if self.process and self.process.poll() is None:
            self.stop_file.touch()

    def release(self):
        if self.job:
            self.job.close()
            self.job = None
        if self.output:
            self.output.close()
            self.output = None
        self.stop_file.unlink(missing_ok=True)
        self.state_file.unlink(missing_ok=True)

    def web_url(self):
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))["web_url"]
        except (OSError, ValueError, KeyError):
            return None


def gui():
    import tkinter as tk
    from tkinter import ttk, messagebox
    from tkinter.scrolledtext import ScrolledText
    window = tk.Tk()
    window.title("浙大学习工作台")
    window.geometry("780x560")
    window.minsize(620, 400)
    runner = Runner(ROOT)
    closing = False
    stop_deadline = None
    log_offset = runner.log_file.stat().st_size if runner.log_file.exists() else 0
    status = tk.StringVar(value="准备启动…")
    xzzd = tk.BooleanVar(value=True)
    zykt = tk.BooleanVar(value=True)
    frame = ttk.Frame(window, padding=16)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="浙大学习工作台", font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
    ttk.Label(frame, text="启动后自动打开网页；请在受控浏览器窗口登录。关闭本窗口会停止本次服务。").pack(anchor="w", pady=(8, 12))
    options = ttk.Frame(frame)
    options.pack(anchor="w")
    ttk.Checkbutton(options, text="学在浙大", variable=xzzd).pack(side="left")
    ttk.Checkbutton(options, text="智云课堂", variable=zykt).pack(side="left", padx=12)
    ttk.Label(frame, textvariable=status).pack(anchor="w", pady=10)
    buttons = ttk.Frame(frame)
    buttons.pack(fill="x")

    def start(setup=False):
        nonlocal stop_deadline
        try:
            runner.start([p for p, enabled in (("xzzd", xzzd.get()), ("zykt", zykt.get())) if enabled], setup=setup)
            stop_deadline = None
            status.set("正在初始化依赖（首次需要网络）…" if setup else "服务启动中；网页就绪后自动打开。")
        except Exception as exc:
            status.set("启动失败")
            messagebox.showerror("启动失败", str(exc), parent=window)

    def stop():
        nonlocal stop_deadline
        runner.stop()
        stop_deadline = time.monotonic() + 30
        status.set("正在停止本次服务…")

    start_button = ttk.Button(buttons, text="启动", command=start)
    start_button.pack(side="left")
    def open_web():
        url = runner.web_url()
        if url and runner.process and runner.process.poll() is None:
            webbrowser.open(url)
        else:
            messagebox.showinfo("网页尚未就绪", "请先启动并等待服务就绪；若启动失败，请查看下方日志。", parent=window)

    ttk.Button(buttons, text="打开网页", command=open_web).pack(side="left", padx=8)
    stop_button = ttk.Button(buttons, text="停止", command=stop)
    stop_button.pack(side="left")
    setup_button = ttk.Button(buttons, text="初始化依赖", command=lambda: start(True))
    setup_button.pack(side="left", padx=8)
    ttk.Button(buttons, text="使用说明", command=lambda: os.startfile(ROOT / "docs" / "coord" / "11-EXE启动器使用说明.md")).pack(side="left")
    logs = ScrolledText(frame, wrap="word", height=18, font=("Microsoft YaHei UI", 9))
    logs.pack(fill="both", expand=True, pady=12)
    logs.configure(state="disabled")

    def close():
        nonlocal closing
        closing = True
        stop()

    def poll():
        nonlocal log_offset
        if runner.log_file.exists():
            with runner.log_file.open("rb") as source:
                source.seek(log_offset)
                data = source.read()
                log_offset = source.tell()
            if data:
                logs.configure(state="normal")
                logs.insert("end", data.decode("utf-8", errors="replace"))
                logs.see("end")
                logs.configure(state="disabled")
                if "启动与网页代理检查通过" in data.decode("utf-8", errors="replace"):
                    status.set("服务已就绪；在浏览器中使用工作台。")
        active = runner.process is not None and runner.process.poll() is None
        if active and stop_deadline and time.monotonic() >= stop_deadline:
            runner.release()  # Job Object 兜底，只停止本次进程树。
            runner.process.wait(timeout=5)
            active = False
        start_button.configure(state="disabled" if active else "normal")
        setup_button.configure(state="disabled" if active else "normal")
        stop_button.configure(state="normal" if active else "disabled")
        if not active and runner.process and runner.output:
            code = runner.process.returncode
            status.set("已停止。" if runner.requested_stop else ("已完成；点击启动进入工作台。" if code == 0 else "启动失败，请查看下方日志。"))
            runner.release()
        if closing and not active:
            window.destroy()
            return
        window.after(250, poll)

    window.protocol("WM_DELETE_WINDOW", close)
    window.after(100, poll)
    window.after(200, start)
    try:
        window.mainloop()
    finally:
        runner.release()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--api-port", type=int, default=18081)
    parser.add_argument("--web-port", type=int, default=15174)
    args = parser.parse_args()
    if args.smoke_test:
        runner = Runner(ROOT)
        try:
            runner.start([], smoke=True, api_port=args.api_port, web_port=args.web_port)
            return runner.process.wait(timeout=90)
        finally:
            runner.release()
    gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
