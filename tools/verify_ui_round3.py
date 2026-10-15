"""Round-3 measurable acceptance for the compact chat UI.

Drives the running Vite app (synthetic acceptance backend, no school account and no
paid model) at desktop and mobile viewports, records layout/typography facts, and
then performs one real end-to-end turn so answer text can be measured too.

    python tools/verify_ui_round3.py --base http://127.0.0.1:15173
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
  const font = (selector) => {
    const node = document.querySelector(selector);
    return node ? getComputedStyle(node).fontSize : null;
  };
  // Largest computed font-size of any element that directly renders visible text.
  const largestFont = () => {
    const scope = document.querySelector('.agent-page');
    if (!scope) return null;
    let size = 0;
    let where = '';
    for (const node of scope.querySelectorAll('*')) {
      const own = Array.from(node.childNodes).some(text => text.nodeType === 3 && text.textContent.trim());
      if (!own) continue;
      const rect = node.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) continue;
      const current = parseFloat(getComputedStyle(node).fontSize);
      if (current > size) { size = current; where = node.tagName.toLowerCase() + '.' + (node.className || ''); }
    }
    return { size: size, where: where };
  };
  return {
    inner_width: window.innerWidth,
    inner_height: window.innerHeight,
    scroll_width: Math.max(root.scrollWidth, document.body ? document.body.scrollWidth : 0),
    overflow: Math.max(root.scrollWidth, document.body ? document.body.scrollWidth : 0) - window.innerWidth,
    composer: box('textarea[aria-label="向校园助手提问"]'),
    composer_box: box('.agent-layout-composer'),
    send: box('button[aria-label="发送"]'),
    header: box('.app-header'),
    toolbar: box('.agent-toolbar'),
    toolbar_height: box('.agent-toolbar') ? box('.agent-toolbar').height : null,
    context_row: box('.agent-context-row'),
    course_select: box('.agent-context-row > .ant-select'),
    provider_label: box('.agent-provider-label'),
    provider_select: box('[aria-label="选择对话模型"]'),
    chat_title: box('.agent-heading-title'),
    welcome_heading: box('.agent-welcome h2.ant-typography'),
    sidebar: box('aside[aria-label="助手侧边栏"]'),
    mobile_sidebar_toggle: box('button[aria-label="打开导航"]'),
    browser_workspace: box('button[aria-label="浏览器工作区"]'),
    calendar: box('button[aria-label="日历"]'),
    chat_title_font: font('.agent-heading-title'),
    welcome_heading_font: font('.agent-welcome h2.ant-typography'),
    answer_font: font('.agent-answer'),
    alert_title_font: font('.ant-alert-title'),
    alert_font: font('.ant-alert-message'),
    alert_description_font: font('.ant-alert-description'),
    provider_label_font: font('.agent-provider-label'),
    composer_font: font('textarea[aria-label="向校园助手提问"]'),
    largest_font: largestFont(),
    start_gate_present: Boolean(document.querySelector('.agent-start')),
    start_button_present: Array.from(document.querySelectorAll('button')).some(
      node => (node.textContent || '').trim() === '开始对话'),
  };
}"""


def collect(page, out: Path, name: str) -> dict:
    """One measurement pass plus a screenshot for the current page state."""
    facts = page.evaluate(MEASURE)
    page.screenshot(path=str(out / f"round3-chat-{name}.png"), full_page=False)
    return facts


