"""清掉残留的引导浏览器进程。

背景：程序会为每次运行启动一个 Edge 实例（独立的临时配置目录）。如果某次异常退出
（强杀、崩溃），Edge 子进程会残留；它们会占着资源，也可能让下一次启动相互干扰。

本脚本**只结束命令行里明确带有本项目 profile 目录名的 msedge 进程**，
不会碰用户日常使用的 Edge。找不到就什么都不做。
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
# 本项目用到的配置目录名前缀
PROFILE_PATTERNS = ("guide-profile", "guide-read-profile", "run-profile", "probe-profile")

# 合理的进程号范围（Windows PID 远小于这个值）；用来排除解析出的垃圾数字
_MAX_PID = 4_000_000


def _list_edge_processes() -> list[tuple[int, str]]:
    """返回 [(pid, commandline), ...]。优先 PowerShell（wmic 在新系统已移除）。"""
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
        "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"
    )
    out = ""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        ).stdout or ""
    except Exception:  # noqa: BLE001
        out = ""

    if not out:
        try:
            out = subprocess.run(
                ["wmic", "process", "where", "name='msedge.exe'",
                 "get", "ProcessId,CommandLine", "/format:csv"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=30,
            ).stdout or ""
        except Exception:  # noqa: BLE001
            return []

    items: list[tuple[int, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        head = line.split("\t", 1)[0].strip()
        if head.isdigit():
            pid = int(head)
            if 0 < pid < _MAX_PID:
                items.append((pid, line))
            continue
        # CSV 形式：逗号分隔，最后一个数字字段是 PID
        parts = [p.strip().strip('"') for p in line.split(",")]
        cmd = ",".join(parts[:-1]) if len(parts) > 1 else line
        for part in reversed(parts):
            if part.isdigit():
                pid = int(part)
                if 0 < pid < _MAX_PID:
                    items.append((pid, cmd))
                break
    return items


def find_stale_pids() -> list[int]:
    pids: list[int] = []
    for pid, cmd in _list_edge_processes():
        if any(name in cmd for name in PROFILE_PATTERNS):
            pids.append(pid)
    return sorted(set(pids))


def main() -> int:
    pids = find_stale_pids()
    if not pids:
        print("no stale guide-profile browser processes")
        return 0
    print("killing %d stale browser process(es): %s" % (len(pids), pids))
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=20)
        except Exception:  # noqa: BLE001
            pass
    time.sleep(2)
    remaining = find_stale_pids()
    print("remaining: %s" % remaining)
    return 0


if __name__ == "__main__":
    sys.exit(main())
