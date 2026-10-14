"""B19 ICS 导出：格式受理、时区渲染、全天/定时区分、范围过滤与序列化正确性。

基线取材种子数据（conftest 装载契约样例）：active 计划 pln_6a0b（块 blk_6a01 → tsk_6f2b47）、
candidate 计划 pln_7a1b（其块不得导出）、可用时间 Asia/Shanghai。
用例只增不改：自建任务/状态改动由夹具快照恢复；产物目录指向 tmp_path，不污染 var/。
"""
from __future__ import annotations

import re
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config
from app.ics import _text as ics_escape
from test_sync_api import _drain_until

ACTIVE_PLAN = "pln_6a0b"
BLOCK_ID = "blk_6a01"
BLOCK_TASK = "tsk_6f2b47"
CANDIDATE_BLOCK = "blk_7a01"
PAST_DUE_TASK = "tsk_2c8d13"  # due 2026-10-01T23:59+08（datetime 精度）


@pytest.fixture()
def ics_env(client: TestClient, tmp_path, monkeypatch):
    """FILES_DIR 隔离 + 新增行/计划状态快照恢复。"""
    from app.db import SessionLocal
    from app.models import FileRecord, IdempotencyRecord, Job, LearningTask, Plan

    monkeypatch.setattr(config, "FILES_DIR", tmp_path / "files")
    with SessionLocal() as db:
        jobs = set(db.execute(select(Job.id)).scalars())
        files = set(db.execute(select(FileRecord.id)).scalars())
        tasks = set(db.execute(select(LearningTask.id)).scalars())
        idem = set(db.execute(select(IdempotencyRecord.key)).scalars())
        plan_status = {p.id: p.status for p in db.execute(select(Plan)).scalars()}
    yield
    with SessionLocal() as db:
        for row in db.execute(select(Job)).scalars():
            if row.id not in jobs:
                db.delete(row)
        for row in db.execute(select(FileRecord)).scalars():
            if row.id not in files:
                db.delete(row)
        for row in db.execute(select(LearningTask)).scalars():
            if row.id not in tasks:
                db.delete(row)
        for row in db.execute(select(IdempotencyRecord)).scalars():
            if row.key not in idem:
                db.delete(row)
        for plan in db.execute(select(Plan)).scalars():
            if plan.id in plan_status:
                plan.status = plan_status[plan.id]
        db.commit()


def _export_ics(client: TestClient, body: dict | None = None):
    resp = client.post("/api/v1/exports", json=body or {"format": "ics"})
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job"]["id"]
    assert _drain_until(job_id) == "succeeded"
    job = client.get(f"/api/v1/jobs/{job_id}").json()
    refs = job["result_refs"]
    assert refs and refs[0]["type"] == "export_file"
    download = client.get(f"/api/v1/files/{refs[0]['id']}")
    assert download.status_code == 200, download.text
    return download, job_id


def _unfold(text: str) -> str:
    return text.replace("\r\n ", "")


def _physical_lines(text: str) -> list[str]:
    lines = text.split("\r\n")
    assert lines[-1] == ""  # 末尾 CRLF
    return lines[:-1]


def test_ics_export_seed_scope_and_timezone(client: TestClient, ics_env) -> None:
    """种子基线：active 块 + datetime 截止按用户时区渲染；candidate/未知精度/取消任务不导出。"""
    from app.db import SessionLocal
    from app.models import LearningTask

    download, _ = _export_ics(client)
    assert download.headers["content-type"].startswith("text/calendar")
    disposition = download.headers.get("content-disposition", "")
    assert re.search(r'filename="study-plan-\d{8}T\d{6}Z\.ics"', disposition), disposition

    text = download.content.decode("utf-8")
    assert text.startswith("BEGIN:VCALENDAR\r\n") and text.endswith("END:VCALENDAR\r\n")
    assert text.count("\n") == text.count("\r\n")  # 无裸 LF（RFC 5545 行尾）
    assert all(len(line.encode("utf-8")) <= 75 for line in _physical_lines(text))  # 折行上限

    # 时区定义与头部
    assert "TZID:Asia/Shanghai" in text and "TZOFFSETTO:+0800" in text
    assert "X-WR-TIMEZONE:Asia/Shanghai" in text

    # 生效计划块：UTC 2026-10-08T11:00Z → 上海 19:00（用户时区渲染的直接证据）
    with SessionLocal() as db:
        block_task_title = db.get(LearningTask, BLOCK_TASK).title
    assert f"UID:{BLOCK_ID}@study-workbench" in text
    assert "DTSTART;TZID=Asia/Shanghai:20261008T190000" in text
    assert "DTEND;TZID=Asia/Shanghai:20261008T210000" in text
    assert f"SUMMARY:{ics_escape(block_task_title)}" in text

    # datetime 截止：23:59+08 而不是 UTC 15:59（时区换算正确性的关键断言）
    assert f"UID:{PAST_DUE_TASK}@study-workbench" in text
    assert "DTSTART;TZID=Asia/Shanghai:20261001T235900" in text

    # 范围过滤：candidate 计划块、unknown 精度、取消任务均不出现
    assert CANDIDATE_BLOCK not in text
    assert "tsk_5a2b63" not in text  # cancelled
    assert "tsk_0f3a91" not in text  # due_precision=unknown / 无截止

    # UID 稳定性：重复导出同一数据 → UID 集合一致（日历客户端去重前提）
    download2, _ = _export_ics(client)
    text2 = download2.content.decode("utf-8")
    uids1 = {line for line in _unfold(text).split("\r\n") if line.startswith("UID:")}
    uids2 = {line for line in _unfold(text2).split("\r\n") if line.startswith("UID:")}
    assert uids1 == uids2 and uids1