def run(base: str, out: Path) -> dict:
    import os
    import tempfile

    # The inherited Windows temp can deny mkdtemp (EPERM) here; Playwright's artifact
    # directory follows TEMP/TMP, so point them at a writable project scratch tree.
    scratch = ROOT / ".test-tmp" / "round3-ui-scratch"
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
            page.wait_for_timeout(600)

            new_chat = collect(page, out, f"new-{name}")
            facts = {
                "name": name, "width": width, "height": height,
                "overflow_px": new_chat["overflow"],
                "no_horizontal_scroll": new_chat["overflow"] <= 1,
                "composer_visible": bool(new_chat["composer"] and new_chat["composer"]["visible"]),
                "composer_inside_viewport": bool(new_chat["composer"]
                    and new_chat["composer"]["right"] <= width + 1 and new_chat["composer"]["bottom"] <= height + 1
                    and new_chat["composer"]["top"] >= 0),
                "composer_box_inside_viewport": bool(new_chat["composer_box"]
                    and new_chat["composer_box"]["bottom"] <= height + 1 and new_chat["composer_box"]["top"] >= 0),
                "composer_rect": new_chat["composer"],
                "send_visible": bool(new_chat["send"] and new_chat["send"]["visible"]),
                "header_height": new_chat["header"]["height"] if new_chat["header"] else None,
                "toolbar_height": new_chat["toolbar_height"],
                "context_row_height": new_chat["context_row"]["height"] if new_chat["context_row"] else None,
                "course_select_width": new_chat["course_select"]["width"] if new_chat["course_select"] else None,
                "providers_ready": bool(new_chat["provider_label"] or new_chat["provider_select"]),
                "model_select_visible": bool(new_chat["provider_select"] and new_chat["provider_select"]["visible"]),
                "provider_label_visible": bool(new_chat["provider_label"] and new_chat["provider_label"]["visible"]),
                "start_gate_present": new_chat["start_gate_present"],
                "start_button_present": new_chat["start_button_present"],
                "modal_dialogs": page.locator('[role="dialog"]').count(),
                "checkboxes_on_new_chat": page.locator('input[type="checkbox"]').count(),
                "chat_title_font": new_chat["chat_title_font"],
                "welcome_heading_font": new_chat["welcome_heading_font"],
                "alert_title_font": new_chat["alert_title_font"],
                "alert_font": new_chat["alert_font"],
                "alert_description_font": new_chat["alert_description_font"],
                "provider_label_font": new_chat["provider_label_font"],
                "composer_font": new_chat["composer_font"],
                "largest_font_on_new_chat": new_chat["largest_font"],
                "page_errors": errors,
            }
            if width < 600:
                # The mobile sidebar must still open on demand and close again.
                toggle = page.locator('button[aria-label="打开导航"]')
                facts["mobile_toggle_present"] = toggle.count() == 1
                if toggle.count() == 1:
                    toggle.click()
                    page.wait_for_timeout(500)
                    facts["drawer_opened"] = page.locator('aside[aria-label="助手侧边栏"]').first.is_visible()
                    page.screenshot(path=str(out / "round3-mobile-drawer.png"))
                    close = page.locator('.agent-mobile-drawer button[aria-label="Close"]').first
                    if close.count() == 1:
                        close.click()
                    else:
                        page.keyboard.press("Escape")
                    try:
                        page.wait_for_selector('.agent-mobile-drawer', state="hidden", timeout=5000)
                    except Exception as exc:
                        facts["drawer_close_error"] = f"{type(exc).__name__}: {exc}"
                    page.wait_for_timeout(300)
                    facts["drawer_closed"] = not page.locator('.agent-mobile-drawer').first.is_visible()

            # One real end-to-end turn against the synthetic acceptance backend.
            prompt = f"验收提问（{name}）：这周有哪些作业需要留意？"
            page.fill('textarea[aria-label="向校园助手提问"]', prompt)
            page.wait_for_timeout(150)
            facts["send_enabled_before_click"] = page.locator('button[aria-label="发送"]').is_enabled()
            page.click('button[aria-label="发送"]')
            answer = ""
            try:
                page.wait_for_function(
                    "() => { const node = document.querySelector('.agent-answer');"
                    " return Boolean(node && node.textContent && node.textContent.trim().length > 0); }",
                    timeout=90000)
                page.wait_for_timeout(1200)
                answer = (page.locator('.agent-answer').first.inner_text() or "").strip()
            except Exception as exc:  # Recorded honestly instead of asserted silently.
                facts["answer_wait_error"] = f"{type(exc).__name__}: {exc}"
            facts["answer_non_empty"] = len(answer) > 0
            facts["answer_preview"] = answer[:160]
            facts["url_after_send"] = page.url
            after = collect(page, out, f"answer-{name}")
            facts["answer_font_after_turn"] = after["answer_font"]
            facts["largest_font_after_turn"] = after["largest_font"]
            facts["overflow_px_after_turn"] = after["overflow"]
            facts["modal_dialogs_after_turn"] = page.locator('[role="dialog"]').count()
            report["viewports"].append(facts)
            context.close()

        browser.close()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:15173")
    parser.add_argument("--out", default=str(ROOT / "outputs" / "round3-ui"))
    args = parser.parse_args()
    target = Path(args.out)
    result = run(args.base, target)
    (target / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
