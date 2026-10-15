"""Live acceptance: does the agent bootstrap 学在浙大 by itself?

Drives the real UI (Playwright/Edge headless) with the user's own question, then reads the
turn's actual tool calls out of the journal. Real school reads happen (discovery, course
import, one platform sync) — that is the point of the check. No credentials are handled
here; the connector session in the app does the talking.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUESTION = "这周有哪些作业需要留意？"


def newest_turn(journal: Path) -> dict:
    with sqlite3.connect(journal) as con:
        con.row_factory = sqlite3.Row
        return dict(con.execute("select * from turns order by created_at desc limit 1").fetchone())


def turn_detail(journal: Path, turn_id: str) -> dict:
    with sqlite3.connect(journal) as con:
        con.row_factory = sqlite3.Row
        calls = []
        results = []
        for row in con.execute("select kind,data from events where turn_id=? order by seq", (turn_id,)):
            data = json.loads(row["data"])
            if row["kind"] == "harness_event" and data.get("event", {}).get("type") == "tool/call":
                calls.append({"name": data["event"]["data"].get("name"),
                              "arguments": str(data["event"]["data"].get("arguments"))[:200]})
            if row["kind"] == "tool_result":
                results.append({"name": data.get("name"),
                                "error": (data.get("result") or {}).get("error")})
        turn = dict(con.execute("select * from turns where id=?", (turn_id,)).fetchone())
    return {"turn": turn, "calls": calls, "results": results}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:5173")
    parser.add_argument("--timeout", type=int, default=420)
    parser.add_argument("--out", type=Path, default=ROOT / "outputs" / "homework-bootstrap")
    args = parser.parse_args()

    scratch = ROOT / ".test-tmp" / "homework-bootstrap-scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = os.environ["TMP"] = os.environ["TMPDIR"] = str(scratch)
    tempfile.tempdir = str(scratch)

    from playwright.sync_api import sync_playwright

    args.out.mkdir(parents=True, exist_ok=True)
    journal = Path(os.environ.get("CAMPUS_AGENT_HOME", ROOT / "var" / "agent")) / "journal.sqlite"
    before = newest_turn(journal)["id"] if journal.exists() else None

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_context(viewport={"width": 1440, "height": 960}).new_page()
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(args.base + "/chat", wait_until="networkidle")
        page.wait_for_selector('textarea[aria-label="向校园助手提问"]', timeout=30000)
        page.fill('textarea[aria-label="向校园助手提问"]', QUESTION)
        page.click('button[aria-label="发送"]')

        deadline = time.monotonic() + args.timeout
        turn_id = None
        while time.monotonic() < deadline:
            time.sleep(3)
            if not journal.exists():
                continue
            latest = newest_turn(journal)
            if latest["id"] != before:
                turn_id = latest["id"]
                if latest["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                    break
        page.screenshot(path=str(args.out / "homework-bootstrap.png"))
        detail = turn_detail(journal, turn_id) if turn_id else {"calls": [], "results": [], "turn": {}}
        browser.close()

    report = {"question": QUESTION, "base": args.base, "page_errors": errors,
              "status": detail["turn"].get("status"),
              "response": (detail["turn"].get("response") or "")[:2000],
              "error": detail["turn"].get("error"),
              "tool_calls": detail["calls"], "tool_results": detail["results"],
              "school_data_read": any(c["name"] == "campus_connections_courses" for c in detail["calls"]),
              "courses_imported": any(c["name"] == "campus_connections_courses_import" for c in detail["calls"]),
              "platform_sync_started": any(c["name"] == "campus_connections_sync" for c in detail["calls"])}
    (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "school_data_read", "courses_imported",
                                                  "platform_sync_started", "tool_calls", "error")},
                     ensure_ascii=False)[:1800])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
