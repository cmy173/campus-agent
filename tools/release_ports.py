"""Release this project's local ports and stop its leftover processes.

Why: a verification run (test bench, audit, or an ad-hoc `tools/dev.py` restart) can
outlive the terminal that started it. The workbench launcher then refuses to start —
`端口 8000 已被占用…启动器不会终止其他进程` — and the user sees no window. This tool
is the explicit "hand the machine back" step: it only ever touches processes whose
command line belongs to this repository, and it verifies the ports afterwards.

    python tools/release_ports.py                 # free the default project ports
    python tools/release_ports.py --all           # also stop leftover runners/MCP/browser
    python tools/release_ports.py --dry-run       # show what would be stopped
    python tools/release_ports.py --ports 5173,8000 --keep-launcher
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The workbench's own ports plus every port the verification tooling binds.
DEFAULT_PORTS = (5173, 8000, 8765, 8766, 15173, 18080, 19191)
# Long-lived project helpers that are safe to stop when no launcher owns them. Only
# ever applied to a command line that mentions this repository.
LEFTOVER_MARKERS = ("runner.mjs", "campus-plugin.mjs", "campus-mcp.mjs", "campus-browser-mcp.mjs",
                    "browser-host.mjs", "session_runner", "round2_testbench.py",
                    "verify_agent_runtime.py", "@deepseek-ai/dsh/lib/bin.js",
                    "app.main:app", "app.bootstrap",
                    "playwright\\driver", "playwright/driver")
PROJECT_MARKERS = ("campus-agent", "tools/dev.py", "tools\\dev.py", "round2_testbench.py",
                   "app.main:app", "connectors.browser.session_runner", "vite.js")
# A venv launcher on Windows re-executes the base interpreter, so the process that
# actually binds a project port can be `python.exe -m uvicorn app.main:app` with no repo
# path in its command line. Ownership therefore also follows the ancestor chain: any
# ancestor that mentions the repository or its venv means the whole tree is ours.


def owns(process: dict | None) -> bool:
    if not process:
        return False
    command = (process.get("CommandLine") or "").lower()
    if not command:
        return False
    return any(marker.lower() in command for marker in PROJECT_MARKERS)


def belongs_to_project(process: dict) -> bool:
    return owns(process)


def ancestor_owned(processes: dict[int, dict], pid: int) -> bool:
    """True when this pid or any ancestor is a project process (the binding child may not say so)."""
    seen: set[int] = set()
    current = pid
    while current and current not in seen:
        seen.add(current)
        process = processes.get(current)
        if process is None:
            return False
        if owns(process):
            return True
        parent = int(process.get("ParentProcessId") or 0)
        if parent == current:
            break
        current = parent
    return False


def _powershell(command: str) -> str:
    completed = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;" + command],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        raise SystemExit("PowerShell query failed: " + (completed.stderr or "").strip()[:300])
    return completed.stdout.strip()


def snapshot() -> list[dict]:
    raw = _powershell("Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CommandLine |"
                      " ConvertTo-Json -Compress -Depth 3")
    if not raw:
        return []
    value = json.loads(raw)
    return value if isinstance(value, list) else [value]


def listeners(ports: tuple[int, ...]) -> dict[int, list[int]]:
    joined = ",".join(str(port) for port in ports)
    raw = _powershell(f"Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |"
                      f" Where-Object {{ $_.LocalPort -in @({joined}) }} |"
                      f" Select-Object LocalPort,OwningProcess | ConvertTo-Json -Compress -Depth 3")
    if not raw:
        return {}
    value = json.loads(raw)
    rows = value if isinstance(value, list) else [value]
    result: dict[int, list[int]] = {}
    for row in rows:
        result.setdefault(int(row["LocalPort"]), []).append(int(row["OwningProcess"]))
    return result


def belongs_to_project(process: dict) -> bool:
    command = (process.get("CommandLine") or "").lower()
    return any(marker in command for marker in PROJECT_MARKERS)


def descendants(processes: list[dict], roots: set[int]) -> set[int]:
    children: dict[int, list[int]] = {}
    for process in processes:
        children.setdefault(int(process["ParentProcessId"]), []).append(int(process["ProcessId"]))
    found: set[int] = set()
    pending = list(roots)
    while pending:
        pid = pending.pop()
        if pid in found:
            continue
        found.add(pid)
        pending.extend(children.get(pid, []))
    return found


def stop(pids: set[int], processes: dict[int, dict]) -> list[str]:
    stopped = []
    for pid in sorted(pids):
        command = (processes.get(pid, {}).get("CommandLine") or "")[:110]
        completed = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace")
        ok = completed.returncode == 0
        stopped.append(f"{'stopped' if ok else 'skipped'} pid={pid} {(processes.get(pid, {}).get('Name') or '?'):<12} {command}")
    return stopped


def kept_label(held: dict[int, list[int]], pid: int) -> str:
    ports = sorted(port for port, pids in held.items() if pid in pids)
    return "port=" + ",".join(str(port) for port in ports)


def main() -> int:
    parser = argparse.ArgumentParser(description="Free this project's local ports.")
    parser.add_argument("--ports", default=",".join(str(port) for port in DEFAULT_PORTS),
                        help="comma-separated ports (default: the project's known ports)")
    parser.add_argument("--all", action="store_true",
                        help="also stop leftover project runners/MCP/browser helpers that hold no port")
    parser.add_argument("--dry-run", action="store_true", help="only report what would be stopped")
    parser.add_argument("--keep-launcher", action="store_true",
                        help="do not stop a live tools/dev.py (leave the app running)")
    args = parser.parse_args()
    ports = tuple(int(value) for value in args.ports.split(",") if value.strip())

    processes = {int(process["ProcessId"]): process for process in snapshot()}
    held = listeners(ports)
    owners = {pid for pids in held.values() for pid in pids}

    targets: set[int] = set()
    for pid in owners:
        process = processes.get(pid)
        if process and (belongs_to_project(process) or ancestor_owned(processes, pid)):
            targets.add(pid)
            print(f"port holder: {kept_label(held, pid)} pid={pid} {process.get('Name')} "
                  f"{(process.get('CommandLine') or '')[:90]}")
        elif process:
            print(f"keeping foreign listener on a project port: pid={pid} {process.get('Name')} "
                  f"{(process.get('CommandLine') or '')[:80]}")
    if args.keep_launcher:
        for pid in list(targets):
            command = (processes.get(pid, {}).get("CommandLine") or "").lower()
            if "tools/dev.py" in command or "tools\\dev.py" in command:
                print(f"keep-launcher: leaving pid={pid} (tools/dev.py)")
                targets.discard(pid)
    if args.all:
        for pid, process in processes.items():
            command = (process.get("CommandLine") or "").lower()
            if owns(process) and any(marker.lower() in command for marker in LEFTOVER_MARKERS):
                targets.add(pid)

    # Reduce every target to its outermost project-owned ancestor: one `/T` then removes a
    # whole tree (venv parent + the base-interpreter child that actually binds the port).
    def top_ancestor(pid: int) -> int:
        current, best, seen = pid, pid, set()
        while current and current not in seen:
            seen.add(current)
            process = processes.get(current)
            if process is None:
                break
            if owns(process):
                best = current
            parent = int(process.get("ParentProcessId") or 0)
            if parent == current:
                break
            current = parent
        return best

    targets = {top_ancestor(pid) for pid in targets}

    if not targets:
        print("nothing to release: no project-owned process holds " + ", ".join(str(p) for p in ports))
    else:
        print(f"stopping {len(targets)} project process tree(s)" + (" [dry-run]" if args.dry_run else ""))
        for pid in sorted(targets):
            process = processes.get(pid, {})
            command = (process.get("CommandLine") or "").replace("\n", " ")[:120]
            print(f"  {'would stop' if args.dry_run else 'tree root'} pid={pid} "
                  f"{process.get('Name') or '?'} :: {command}")
        if not args.dry_run:
            for line in stop(targets, processes):
                print("  " + line)

    if args.dry_run:
        print("dry-run only; nothing was stopped")
        return 0

    # A killed tree releases its sockets asynchronously; confirm instead of assuming.
    for _ in range(20):
        time.sleep(0.25)
        if not (set(listeners(ports)) & set(ports)):
            break
    remaining = {port: pids for port, pids in listeners(ports).items() if port in ports}
    if remaining:
        print("still held: " + ", ".join(f"{port} (pid {','.join(str(pid) for pid in pids)})"
                                        for port, pids in sorted(remaining.items())))
        return 1
    print("free: " + ", ".join(str(port) for port in ports))
    print("现在可以用工作台启动器启动（或 python tools/dev.py）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
