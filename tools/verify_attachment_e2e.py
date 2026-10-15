"""Final browser acceptance with an attachment: proves the per-turn read grant works.

Uploads a synthetic text file, asks for its content, and confirms the answer came
from the file. Real Vite + real FastAPI + real Harness + a local controlled model.
No school account, no paid provider, no user data.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(base: str, out: Path) -> dict:
    scratch = ROOT / ".test-tmp" / "round2-attach-scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = os.environ["TMP"] = os.environ["TMPDIR"] = str(scratch)
    tempfile.tempdir = str(scratch)
    from playwright.sync_api import sync_playwright

    out.mkdir(parents=True, exist_ok=True)
    upload = out / "synthetic-notes.txt"
    upload.write_text("验收要点：提交实验报告和数据附件，截止日期以教务公告为准。\n", encoding="utf-8")
    report: dict = {"base": base}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 960})
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        requests: list[str] = []
        page.on("request", lambda request: requests.append(request.url) if "/api/v1/" in request.url else None)
        page.goto(base + "/chat", wait_until="networkidle")
        page.wait_for_selector('textarea[aria-label="向校园助手提问"]', timeout=20000)
        report["initial_dialogs"] = page.locator('[role="dialog"]').count()
        report["initial_checkboxes"] = page.locator('input[type="checkbox"]').count()

        # The app creates a conversation before attachments can be selected.
        page.click('button:has-text("开始对话")')
        page.wait_for_selector('button[aria-label="浏览器工作区"]', timeout=20000)
        page.fill('textarea[aria-label="向校园助手提问"]', '请读取我上传的这份说明，并总结需要提交的材料')
        page.set_input_files('input[aria-label="上传对话附件"]', str(upload))
        page.wait_for_timeout(2000)
        report["attachment_chip_visible"] = page.get_by_text("synthetic-notes.txt").count() > 0
        page.click('button[aria-label="发送"]')
        page.wait_for_timeout(2000)
        report["dialogs_during_run"] = page.locator('[role="dialog"]').count()
        answer = ""
        for _ in range(60):
            body = page.inner_text(".agent-transcript") if page.locator(".agent-transcript").count() else ""
            if "验收要点" in body or "数据附件" in body:
                answer = body
                break
            page.wait_for_timeout(1000)
        report["answer_mentions_attachment_content"] = "数据附件" in answer
        report["answer_excerpt"] = answer[-600:]
        report["page_errors"] = errors
        report["api_paths"] = sorted({url.split("/api/v1/")[-1].split("?")[0].split("/")[0] for url in requests})
        page.screenshot(path=str(out / "round2-attachment-e2e.png"), full_page=False)
        context.close()
        browser.close()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:15173")
    parser.add_argument("--out", default=str(ROOT / "outputs" / "round2-ui"))
    options = parser.parse_args()
    destination = Path(options.out)
    result = run(options.base, destination)
    (destination / "attachment-report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "answer_excerpt"}, ensure_ascii=False, indent=2))
