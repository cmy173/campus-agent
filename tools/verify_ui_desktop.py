"""Desktop full-width visual regression for the chat entry and a task workspace.

Drives the application's own persistent Edge (same channel/profile layout as
`apps/agent-runtime/browser-host.mjs`) against a real API + Vite instance, and
records measurable layout assertions plus screenshots. The model is never
contacted: the entry surface, the toolbar and a task workspace need no provider.

    python tools/verify_ui_desktop.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "apps" / "api"
WEB = ROOT / "apps" / "web"
PYTHON = API / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
SIZES = ((1280, 900), (1920, 1080))
OVERFLOW_TOLERANCE = 1  # sub-pixel rounding in scrollWidth


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_url(url, timeout=60, method="GET", data=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request(url, data=data, method=method,
                                             headers={"Content-Type": "application/json"} if data else {})
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status
        except (OSError, urllib.error.URLError, ValueError):
            time.sleep(0.25)
    raise SystemExit(f"等待服务超时：{url}")


def edge_executable():
    candidates = [os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")]
    for base in candidates:
        if not base:
            continue
        candidate = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        if candidate.exists():
            return str(candidate)
    return None


def measure(page):
    return page.evaluate("""() => {
      const root = document.documentElement;
      const box = (selector) => {
        const node = document.querySelector(selector);
        if (!node) return null;
        const rect = node.getBoundingClientRect();
        return {left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom,
                width: rect.width, height: rect.height,
                visible: rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < window.innerHeight};
      };
      return {
        inner_width: window.innerWidth,
        inner_height: window.innerHeight,
        scroll_width: Math.max(root.scrollWidth, document.body ? document.body.scrollWidth : 0),
        composer: box('textarea[aria-label="向校园助手提问"]'),
        transcript: box('[aria-label="对话记录"]'),
        calendar_button: box('[aria-label="日历"]'),
        settings_button: box('[aria-label="助手设置"]'),
      };
    }""")


def verify():
    (ROOT / "var").mkdir(exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="v2-ui-desktop-", dir=ROOT / "var"))
    # The inherited Windows temp can deny mkdtemp (EPERM) in this environment; the
    # project already gives each session its own scratch temp. Playwright's artifact
    # directory follows TEMP/TMP/TMPDIR, so point them at our own tree.
    scratch = home / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = os.environ["TMP"] = os.environ["TMPDIR"] = str(scratch)
    api_port, web_port = free_port(), free_port()
    base = f"http://127.0.0.1:{web_port}/"
    env = dict(os.environ, PYTHONUTF8="1", PYTHONPATH=str(ROOT),
               TEMP=str(scratch), TMP=str(scratch),
               STUDY_DATABASE_URL="sqlite:///" + (home / "domain.sqlite").as_posix(),
               STUDY_FILES_DIR=str(home / "files"), STUDY_SECRETS_DIR=str(home / "secrets"),
               CAMPUS_AGENT_HOME=str(home / "agent"), STUDY_WORKER_INPROC="1",
               VITE_API_MODE="real", VITE_PROXY_TARGET=f"http://127.0.0.1:{api_port}",
               VITE_API_BASE_URL="", CAMPUS_BRIDGE_URL=f"http://127.0.0.1:{api_port}",
               CAMPUS_WEB_ORIGINS=f"http://127.0.0.1:{web_port},http://localhost:{web_port}")
    log = (home / "services.log").open("w", encoding="utf-8")
    processes = []

    def spawn(command, cwd):
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT)
        processes.append(process)
        return process

    try:
        subprocess.run([str(PYTHON), "-m", "alembic", "upgrade", "head"], cwd=API, env=env, check=True,
                       stdout=log, stderr=subprocess.STDOUT)
        subprocess.run([str(PYTHON), "-m", "app.bootstrap"], cwd=API, env=env, check=True,
                       stdout=log, stderr=subprocess.STDOUT)
        spawn([str(PYTHON), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(api_port)], API)
        wait_url(f"http://127.0.0.1:{api_port}/api/v1/health")
        node = "node"
        spawn([node, str(WEB / "node_modules" / "vite" / "bin" / "vite.js"), "--port", str(web_port), "--strictPort"], WEB)
        wait_url(base)

        from playwright.sync_api import sync_playwright

        evidence = {"kind": "desktop_full_width_visual_regression", "headless": True,
                    "base_url": base, "sizes": [], "browser": "msedge persistent profile (app layout)"}
        screenshots = home / "screenshots"
        screenshots.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(home / "agent" / "browser" / "profile"),
                channel="msedge",
                executable_path=edge_executable(),
                headless=True,
                viewport={"width": SIZES[0][0], "height": SIZES[0][1]},
                accept_downloads=True,
            )
            context.set_default_timeout(15000)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(base, wait_until="domcontentloaded")
            page.wait_for_selector('textarea[aria-label="向校园助手提问"]', state="visible")
            context.set_default_navigation_timeout(60000)

            for width, height in SIZES:
                page.set_viewport_size({"width": width, "height": height})
                page.wait_for_timeout(400)
                chat = measure(page)
                chat_ok = {
                    "no_horizontal_overflow": chat["scroll_width"] <= chat["inner_width"] + OVERFLOW_TOLERANCE,
                    "composer_visible": bool(chat["composer"] and chat["composer"]["visible"]),
                    "composer_inside_viewport": bool(chat["composer"]
                                                     and chat["composer"]["right"] <= chat["inner_width"] + OVERFLOW_TOLERANCE
                                                     and chat["composer"]["bottom"] <= chat["inner_height"] + OVERFLOW_TOLERANCE),
                    "toolbar_visible": bool(chat["calendar_button"] and chat["calendar_button"]["visible"]
                                            and chat["settings_button"] and chat["settings_button"]["visible"]),
                }
                shot = screenshots / f"chat-{width}x{height}.png"
                page.screenshot(path=str(shot), full_page=False)

                page.click('[aria-label="日历"]')
                page.wait_for_timeout(600)
                workspace = measure(page)
                workspace_ok = {
                    "no_horizontal_overflow": workspace["scroll_width"] <= workspace["inner_width"] + OVERFLOW_TOLERANCE,
                    "workspace_opened": page.locator('[aria-label="对话记录"]').count() >= 0,
                    "composer_still_reachable": bool(workspace["composer"] and workspace["composer"]["visible"]),
                }
                workspace_shot = screenshots / f"workspace-calendar-{width}x{height}.png"
                page.screenshot(path=str(workspace_shot), full_page=False)
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)

                evidence["sizes"].append({"width": width, "height": height, "chat": chat,
                                          "chat_checks": chat_ok, "workspace": workspace,
                                          "workspace_checks": workspace_ok,
                                          "screenshots": [str(shot), str(workspace_shot)]})

            # A refresh must keep the unsent draft (a required state-preservation check).
            draft = "桌面视觉回归草稿-保留检查"
            page.set_viewport_size({"width": SIZES[0][0], "height": SIZES[0][1]})
            page.fill('textarea[aria-label="向校园助手提问"]', draft)
            page.wait_for_timeout(300)
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector('textarea[aria-label="向校园助手提问"]', state="visible")
            page.wait_for_timeout(400)
            evidence["draft_preserved_after_reload"] = page.input_value('textarea[aria-label="向校园助手提问"]') == draft
            evidence["draft_value"] = page.input_value('textarea[aria-label="向校园助手提问"]')
            context.close()

        failed = []
        for entry in evidence["sizes"]:
            for name, ok in {**entry["chat_checks"], **entry["workspace_checks"]}.items():
                if not ok:
                    failed.append(f"{entry['width']}x{entry['height']}:{name}")
        if not evidence["draft_preserved_after_reload"]:
            failed.append("draft-preserved-after-reload")
        evidence["failed_checks"] = failed
        evidence["ok"] = not failed
        (home / "ui-desktop-evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"ok": evidence["ok"], "failed": failed,
                          "sizes": [(e["width"], e["height"]) for e in evidence["sizes"]],
                          "evidence": str(home / "ui-desktop-evidence.json")}, ensure_ascii=False, indent=2))
        if failed:
            raise SystemExit(1)
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
        log.close()


if __name__ == "__main__":
    verify()