def test_ics_all_day_vs_timed_and_exclusions(client: TestClient, ics_env) -> None:
    """全天（VALUE=DATE，按用户时区判日期）与定时区分；归档/取消/未知精度不导出。"""
    from app.db import SessionLocal
    from app.models import LearningTask, utc_now

    with SessionLocal() as db:
        db.add(
            LearningTask(
                id="tsk_ics_day1",
                type="assignment",
                title="全天截止作业",
                estimated_minutes=30,
                due_at=datetime(2026, 10, 12, 16, 0),  # UTC 16:00 = 上海 2026-10-13 00:00
                due_precision="date",
                status="pending",
                fact_source="platform",
            )
        )
        db.add(
            LearningTask(
                id="tsk_ics_cancel1",
                type="assignment",
                title="已取消作业",
                estimated_minutes=30,
                due_at=datetime(2026, 10, 7, 4, 0),  # 上海 12:00
                due_precision="datetime",
                status="cancelled",
                fact_source="platform",
            )
        )
        db.add(
            LearningTask(
                id="tsk_ics_arch1",
                type="reading",
                title="已归档任务",
                estimated_minutes=30,
                due_at=datetime(2026, 10, 8, 4, 0),
                due_precision="datetime",
                status="pending",
                fact_source="platform",
                archived_at=utc_now(),
            )
        )
        db.add(
            LearningTask(
                id="tsk_ics_unk1",
                type="assignment",
                title="精度未知",
                estimated_minutes=30,
                due_at=datetime(2026, 10, 9, 4, 0),
                due_precision="unknown",
                status="pending",
                fact_source="platform",
            )
        )
        db.commit()

    download, _ = _export_ics(client)
    text = download.content.decode("utf-8")

    # 全天事件：无时刻、无 TZID；结束为次日（半开区间）
    assert "DTSTART;VALUE=DATE:20261013" in text
    assert "DTEND;VALUE=DATE:20261014" in text
    assert "DTSTART;TZID=Asia/Shanghai:20261013T000000" not in text  # 全天不得渲染成定时

    assert "tsk_ics_cancel1" not in text
    assert "tsk_ics_arch1" not in text
    assert "tsk_ics_unk1" not in text


def test_ics_include_is_rejected_for_ics_only(client: TestClient, ics_env) -> None:
    resp = client.post("/api/v1/exports", json={"format": "ics", "include": ["tasks"]})
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_FAILED" and error["details"]["format"] == "ics"
    # csv 等未知格式仍拒（b4 用例的 ICS 扩展回归口径）
    assert client.post("/api/v1/exports", json={"format": "csv"}).status_code == 400


def test_ics_without_active_plan_still_exports_deadlines(client: TestClient, ics_env) -> None:
    from app.db import SessionLocal
    from app.models import Plan

    with SessionLocal() as db:
        db.get(Plan, ACTIVE_PLAN).status = "superseded"
        db.commit()

    download, _ = _export_ics(client)
    text = download.content.decode("utf-8")
    assert BLOCK_ID not in text  # 无生效计划 → 无计划块
    assert f"UID:{PAST_DUE_TASK}@study-workbench" in text  # 截止仍导出


def test_ics_long_title_folded_and_escaped(client: TestClient, ics_env) -> None:
    from app.db import SessionLocal
    from app.models import LearningTask

    title = "长标题测试与转义" * 12 + ",含逗号; 分号\\反斜杠"
    with SessionLocal() as db:
        db.add(
            LearningTask(
                id="tsk_ics_long1",
                type="reading",
                title=title,
                estimated_minutes=30,
                due_at=datetime(2026, 10, 21, 4, 0),
                due_precision="datetime",
                status="pending",
                fact_source="platform",
            )
        )
        db.commit()

    download, _ = _export_ics(client)
    text = download.content.decode("utf-8")
    assert all(len(line.encode("utf-8")) <= 75 for line in _physical_lines(text))
    unfolded = _unfold(text)
    assert f"SUMMARY:{ics_escape(title)}" in unfolded
    # 转义实际发生（逗号/分号/反斜杠以转义序列形态出现）
    summary_line = [line for line in unfolded.split("\r\n") if line.startswith("SUMMARY:") and "长标题测试" in line][0]
    assert "\\,含逗号" in summary_line
    assert "\\; 分号" in summary_line
    assert "\\\\反斜杠" in summary_line
