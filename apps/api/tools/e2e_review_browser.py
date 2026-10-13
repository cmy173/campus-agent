"""Isolated real browser/API regression; the model endpoint is a local test double.

Run from apps/api with the project venv. Uses only b50 test data, never personal.db.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time

import requests
from playwright.sync_api import sync_playwright

import e2e_review_realapi as scenario


def browser_check() -> None:
    env = os.environ.copy()
    env.update(VITE_API_MODE="real", VITE_PROXY_TARGET="http://127.0.0.1:8317")
    env.pop("VITE_API_BASE_URL", None)
    web = scenario.REPO / "apps" / "web"
    node = shutil.which("node")
    assert node, "Node.js is required"
    log = (scenario.OUT_DIR / "browser-vite.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [node, str(web / "node_modules/vite/bin/vite.js"), "--host", "127.0.0.1",
         "--port", "5199", "--strictPort"], cwd=web, env=env, stdout=log, stderr=log,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        for _ in range(100):
            if proc.poll() is not None:
                raise RuntimeError("Vite stopped; inspect browser-vite.log")
            try:
                if requests.get("http://127.0.0.1:5199", timeout=1).ok:
                    break
            except requests.RequestException:
                pass
            time.sleep(.2)
        notes = scenario.NOTES + "\n网页联调补充：请强调边界条件。"
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto("http://127.0.0.1:5199/courses/crs_3f8a21?tab=review")
            page.get_by_role("button", name="预览将发送的内容").wait_for()
            # Use enabled material checkboxes; initial draft has no selections.
            boxes = page.locator('input[type="checkbox"]:enabled')
            for i in range(boxes.count()):
                boxes.nth(i).check()
            page.get_by_placeholder(re.compile("留空由服务端命名")).fill("网页真实联调复习卷")
            page.get_by_role("textbox", name="出卷备注（会与选定资料原文一起发送）").fill(notes)
            page.get_by_role("button", name="预览将发送的内容").click()
            page.get_by_text("请求指纹", exact=False).wait_for()
            with page.expect_response(lambda r: r.url.endswith("/review-papers/generate") and r.request.method == "POST") as response:
                page.get_by_role("button", name=re.compile("生成复习卷$")).click()
            r = response.value
            assert r.status == 202, r.text()
            job = scenario.poll_job(r.json()["job"]["id"])
            assert job["status"] == "succeeded", job.get("errors")
            papers = requests.get(f"{scenario.BASE}/review-papers", params={"course_id": "crs_3f8a21"}, timeout=10).json()["items"]
            paper_id = next(x["id"] for x in papers if x["title"] == "网页真实联调复习卷")
            paper = requests.get(f"{scenario.BASE}/review-papers/{paper_id}", timeout=10).json()
            assert paper["snapshot"]["notes"]["text"] == notes
            assert all(not str(ref.get("assignment_id") or "").startswith("att::")
                       for question in paper["questions"] for ref in question["source_refs"])
            sent = scenario.CAPTURED[-1]["payload"]["messages"][1]["content"]
            assert notes in sent and "线性表是由n个数据元素构成的有限序列" in sent
            page.goto(f"http://127.0.0.1:5199/reviews/{paper_id}")
            page.get_by_text("网页真实联调复习卷", exact=True).wait_for()
            page.screenshot(path=str(scenario.OUT_DIR / "browser-review.png"), full_page=True)
            for variant in ("student", "answers"):
                page.goto(f"http://127.0.0.1:5199/print/reviews/{paper_id}?variant={variant}")
                page.get_by_role("heading", name="网页真实联调复习卷", exact=True).wait_for()
                body = page.locator("body").inner_text()
                assert ("答案：" in body) == (variant == "answers")
                assert ("解析：" in body) == (variant == "answers")
            page.screenshot(path=str(scenario.OUT_DIR / "browser-answers.png"), full_page=True)
            browser.close()
        scenario.check("真实网页选择材料→预览→生成→详情→学生/答案打印", True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


if __name__ == "__main__":
    for target in (scenario.DB_FILE, scenario.SECRETS_DIR):
        assert target.resolve().is_relative_to((scenario.REPO / "var").resolve())
    original_check = scenario.check

    def check_and_browser(name: str, cond: bool, detail: str = "") -> None:
        original_check(name, cond, detail)
        if name == "题库能力端点 200" and cond:
            browser_check()

    scenario.check = check_and_browser
    raise SystemExit(scenario.main())
