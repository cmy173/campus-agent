"""Measurable desktop/mobile acceptance for the Campus workspace UI.

Drives the real built app (Vite + real FastAPI + real Harness) at two viewports and
records layout facts plus screenshots. It never contacts a school account or a paid
model; the run is labelled as synthetic inside the app itself.

    python tools/verify_ui_round2.py --base http://127.0.0.1:15173
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MEASURE = """() => {
  const root = document.documentElement;
  const box = (selector) => {
    const node = document.querySelector(selector);
    if (!node) return null;
    const rect = node.getBoundingClientRect();
    const style = getComputedStyle(node);
    return { left: Math.round(rect.left), right: Math.round(rect.right), top: Math.round(rect.top),
             bottom: Math.round(rect.bottom), width: Math.round(rect.width), height: Math.round(rect.height),
             display: style.display, visibility: style.visibility,
             visible: rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none' };
  };
  return {
    inner_width: window.innerWidth,
    inner_height: window.innerHeight,
    scroll_width: Math.max(root.scrollWidth, document.body ? document.body.scrollWidth : 0),
    overflow: Math.max(root.scrollWidth, document.body ? document.body.scrollWidth : 0) - window.innerWidth,
    composer: box('textarea[aria-label="向校园助手提问"]'),
    send: box('button[aria-label="发送"]'),
    sidebar: box('aside[aria-label="助手侧边栏"]'),
    mobile_sidebar_toggle: box('button[aria-label="打开导航"]'),
    browser_workspace: box('button[aria-label="浏览器工作区"]'),
    calendar: box('button[aria-label="日历"]'),
    chat_title: box('.agent-heading-title'),
    start_gate: box('.agent-start'),
    model_status: box('.agent-provider-label, [aria-label="选择对话模型"]'),
  };
}"""


def run(base: str, out: Path) -> dict:
    import os
    import tempfile

    # The inherited Windows temp can deny mkdtemp (EPERM) here; Playwright's artifact
    # directory follows TEMP/TMP, so point them at a writable project scratch tree.
    scratch = ROOT / ".test-tmp" / "round2-ui-scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = os.environ["TMP"] = os.environ["TMPDIR"] = str(scratch)
    tempfile.tempdir = str(scratch)

    from playwright.sync_api import sync_playwright

    out.mkdir(parents=True, exist_ok=True)
    report: dict = {"base": base, "viewports": []}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        for width, height, name in ((1440, 960, "desktop"), (390, 844, "mobile")):
            context = browser.new_context(viewport={"width": width, "height": height},
                                          device_scale_factor=1, is_mobile=width < 600, has_touch=width < 600)
            page = context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(base + "/chat", wait_until="networkidle")
            page.wait_for_selector('textarea[aria-label="向校园助手提问"]', timeout=20000)
            chat = page.evaluate(MEASURE)
            page.screenshot(path=str(out / f"round2-chat-{name}.png"))
            facts = {
                "name": name, "width": width, "height": height,
                "overflow_px": chat["overflow"],
                "no_horizontal_scroll": chat["overflow"] <= 1,
                "composer_visible": bool(chat["composer"] and chat["composer"]["visible"]),
                "composer_inside_viewport": bool(chat["composer"]
                    and chat["composer"]["right"] <= width + 1 and chat["composer"]["bottom"] <= height + 1
                    and chat["composer"]["top"] >= 0),
                "send_visible": bool(chat["send"] and chat["send"]["visible"]),
                "chat_title_visible": bool(chat["chat_title"] and chat["chat_title"]["visible"]),
                "start_gate_present": chat["start_gate"] is not None,
                "model_status_visible": bool(chat["model_status"] and chat["model_status"]["visible"]),
                "modal_dialogs": page.locator('[role="dialog"]').count(),
                "checkboxes_on_new_chat": page.locator('input[type="checkbox"]').count(),
                "page_errors": errors,
            }
            if width < 600:
                # The mobile sidebar must open on demand and close with Escape.
                toggle = page.locator('button[aria-label="打开导航"]')
                facts["mobile_toggle_present"] = toggle.count() == 1
                if toggle.count() == 1:
                    toggle.click()
                    page.wait_for_timeout(400)
                    facts["drawer_opened"] = page.locator('aside[aria-label="助手侧边栏"]').first.is_visible()
                    page.screenshot(path=str(out / "round2-mobile-drawer.png"))
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(300)
            report["viewports"].append(facts)
            context.close()

        # Settings → data management: one central irreversible confirmation only.
        context = browser.new_context(viewport={"width": 1440, "height": 960})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(base + "/chat?workspace=settings&settings_tab=accounts", wait_until="networkidle")
        page.wait_for_timeout(1200)
        report["settings_page_errors"] = errors
        context.close()
        browser.close()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:15173")
    parser.add_argument("--out", default=str(ROOT / "outputs" / "round2-ui"))
    args = parser.parse_args()
    target = Path(args.out)
    result = run(args.base, target)
    (target / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
