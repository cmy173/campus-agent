#!/usr/bin/env python
"""M3 real-model half for the campus read-only round: 4 tasks x 1 independent real-model session.

Acceptance contract: ``docs/audit/harness-campus-readonly-acceptance.md`` ("四级证据与评测" item 3) and
``docs/agent-design/harness-campus-readonly-task.md`` section 5 ("真实模型"): four independent real-model
sessions -- 通知依据回答 / 课程资料与截止 / 课表学习安排 / 登录失效后只读继续 -- each driving the REAL
FastAPI app, the REAL installed Harness (``apps/agent-runtime/runner.mjs``) and the REAL saved official
provider, against FIXED synthetic school data.

This driver is offline by default. Without ``--model-live`` it refuses and exits 2 before reading the
provider row, the credential file or calling any model. It never asks for, prints, copies into outputs or
logs a key; a final redaction pass removes credential/ticket/signed-URL shaped strings from the record.

What is real
    - the saved, enabled official provider row ``prv_c68d2878`` read READ-ONLY from ``var/personal.db``
      (host asserted to be the official ``api.deepseek.com``) and the saved DPAPI credential copied into the
      run's isolated secrets dir (never decrypted here, never archived, deleted after the session);
    - the real FastAPI app (uvicorn, ``app.main:app``), the real worker process for the sync job, the real
      domain DB models/repository/sync engine, the real Harness runner + campus tool surface, and the real
      paid provider calls (metered).

What is synthetic and isolated (fresh UUID home per session)
    - the isolated domain SQLite DB, files/workspace/harness-home/campus/tmp dirs and secrets dir;
    - the synthetic school itself: a fixed local timetable/calendar + local learning tasks + a single
      synthetic college notice in the campus cache, and a local synthetic platform adapter registered through
      the supported ``STUDY_SYNC_ADAPTERS`` configuration seam. The school is never contacted: the campus
      tools used here are cache reads, and the only platform transport is the local adapter module.
    - ``var/personal.db``, ``var/files``, ``var/secrets``, ``var/agent`` and ``var/workspace`` are never
      written.

Judging (judge v1, see ``JUDGE_CHANGELOG``)
    A verdict is decided from the recorded trajectory (ordered tool calls with their returned status and
    compacted evidence) plus the final answer, never from "the expected tool ran". Turn status must be
    ``succeeded`` and the final answer non-empty. Four sessions is a small diagnostic sample: a judge pass
    means *these recorded checks* held, not a statistical success rate.

Usage
    # refusal (no --model-live): exits 2 before any provider/credential read
    apps/api/.venv/Scripts/python.exe -X utf8 tools/verify_campus_readonly_live.py

    # offline judge regression (no provider read, no credential access, no model call)
    apps/api/.venv/Scripts/python.exe -X utf8 tools/verify_campus_readonly_live.py --self-test \
        --out var/harness-campus-readonly-2b0480ed/m3/selftest.json

    # the four paid real-model sessions (the lead runs this)
    apps/api/.venv/Scripts/python.exe -X utf8 tools/verify_campus_readonly_live.py --model-live

    # one task only, or a fresh evidence file
    ... --model-live --tasks T1,T4 --out var/harness-campus-readonly-2b0480ed/m3/model-live-retry.json

Exit codes
    0  every requested session ran and achieved its judged goal (``--self-test``: all cases passed)
    1  at least one recorded session did not achieve its goal, failed, or hit a budget limit
       (``--self-test``: at least one judge case failed)
    2  refusal / gate: missing ``--model-live``, bad ``--tasks``/``--runs``
    3  cannot run: no saved enabled official provider row or no saved credential file
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "apps" / "api"
PROVIDER_ID = "prv_c68d2878"
OFFICIAL_PROVIDER_HOST = "api.deepseek.com"
EVIDENCE_DIR = ROOT / "var" / "harness-campus-readonly-2b0480ed" / "m3"
DEFAULT_OUT = EVIDENCE_DIR / "model-live.json"
DEFAULT_SCRATCH_ROOT = EVIDENCE_DIR / "scratch"
SELFTEST_SENTINEL = EVIDENCE_DIR / "selftest-must-not-read"

sys.path.insert(0, str(ROOT / "tools"))
import verify_next_round_live as live  # noqa: E402  (reused building blocks; its CLI stays untouched)

# Hard per-turn caps. They are pushed into the API process as CAMPUS_MAX_* so the server enforces them too;
# the driver additionally measures them and fails a session honestly instead of truncating silently.
BUDGET = {"model_requests": 12, "tool_calls": 40, "seconds": 150}
MAX_RECOVERY_TURNS = 2
COST = "unknown"
COST_BASIS = "no billing basis available; per-call token usage is recorded but not priced"

JUDGE_VERSION = 2
JUDGE_CHANGELOG = {
    2: ("R3 fixes from the independent review; no other rule changed. "
        "(a) The whole-school check is now clause-aware and markdown-tolerant: a claim counts only when its "
        "own clause (split on Chinese/ASCII sentence and comma marks, emphasis markers stripped) does not "
        "negate or limit it. This fixes the reproduced false positive "
        "'本机缓存里**没有**能读到正文的「全校性本科生选课确认」通知。', which the six-character look-behind "
        "mis-flagged; genuinely affirmative claims ('覆盖全部学院', '全校所有学院均须参加') still fail. "
        "(b) `job_observed` also accepts the terminal state of the SAME durable sync job returned by a "
        "`connections.sync` issued with an explicit `wait_seconds`, provided the worker's own row records that "
        "job/status as terminal; a redundant `jobs.read` is no longer required. queued/running, a job id the "
        "worker never recorded, and a status it never recorded still fail, and business success is still "
        "judged by `sync_job_succeeded`."),
    1: ("initial rules. Every task requires turn_succeeded and a non-empty final answer, judged from the "
        "recorded trajectory (tool name, scrubbed arguments, returned status, compacted evidence) and the "
        "final answer, never from 'the expected tool ran'. "
        "T1: the notice FULL body must have been returned by a successful campus read -- either "
        "campus.notice.read or a campus cache read whose recorded body digest equals the seeded body "
        "(body_chars + sha256); a title-only list/summary can never satisfy it. The answer must name the "
        "title and date, cite the body reference number, state the limited applicability scope, make no "
        "affirmative whole-school claim (negated/limited wording is exempt) and mark a title-only "
        "login-board notice as a summary if it cites it. "
        "T2: a cursor page read of the platform listing, a local attach of the page-2 target course, a real "
        "sync job that the real worker finished with succeeded, an observed terminal jobs.read, a real "
        "material body read (text length + digest from the tool result, never directory metadata), an "
        "honest statement about the unparsed material, an unknown deadline that stays unknown in the "
        "envelope, the DB row AND the answer sentence naming it, and an answer citing the material. "
        "T3: a successful campus.academics and tasks.list, a pending apply action from plans.preview (with "
        "the persisted candidate plan/blocks/warnings as fallback evidence, and naive DB instants read as "
        "UTC), no plan block overlapping the timetable blocks the fixed fixture must admit for that "
        "horizon, the CAMPUS_TIMETABLE_UNCERTAIN warning acknowledged in the answer, and no formal plan "
        "write (no successful plans.apply/revoke, no active plan, no completed action). "
        "T4: a structured AUTH_* error from a campus read in turn 1, a persisted checkpoint whose pending "
        "tool is a campus read and whose original goal is turn 1's own goal, a trusted human-auth "
        "notification answered 200 by the app's own entry, a recorded probe outcome (a failure is recorded "
        "honestly), a second turn metered with its own 12/40/150 budget, no successful write-effect call in "
        "the resumed turn, no out-of-scope id or subsystem, no repeat of a completed WRITE-effect action "
        "(re-reading a status is recorded, not failed), and -- because the read really does depend on a live "
        "school session -- the server's own trusted automatic resume must have completed for the goal to "
        "pass; a driver-initiated fallback turn never substitutes for it."),
}

TURN_SUCCESS_STATES = {"succeeded"}
AUTH_ERROR_CODES = {"AUTH_REQUIRED", "AUTH_EXPIRED", "SESSION_EXPIRED", "BROWSER_AUTH_REQUIRED"}

# Effects per tool, as published by apps/api/app/agent_bridge/tools.py::schemas() at this source revision.
# Reviewed by hand for judge v1; a resumed automatic turn is server-guarded to read-only tools, and the
# manual fallback turn is checked against this list.
WRITE_EFFECT_TOOLS = frozenset({
    "campus.notice.import", "plans.preview", "plans.apply", "plans.revoke", "tasks.build", "jobs.cancel",
    "tasks.feedback", "connections.sync", "connections.courses.import", "artifacts.export",
    "reviews.generate", "reviews.export",
})
CAMPUS_READ_TOOLS = frozenset({
    "campus.status", "campus.query", "campus.sync", "campus.academics", "campus.notice.read",
    "campus.imported.read", "campus.webvpn.entry",
})

# ───────────────────────── synthetic school fixtures (fixed; never model input) ─────────────────────────

NOTICE_ID = "ckc:3001200001"
NOTICE_SOURCE = "https://webplus.zju.edu.cn/_s597/2026/0311/c59700a3001200001/page.psp"
NOTICE_COLLEGE = "竺可桢学院"
NOTICE_TITLE = "关于开展2026年秋冬学期本科生选课确认的通知"
NOTICE_DATE = "2026-03-11"
# Only the notice body carries this reference number; a list summary never does.
NOTICE_TOKEN = "CK-2026-0311"
NOTICE_BODY = (
    "各位同学：\n"
    "本次选课确认仅面向竺可桢学院2026级本科生，其他学院学生的选课安排由所在学院另行通知。\n"
    "请于2026年3月18日前登录教务系统完成确认；逾期未确认的课程将由学院统一处理。\n"
    "通知编号：" + NOTICE_TOKEN + "。\n"
    "如需咨询，请联系竺可桢学院教学办公室；本通知不涉及全校性选课政策调整。\n"
)

# Title-only login-board summaries: they exist so "a summary is not the full text" is a real distinction.
BOARD_NOTICES = [
    {"id": "1a2b3c4d5e6f708192a3b4c5d6e7f801",
     "title": "关于2026年秋冬学期本科选课工作安排的通知",
     "published_at": "2026-03-10 09:00:00"},
    {"id": "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
     "title": "关于2026年秋冬学期教学日历发布的通知",
     "published_at": "2026-03-05 16:30:00"},
]

MATERIAL_RESOURCE_ID = "res_syn_material"
MATERIAL_VERSION = 1
MATERIAL_TITLE = "合成实验指导书"
MATERIAL_TOKEN = "SYN-MAT-7"
MATERIAL_CHUNKS = [
    "合成实验指导书（合成数据，不代表任何真实课程）。\n"
    "实验报告必须包含独立的“误差分析”一节，并在结论中给出不确定度区间。引用号：" + MATERIAL_TOKEN + "。",
    "第二条合成材料要点：数据表需标注单位与测量次数；未测项目必须写“未测”，不得补零。",
]
PENDING_RESOURCE_ID = "res_syn_pending"
PENDING_RESOURCE_TITLE = "合成实验要求（未解析原件）"
TARGET_COURSE_ID = "crs_syn_target"
TARGET_COURSE_TITLE = "合成实验课程B"
TARGET_COURSE_TERM = "2026-2027秋冬"
TARGET_COURSE_EXTERNAL_ID = "xzzd-course-target"
MATERIAL_EXTERNAL_ID = "xzzd-res-material"
PENDING_EXTERNAL_ID = "xzzd-res-pending"
CONNECTION_ID = "conn_syn_xzzd"
PLATFORM = "xzzd"
UNKNOWN_ASSIGNMENT_TITLE = "合成实验报告提交"
UNKNOWN_ASSIGNMENT_INSTRUCTIONS = "提交实验报告初稿与原始数据表；截止时间以平台通知为准。"
KNOWN_ASSIGNMENT_TITLE = "合成预习测验"
KNOWN_ASSIGNMENT_INSTRUCTIONS = "完成第 1-2 章预习测验，限时 20 分钟。"
# Page 2 holds the target course: paging is required to find the course the user asks about.
PLATFORM_PAGES = [
    [{"external_id": "xzzd-course-decoy-1", "title": "合成通识课程A", "term": TARGET_COURSE_TERM, "teacher": "合成教师甲"},
     {"external_id": "xzzd-course-decoy-2", "title": "合成通识课程C", "term": TARGET_COURSE_TERM, "teacher": "合成教师乙"}],
    [{"external_id": TARGET_COURSE_EXTERNAL_ID, "title": TARGET_COURSE_TITLE, "term": TARGET_COURSE_TERM, "teacher": "合成教师丙"}],
]

T3_COURSE_ID = "crs_syn_study"
T3_COURSE_TITLE = "合成学习课程"
# Any weekday is occupied: the horizon of a one-week preview therefore contains an admitted class block
# whatever day the paid session is started, so "avoid real class blocks" is never a vacuous check.
T3_CLASS_PERIODS = ("1-2",)
T3_CLASS_TIMES = ("08:00", "09:40")
T3_CLASS_WEEKDAYS = ("1", "2", "3", "4", "5", "6", "7")
T3_CLASS_WEEKS = "1-16"
T3_CLASS_SEASON = "秋冬"
T3_CLASS_TERM = "2026-2027秋冬"
# Campus semesters are codes, not seasons: the connector contract only accepts e.g. "1|秋"/"1|秋冬"
# (SEMESTER_PATTERN). The calendar key is year + "/" + that same code.
T3_CAMPUS_YEAR = "2026"
T3_CAMPUS_SEMESTER = "1|秋冬"
T3_CAMPUS_TERM_KEY = T3_CAMPUS_YEAR + "/" + T3_CAMPUS_SEMESTER
T3_AMBIGUOUS_TITLE = "合成时间待定课程"
CALENDAR_PERIOD_TIMES = [
    ["08:00", "08:45"], ["08:55", "09:40"], ["10:00", "10:45"], ["10:55", "11:40"],
    ["13:15", "14:00"], ["14:10", "14:55"], ["15:10", "15:55"], ["16:05", "16:50"],
    ["18:30", "19:15"], ["19:25", "20:10"], ["20:20", "21:05"], ["21:15", "22:00"],
]
T3_LEARNING_TASKS = [
    {"id": "tsk_syn_review", "type": "review", "title": "复习合成课程第三章", "estimated_minutes": 120,
     "due_offset_days": 1, "due_time": "21:00"},
    {"id": "tsk_syn_report", "type": "homework", "title": "完成合成实验报告初稿", "estimated_minutes": 180,
     "due_at": None, "due_precision": "unknown"},
    {"id": "tsk_syn_reading", "type": "reading", "title": "阅读合成材料第二章", "estimated_minutes": 60,
     "due_offset_days": 4, "due_time": "20:00"},
]
T3_UNCERTAINTY_QUALIFIERS = ("未确认", "不确定", "未核实", "待确认", "可能", "仅供参考", "校历", "以实际课表为准",
                             "无法确认", "尚未确认")
UNPARSED_QUALIFIERS = ("未解析", "无正文", "待解析", "无法读取", "未提供正文", "解析中", "没有正文")

T1_PROMPT = (
    "请根据本机已缓存的校园数据回答：2026年3月，学校关于本科生选课确认的通知是什么？\n"
    "要求：\n"
    "1) 给出通知标题与发布日期；\n"
    "2) 给出你得出结论所依据的通知原文依据（引用原文语句或通知编号），只有真正读到的正文才算依据；\n"
    "3) 说明这条通知的适用范围；\n"
    "4) 只使用本机已缓存的校园数据；缓存里只有标题/摘要的通知只能当线索，不能当作读过全文；"
    "缓存中无法确认的信息请如实说明，不要补全或夸大覆盖范围。"
)
T2_PROMPT = (
    "我在学在浙大上有一门叫《" + TARGET_COURSE_TITLE + "》的课。请只读地帮我把这门课的资料和作业看到"
    "能回答下面的问题：\n"
    "1) 这门课当前的资料里，对实验报告提出了哪一条硬性要求？请引用资料原文；\n"
    "2) 这门课现在有哪些作业，各自的截止时间是什么？\n"
    "要求：\n"
    "- 平台上可能不只一页课程，注意分页，不要把第一页当成全部；把目标课程挂接到本机；\n"
    "- 回答课程资料/作业问题前要先真实同步并等到后台任务结束，不能用“已受理”当作完成；\n"
    "- 只有标题/目录元数据的资料不算读到正文；读不到的部分请如实说明；\n"
    "- 截止时间未知的作业必须保持未知，不要推测日期；\n"
    "- 不要提交任何作业，也不要在平台上做写操作。"
)
T3_PROMPT = (
    "请根据本机缓存的课表和校历，加上我本机的学习任务，为本周做一个学习安排预览。\n"
    "要求：\n"
    "1) 先说明你依据的课表/校历来源与精度；课表或校历不确定的地方要如实说明；\n"
    "2) 预览必须避开真实的上课时间，不能把学习任务排进上课时段；\n"
    "3) 只做预览，不要写成正式计划，也不要替我应用；\n"
    "4) 我在本机已登记了学习任务，如果某项任务无法安排请说明原因。"
)
T4_PROMPT = (
    "请读取我本学期的校园成绩并汇总说明（课程、成绩、绩点）。只读操作，不要修改任何数据。"
)
T4_RESUME_PROMPT = (
    "（用户在受控界面完成校园登录后回到本对话）请只读地继续原来的目标：读取本学期校园成绩并汇总说明。\n"
    "要求：只读；不要扩大读取范围；不要重复已经完成的步骤；仍然读不到就如实说明还缺什么，"
    "绝对不要编造成绩数据。"
)

TASKS = {
    "T1": {"title": "通知依据回答", "prompt": T1_PROMPT,
           "criteria": ["turn_succeeded", "final_answer_nonempty", "notice_full_text_read",
                        "answer_title_and_date", "answer_cites_body", "answer_scope_limited",
                        "no_whole_school_claim", "summary_not_misrepresented"]},
    "T2": {"title": "课程资料与截止", "prompt": T2_PROMPT,
           "criteria": ["turn_succeeded", "final_answer_nonempty", "course_discovered_by_paging",
                        "course_attached_locally", "sync_job_succeeded", "job_observed",
                        "material_body_read", "unparsed_material_honest", "unknown_deadline_stays_unknown",
                        "answer_cites_material"]},
    "T3": {"title": "课表学习安排", "prompt": T3_PROMPT,
           "criteria": ["turn_succeeded", "final_answer_nonempty", "timetable_and_tasks_read",
                        "preview_created", "preview_avoids_class_blocks", "timetable_uncertainty_honest",
                        "no_formal_plan_written"]},
    "T4": {"title": "登录失效后只读继续", "prompt": T4_PROMPT,
           "criteria": ["turn_succeeded", "final_answer_nonempty", "turn1_auth_error_observed",
                        "checkpoint_persisted", "trusted_human_auth_notified", "probe_outcome_recorded",
                        "resumed_turn_separately_metered", "resumed_no_write", "resumed_no_scope_widening",
                        "resumed_no_repeat_of_completed_action", "resumed_honest_about_blocker",
                        "trusted_automatic_resume_completed"]},
}

NOTES_UNCONTROLLED = [
    "sampling: the provider bridge exposes no temperature/top_p/seed control; provider defaults apply, so "
    "these sessions are not deterministic and no determinism is claimed",
    "provider-side routing/model-version selection and network/rate-limit behaviour are uncontrolled",
    "no real school was contacted: the campus evidence is fixed synthetic data in the isolated cache and the "
    "platform transport is a local synthetic adapter, so these runs are NOT evidence about real school "
    "protocols, real accounts or real authentication",
    "the real model may choose different tools per session; the judge only checks the recorded trajectory",
    "four sessions is a small diagnostic sample, not a success rate",
    "T4 automatic recovery: the trusted probe re-executes the persisted read through the real campus service, "
    "which needs a live authorized school session. With no school session (and no product change) the probe "
    "cannot be confirmed; when that happens the record says so and the verdict is not a pass",
]

# ───────────────────────────────── generic helpers ─────────────────────────────────

_SECRET_KEY = re.compile(
    r"(password|passwd|secret|credential|cookie|authorization|access_token|refresh_token|api_key|"
    r"session_token|csrf|otp|ticket|signature|signed|^token$|_token$|token_)", re.I)
_SECRET_TEXT = re.compile(
    r"(?i)(\b(?:password|passwd|secret|access_token|refresh_token|api_key|session_token|cookie|authorization)"
    r"\s*[:=]\s*)([^\s,;\"']+)"
    r"|\bBearer\s+[A-Za-z0-9._\-]{12,}"
    r"|sk-[A-Za-z0-9]{16,}"
    r"|iplanetdirectorypro[=:]\s*[^\"'\s,}]+"
    r"|https://webvpn\.zju\.edu\.cn/https/[^\s\"'\\]+"
    r"|[?&](?:ticket|token|signature|Signature|X-Amz-Signature|sign|apikey|api_key)=[^\s\"'&\\]+")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def clip(value, limit: int = 400):
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…[truncated]"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…[truncated]"


def scrub_string(value: str, limit: int = 400) -> str:
    return clip(_SECRET_TEXT.sub("[REDACTED]", value), limit)


def scrub_arguments(value, depth: int = 0):
    """Model-facing argument summary: no credentialed keys, no signed URLs, bounded size."""
    if depth > 4:
        return "[depth]"
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if _SECRET_KEY.search(str(key)):
                out[str(key)] = "[dropped]"
            else:
                out[str(key)] = scrub_arguments(item, depth + 1)
        return out
    if isinstance(value, list):
        return [scrub_arguments(item, depth + 1) for item in value[:10]]
    if isinstance(value, str):
        return scrub_string(value)
    return value


def redact_tree(value, counter: dict):
    """Last-resort redaction over a whole record: credential/ticket/signed-URL shaped strings."""
    if isinstance(value, dict):
        return {key: redact_tree(item, counter) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_tree(item, counter) for item in value]
    if isinstance(value, str):
        cleaned = _SECRET_TEXT.sub("[REDACTED]", value)
        if cleaned != value:
            counter["redactions"] = counter.get("redactions", 0) + 1
        return cleaned
    return value


def git_facts() -> dict:
    def run(args):
        try:
            done = subprocess.run(["git"] + args, cwd=str(ROOT), capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:  # noqa: BLE001 - diagnostics only
            return None, f"{type(exc).__name__}: {exc}"
        return done.stdout, None
    head, error = run(["rev-parse", "HEAD"])
    status_out, _ = run(["status", "--porcelain"])
    dirty = [line for line in (status_out or "").splitlines() if line.strip()]
    return {"git_head": (head or "").strip() or None, "dirty_files": dirty[:500],
            "dirty_files_total": len(dirty), "dirty_files_truncated": len(dirty) > 500,
            "git_error": error}


def db_rows(db_path: Path, sql: str, params=()) -> list:
    connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, params).fetchall()]
    finally:
        connection.close()


def journal_auth_recovery(agent_home: Path) -> list:
    """Read this run's own journal: the persisted checkpoint is the app's durable record."""
    journal = agent_home / "journal.sqlite"
    if not journal.is_file():
        return []
    try:
        rows = db_rows(journal, "select id,conversation_id,source_turn_id,status,probe_requested,next_probe_at,"
                                "probe_result,error,continuation_turn_id,payload,created_at,updated_at "
                                "from auth_recovery order by created_at")
    except sqlite3.Error as exc:
        return [{"error": f"{type(exc).__name__}: {exc}"}]
    out = []
    for row in rows:
        payload = {}
        try:
            payload = json.loads(row.get("payload") or "{}")
        except ValueError:
            payload = {}
        pending = payload.get("pending_tool") or {}
        out.append({
            "id": row["id"], "conversation_id": row["conversation_id"], "source_turn_id": row["source_turn_id"],
            "status": row["status"], "probe_requested": bool(row["probe_requested"]),
            "next_probe_at": row["next_probe_at"],
            "probe_result_present": bool(row["probe_result"]),
            "probe_result_error": (json.loads(row["probe_result"]).get("error")
                                   if row["probe_result"] else None),
            "error": json.loads(row["error"]) if row["error"] else None,
            "continuation_turn_id": row["continuation_turn_id"],
            "pending_tool": {"name": pending.get("name"),
                             "arguments": scrub_arguments(pending.get("arguments") or {})},
            "pending_tool_is_campus_read": pending.get("name") in CAMPUS_READ_TOOLS,
            "original_goal": clip(payload.get("original_goal"), 600),
            "original_turn_id": payload.get("original_turn_id"),
            "scope": payload.get("scope"),
            "created_at": row["created_at"], "updated_at": row["updated_at"]})
    return out


def journal_conversation_settings(agent_home: Path, conversation_id: str) -> dict:
    """This conversation's own grant from the isolated journal (course/connection/job scope)."""
    journal = agent_home / "journal.sqlite"
    if not journal.is_file():
        return {}
    try:
        rows = db_rows(journal, "select settings from conversations where id=?", (conversation_id,))
    except sqlite3.Error:
        return {}
    if not rows:
        return {}
    try:
        settings = json.loads(rows[0]["settings"] or "{}")
    except ValueError:
        return {}
    return {"course_ids": sorted(settings.get("course_ids") or []),
            "connection_ids": sorted(settings.get("connection_ids") or []),
            "job_ids": sorted(settings.get("job_ids") or []),
            "current_course_id": settings.get("current_course_id"),
            "provider_id": (settings.get("provider") or {}).get("id")}


def conversation_scope(api, conversation_id: str) -> dict:
    view = api.get(f"/api/v1/agent/conversations/{conversation_id}", timeout=30).json()
    return {"course_ids": sorted(view.get("course_ids") or []),
            "current_course_id": view.get("current_course_id"),
            "provider_id": view.get("provider_id"),
            "turn_ids": [item["id"] for item in view.get("turns") or []]}


def extract_tool_calls(events: list) -> list:
    """Ordered tool calls of one turn from the API journal (authoritative status + returned envelope)."""
    calls = []
    for event in events:
        if event.get("kind") != "tool_result" or not isinstance(event.get("data"), dict):
            continue
        data = event["data"]
        envelope = data.get("result") if isinstance(data.get("result"), dict) else {}
        error = envelope.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        name = str(data.get("name") or "")
        try:
            arguments = json.loads(data.get("arguments") or "{}")
        except (TypeError, ValueError):
            arguments = data.get("arguments")
        calls.append({
            "position": len(calls) + 1,
            "call_id": data.get("call_id"),
            "name": name,
            "model_name": "campus_" + name.replace(".", "_"),
            "arguments": scrub_arguments(arguments),
            "status": envelope.get("status"),
            "error": code,
            "job_id": envelope.get("job_id"),
            "result_chars": len(json.dumps(envelope, ensure_ascii=False, default=str)),
            "evidence": tool_evidence(name, envelope.get("data")),
        })
    return calls


def _records(data) -> list:
    if isinstance(data, dict):
        records = data.get("records")
        if isinstance(records, list):
            return records
        nested = data.get("data")
        if isinstance(nested, dict) and isinstance(nested.get("records"), list):
            return nested["records"]
    return []


def tool_evidence(name: str, data):
    """Keep exactly what the judge must verify, without echoing full bodies into the record."""
    if not isinstance(data, dict):
        return {"data": clip(data, 120)}
    if name in {"campus.query", "campus.sync"}:
        records = _records(data)
        # Some campus capabilities return the full record (the validated college notice includes its
        # body). Record a digest of any body that was actually returned, so the judge can verify the
        # full text really reached the model without storing the text itself.
        bodies = [{"id": item.get("id"), "title": clip(item.get("title"), 200),
                   "published_at": item.get("published_at"), "body_chars": len(item["body"]),
                   "body_sha256": sha256_text(item["body"]),
                   "body_token_present": NOTICE_TOKEN in item["body"],
                   "has_unread_content": bool(item.get("has_unread_content"))}
                  for item in records if isinstance(item.get("body"), str) and item["body"]]
        return {"capability": data.get("source"), "platform": data.get("platform"),
                "record_count": len(records),
                "record_ids": [str(item.get("id") or item.get("code") or "") for item in records[:20]],
                "record_titles": [clip(item.get("title"), 120) for item in records[:10]],
                "bodies": bodies[:5],
                "fetched_at": data.get("fetched_at"), "freshness": data.get("freshness"),
                "completeness": data.get("completeness"),
                "error_code": (data.get("error") or {}).get("code") if isinstance(data.get("error"), dict) else None}
    if name == "campus.notice.read":
        notice = data.get("notice") if isinstance(data.get("notice"), dict) else {}
        body = notice.get("body")
        return {"record_id": notice.get("id"), "title": clip(notice.get("title"), 200),
                "published_at": notice.get("published_at"), "college": notice.get("college"),
                "body_chars": len(body) if isinstance(body, str) else 0,
                "body_sha256": sha256_text(body) if isinstance(body, str) else None,
                "body_token_present": bool(isinstance(body, str) and NOTICE_TOKEN in body),
                "attachments": len(notice.get("attachments") or []),
                "has_unread_content": bool(notice.get("has_unread_content"))}
    if name == "campus.academics":
        agenda = data.get("agenda") if isinstance(data.get("agenda"), dict) else {}
        return {"platform": data.get("platform"),
                "event_count": len(agenda.get("events") or []),
                "has_calendar": bool(agenda.get("calendar")),
                "calendar_precision": clip(agenda.get("calendar_precision"), 120),
                "warnings": [clip(item.get("code") if isinstance(item, dict) else item, 80)
                             for item in (agenda.get("warnings") or [])][:5],
                "grade_terms": len((data.get("grades") or {}).get("terms") or [])
                if isinstance(data.get("grades"), dict) else 0}
    if name == "connections.courses":
        return {"courses": [{"external_id": item.get("external_id") or item.get("id"),
                             "title": clip(item.get("title"), 120), "term": item.get("term")}
                            for item in (data.get("courses") or [])][:10],
                "next_cursor": data.get("next_cursor"),
                "continuations": data.get("continuations"),
                "stats": data.get("stats"),
                "errors": [clip(item, 200) for item in (data.get("errors") or [])][:5]}
    if name == "connections.courses.import":
        return {"courses": [{"id": item.get("id"), "title": clip(item.get("title"), 120),
                             "external_id": item.get("external_id")}
                            for item in (data.get("courses") or [])][:10],
                "next_cursor": data.get("next_cursor"), "stats": data.get("stats"),
                "errors": [clip(item, 200) for item in (data.get("errors") or [])][:5]}
    if name == "connections.sync":
        jobs = data.get("jobs") or []
        return {"jobs": [{"id": item.get("id"), "status": item.get("status"),
                          "platform": item.get("platform"), "reused": item.get("reused")}
                         for item in jobs][:5]}
    if name == "jobs.read":
        job = data.get("job") if isinstance(data.get("job"), dict) else data
        return {"job_id": job.get("id"), "type": job.get("type"), "status": job.get("status"),
                "phase": job.get("phase"), "errors": [clip(item, 160) for item in (job.get("errors") or [])][:5],
                "result_refs": job.get("result_refs")}
    if name == "materials.list":
        return {"items": [{"id": item.get("id"), "title": clip(item.get("title"), 120),
                           "version": item.get("version"), "parse_status": item.get("parse_status"),
                           "chunk_count": item.get("chunk_count")}
                          for item in (data.get("items") or [])][:10],
                "next_cursor": data.get("next_cursor")}
    if name == "materials.read":
        texts = [str(item.get("text") or "") for item in (data.get("items") or [])]
        joined = "\n".join(texts)
        return {"resource": {"id": (data.get("resource") or {}).get("id"),
                             "version": (data.get("resource") or {}).get("version"),
                             "parse_status": (data.get("resource") or {}).get("parse_status"),
                             "chunk_count": (data.get("resource") or {}).get("chunk_count")},
                "chunk_ids": [item.get("id") for item in (data.get("items") or [])][:20],
                "text_chars": len(joined), "text_sha256": sha256_text(joined) if joined else None,
                "token_present": MATERIAL_TOKEN in joined,
                "next_cursor": data.get("next_cursor")}
    if name == "assignments.list":
        return {"items": [{"id": item.get("id"), "title": clip(item.get("title"), 160),
                           "due_at": item.get("due_at"), "due_precision": item.get("due_precision"),
                           "submission_status": item.get("submission_status"),
                           "instructions_chars": len(str(item.get("instructions") or ""))}
                          for item in (data.get("items") or [])][:10],
                "next_cursor": data.get("next_cursor")}
    if name == "tasks.list":
        return {"items": [{"id": item.get("id"), "title": clip(item.get("title"), 120),
                           "estimated_minutes": item.get("estimated_minutes"), "due_at": item.get("due_at"),
                           "due_precision": item.get("due_precision"), "status": item.get("status")}
                          for item in (data.get("items") or [])][:10],
                "next_cursor": data.get("next_cursor")}
    if name == "plans.preview":
        # The tool envelope is {"action": <plan action view>} (tools.py::execute).
        action = data.get("action") if isinstance(data.get("action"), dict) else data
        payload = action.get("preview") if isinstance(action.get("preview"), dict) else {}
        plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
        diff = payload.get("diff") if isinstance(payload.get("diff"), dict) else {}
        return {"action_id": action.get("id"), "action_kind": action.get("kind"),
                "action_status": action.get("status"), "plan_id": action.get("plan_id"),
                "plan_status": plan.get("status"),
                "horizon_start": plan.get("horizon_start"), "horizon_end": plan.get("horizon_end"),
                "input_revision": payload.get("input_revision"),
                "blocks": [{"task_id": item.get("task_id"), "starts_at": item.get("starts_at"),
                            "ends_at": item.get("ends_at"), "locked": item.get("locked")}
                           for item in (plan.get("blocks") or [])][:40],
                "unassigned": [{"task_id": item.get("task_id"), "reason_code": item.get("reason_code")}
                               for item in (plan.get("unassigned") or [])][:20],
                "warnings": [{"code": item.get("code") if isinstance(item, dict) else item,
                              "message": clip(item.get("message"), 160) if isinstance(item, dict) else None}
                             for item in (plan.get("warnings") or [])][:10],
                "diff_added": len(diff.get("added") or []), "diff_removed": len(diff.get("removed") or [])}
    if name in {"plans.apply", "plans.revoke"}:
        receipt = data.get("receipt") if isinstance(data.get("receipt"), dict) else data
        return {"action_id": receipt.get("id"), "kind": receipt.get("kind"),
                "status": receipt.get("status"),
                "result_keys": sorted((receipt.get("result") or {}).keys())
                if isinstance(receipt.get("result"), dict) else []}
    if name in {"campus.status", "campus.webvpn.entry", "campus.imported.read"}:
        # These surfaces can carry private/credential-adjacent material: keep keys only.
        return {"data_keys": sorted(data.keys())[:20]}
    return {"data_keys": sorted(data.keys())[:20]}


# ───────────────────────────────── synthetic school (local adapter + seeds) ─────────────────────────────────

ADAPTER_SOURCE = '''"""Synthetic school transport for ONE isolated verification session (generated at run time).

It is loaded through the supported ``STUDY_SYNC_ADAPTERS`` configuration seam and never opens a socket:
every answer comes from the fixed local fixture embedded below. It is not part of the product.
"""
import json
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))
SCHOOL = json.loads(r"""__SCHOOL_JSON__""")


def _now():
    return datetime.now(TZ).isoformat(timespec="seconds")


class SyntheticSchool:
    def __init__(self, *, platform, connection_id):
        self.platform = platform
        self.connection_id = connection_id

    def check_connection(self):
        return {"state": "ok", "platform": self.platform, "checked_at": _now(),
                "detail": "合成学校连接检查（本地合成传输，未访问学校）"}

    def list_courses(self, cursor=None, term=None):
        pages = SCHOOL["course_pages"]
        index = 0 if not cursor else int(str(cursor).split(":")[-1])
        if index < 0 or index >= len(pages):
            return {"items": [], "next_cursor": None}
        next_cursor = "page:%d" % (index + 1) if index + 1 < len(pages) else None
        return {"items": [dict(item) for item in pages[index]], "next_cursor": next_cursor}

    def sync_course(self, course_id, since_cursor=None, kinds=None):
        observed = _now()
        course = SCHOOL["courses"].get(course_id) or {"resources": [], "assignments": []}
        resources = []
        for item in course.get("resources", []):
            resources.append({
                "kind": item["kind"], "title": item["title"],
                "source_ref": {"platform": self.platform, "external_id": item["external_id"],
                               "canonical_url": None, "observed_at": observed},
                "mime_type": item.get("mime_type"), "size_bytes": item.get("size_bytes"),
                "access_level": item.get("access_level", "full")})
        assignments = []
        for item in course.get("assignments", []):
            entry = {"title": item["title"], "instructions": item.get("instructions"),
                     "due_precision": item.get("due_precision", "unknown"),
                     "submission_status": item.get("submission_status", "unknown"),
                     "source_ref": {"platform": self.platform, "external_id": item["external_id"],
                                    "canonical_url": None, "observed_at": observed}}
            if item.get("due_at"):
                entry["due_at"] = item["due_at"]
            assignments.append(entry)
        return {"platform": self.platform, "connection_id": self.connection_id, "course_id": course_id,
                "mode": "resync" if since_cursor is None else "incremental",
                "cursor": {"next": None, "previous": since_cursor},
                "upserts": {"resources": resources, "assignments": assignments, "recordings": []},
                "deletions": [], "errors": [],
                "stats": {"scanned": len(resources) + len(assignments),
                          "upserted": len(resources) + len(assignments), "deleted": 0, "errors": 0},
                "observed_at": observed}
'''

CAMPUS_SEED_SCRIPT = r'''
import json, os, sys
from pathlib import Path

from app.campus_workspace import CacheStore

home = Path(os.environ["CAMPUS_AGENT_HOME"])
payload = json.loads(os.environ["CAMPUS_M3_CAMPUS_JSON"])
store = CacheStore(home / "campus" / "workspace.sqlite3")
now = payload["fetched_at"]
snapshot = payload.get("snapshot") or {"profile": None, "courses": None, "grades": None}
with store.connect() as db:
    db.execute("INSERT INTO campus_snapshots(owner,snapshot) VALUES(?,?) "
               "ON CONFLICT(owner) DO UPDATE SET snapshot=excluded.snapshot",
               ("local", json.dumps(snapshot, ensure_ascii=False)))
    for row in payload.get("cache", []):
        scope = json.dumps(row["scope"], sort_keys=True, ensure_ascii=False)
        db.execute("INSERT INTO connector_cache(owner,platform,capability,scope,result) VALUES(?,?,?,?,?) "
                   "ON CONFLICT(owner,platform,capability,scope) DO UPDATE SET result=excluded.result",
                   ("local", row["platform"], row["capability"], scope,
                    json.dumps(row["result"], ensure_ascii=False)))
print("campus seeded", len(payload.get("cache", [])))
'''

DOMAIN_SEED_SCRIPT = r'''
import json, os
from datetime import datetime, timedelta, timezone
from sqlalchemy import text
from app.db import Base, engine, SessionLocal
from app import models as m

Base.metadata.create_all(engine)
payload = json.loads(os.environ["CAMPUS_M3_DOMAIN_JSON"])
TZ = timezone(timedelta(hours=8))


def at(value):
    return datetime.fromisoformat(value) if value else None


with SessionLocal() as db:
    # Drop the other tool's placeholder course so this session contains exactly the synthetic school.
    db.execute(text("DELETE FROM courses WHERE id='course_synthetic'"))
    for row in payload.get("courses", []):
        db.add(m.Course(id=row["id"], title=row["title"], term=row.get("term"),
                        teacher=row.get("teacher")))
    for row in payload.get("connections", []):
        db.add(m.Connection(id=row["id"], platform=row["platform"], display_name=row["display_name"],
                            status=row.get("status", "ok"), capabilities=row.get("capabilities")))
    db.flush()
    for row in payload.get("source_refs", []):
        db.add(m.SourceRef(owner_type=row["owner_type"], owner_id=row["owner_id"],
                           platform=row["platform"], external_id=row["external_id"],
                           canonical_url=None, observed_at=datetime.now(timezone.utc), seq=0))
    for row in payload.get("resources", []):
        db.add(m.Resource(id=row["id"], course_id=row["course_id"], kind=row["kind"], title=row["title"],
                          version=row.get("version", 1), content_hash=row.get("content_hash"),
                          parse_status=row.get("parse_status", "pending"),
                          mime_type=row.get("mime_type"), size_bytes=row.get("size_bytes"),
                          access_level=row.get("access_level", "full"), file_id=None))
        db.add(m.ResourceVersion(resource_id=row["id"], version=row.get("version", 1),
                                 content_hash=row.get("content_hash"), mime_type=row.get("mime_type"),
                                 size_bytes=row.get("size_bytes"), file_id=None,
                                 parse_status=row.get("parse_status", "pending")))
        db.flush()
        for index, text_body in enumerate(row.get("chunks", [])):
            db.add(m.ContentChunk(id="chk_" + row["id"] + "_%02d" % index, resource_id=row["id"],
                                  resource_version=row.get("version", 1), ordinal=index, text=text_body,
                                  locator={"kind": "text", "start_offset": 0, "end_offset": len(text_body)},
                                  provenance={"kind": "synthetic"}))
    for row in payload.get("learning_tasks", []):
        db.add(m.LearningTask(id=row["id"], course_id=row.get("course_id"), type=row["type"],
                              title=row["title"], estimated_minutes=row["estimated_minutes"],
                              due_at=at(row.get("due_at")), due_precision=row.get("due_precision", "unknown"),
                              status=row.get("status", "pending"), fact_source="user"))
    availability = payload.get("availability")
    if availability:
        db.add(m.Availability(id=1, timezone=availability.get("timezone", "Asia/Shanghai"),
                              weekly_windows=availability.get("weekly_windows", []),
                              date_exceptions=[], daily_limit_minutes=None))
    db.commit()
print("domain seeded")
'''


def local_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def calendar_fixture(today: date) -> dict:
    """A verified calendar anchored so today sits in week 4 of the 秋 season."""
    autumn_start = monday_of(today) - timedelta(days=21)
    autumn_end = autumn_start + timedelta(days=55)
    winter_start = autumn_end + timedelta(days=1)
    winter_end = winter_start + timedelta(days=55)
    return {"verified": True, "stale": False, "fetched_at": now_iso(),
            "seasons": [{"name": "秋", "start": autumn_start.isoformat(), "end": autumn_end.isoformat()},
                        {"name": "冬", "start": winter_start.isoformat(), "end": winter_end.isoformat()}],
            "period_times": [list(slot) for slot in CALENDAR_PERIOD_TIMES],
            "holidays": [], "exchanges": {}, "uncertain_dates": [], "valid_until": winter_end.isoformat(),
            "sources": [{"url": "https://zdbk.zju.edu.cn/jwglxt/xtgl/login_slogin.html", "sha256": "a" * 64}],
            "scope": "合成校历（本地固定夹具，未访问学校）", "error": None}


def timetable_rows(fetched_at: str) -> list:
    rows = []
    for weekday in T3_CLASS_WEEKDAYS:
        rows.append({"name": T3_COURSE_TITLE + "（周" + weekday + "）", "code": "SYN-" + weekday,
                     "location": "合成楼" + weekday + "-101", "weekday": weekday,
                     "periods": T3_CLASS_PERIODS[0], "start_period": "", "duration": "",
                     "weeks": T3_CLASS_WEEKS, "season": T3_CLASS_SEASON, "odd_even": "",
                     "year": T3_CAMPUS_YEAR, "semester": T3_CAMPUS_SEMESTER, "term": T3_CLASS_TERM,
                     "stale": False, "fetched_at": fetched_at})
    # One deliberately ambiguous row: it cannot be located in time, so the preview must warn about it
    # instead of inventing an occupied slot.
    rows.append({"name": T3_AMBIGUOUS_TITLE, "code": "SYN-?", "location": "合成楼-待定", "weekday": "6",
                 "periods": "", "start_period": "", "duration": "", "weeks": "", "season": "",
                 "odd_even": "", "year": T3_CAMPUS_YEAR, "semester": T3_CAMPUS_SEMESTER,
                 "term": T3_CLASS_TERM, "stale": False, "fetched_at": fetched_at})
    return rows


def domain_payload(task: str) -> dict:
    """Rows the isolated domain DB is seeded with for one task."""
    payload = {"courses": [], "connections": [], "source_refs": [], "resources": [],
               "learning_tasks": [], "availability": None}
    if task == "T2":
        payload["courses"] = [{"id": TARGET_COURSE_ID, "title": TARGET_COURSE_TITLE,
                               "term": TARGET_COURSE_TERM, "teacher": "合成教师丙"}]
        payload["connections"] = [{"id": CONNECTION_ID, "platform": PLATFORM,
                                   "display_name": "学在浙大（合成）", "status": "ok",
                                   "capabilities": {"courses": True, "coursewares": True, "homework": True}}]
        payload["source_refs"] = [
            {"owner_type": "course", "owner_id": TARGET_COURSE_ID, "platform": PLATFORM,
             "external_id": TARGET_COURSE_EXTERNAL_ID},
            {"owner_type": "resource", "owner_id": MATERIAL_RESOURCE_ID, "platform": PLATFORM,
             "external_id": MATERIAL_EXTERNAL_ID},
            {"owner_type": "resource", "owner_id": PENDING_RESOURCE_ID, "platform": PLATFORM,
             "external_id": PENDING_EXTERNAL_ID}]
        payload["resources"] = [
            {"id": MATERIAL_RESOURCE_ID, "course_id": TARGET_COURSE_ID, "kind": "document",
             "title": MATERIAL_TITLE, "version": MATERIAL_VERSION, "content_hash": None,
             "parse_status": "ready", "mime_type": "text/markdown",
             "size_bytes": sum(len(chunk.encode()) for chunk in MATERIAL_CHUNKS),
             "access_level": "full", "chunks": MATERIAL_CHUNKS},
            {"id": PENDING_RESOURCE_ID, "course_id": TARGET_COURSE_ID, "kind": "document",
             "title": PENDING_RESOURCE_TITLE, "version": 1, "content_hash": None,
             "parse_status": "pending", "mime_type": "application/pdf", "size_bytes": 4096,
             "access_level": "full", "chunks": []}]
    elif task == "T3":
        today = local_now().date()
        payload["courses"] = [{"id": T3_COURSE_ID, "title": T3_COURSE_TITLE, "term": T3_CLASS_TERM,
                               "teacher": "合成教师丁"}]
        tasks = []
        for row in T3_LEARNING_TASKS:
            due = None
            if row.get("due_offset_days") is not None:
                due = datetime.combine(today + timedelta(days=row["due_offset_days"]),
                                       datetime.strptime(row["due_time"], "%H:%M").time(),
                                       timezone(timedelta(hours=8))).isoformat()
            tasks.append({"id": row["id"], "course_id": T3_COURSE_ID, "type": row["type"],
                          "title": row["title"], "estimated_minutes": row["estimated_minutes"],
                          "due_at": due, "due_precision": "minute" if due else "unknown",
                          "status": "pending"})
        payload["learning_tasks"] = tasks
        payload["availability"] = {"timezone": "Asia/Shanghai",
                                  "weekly_windows": [{"weekday": weekday, "start": "07:00", "end": "23:00"}
                                                     for weekday in range(1, 8)]}
    elif task == "T4":
        payload["connections"] = [{"id": CONNECTION_ID, "platform": PLATFORM,
                                   "display_name": "学在浙大（合成）", "status": "ok",
                                   "capabilities": {"courses": True, "coursewares": True, "homework": True}}]
    return payload


def campus_payload(task: str) -> dict:
    """Campus cache rows. The school is never contacted: these are cache reads."""
    stamp = now_iso()
    payload = {"fetched_at": stamp, "snapshot": {"profile": None, "courses": None, "grades": None}, "cache": []}
    profile = {"data": {"student_id": "SYN0001", "name": "合成学生", "college": NOTICE_COLLEGE,
                        "major": "合成专业", "class_name": "合成班", "year": "2026"},
               "field_sources": {key: {"source": "ETA 合成夹具", "fetched_at": stamp, "stale": False}
                                 for key in ("student_id", "name", "college", "major", "class_name", "year")},
               "fetched_at": stamp, "stale": False}
    if task == "T1":
        rows = [{"id": NOTICE_ID, "college": NOTICE_COLLEGE, "title": NOTICE_TITLE,
                 "published_at": NOTICE_DATE, "body": NOTICE_BODY, "body_sha256": sha256_text(NOTICE_BODY),
                 "attachments": [], "has_unread_content": False, "source": NOTICE_SOURCE,
                 "fetched_at": stamp, "stale": False}]
        payload["snapshot"] = {"profile": profile, "courses": None, "grades": None}
        payload["cache"] = [
            {"platform": "ckc_public", "capability": "college_notices",
             "scope": {"college": NOTICE_COLLEGE, "limit": 3},
             "result": {"records": rows, "source": "college_notices", "platform": "ckc_public",
                        "fetched_at": stamp, "last_attempt_at": stamp, "freshness": "fresh",
                        "completeness": "partial", "next_cursor": None,
                        "warnings": [{"code": "RECENT_COLLEGE_NOTICES", "message": "仅学院通知首页中的有限条目"}],
                        "error": None,
                        "selection": {"college": NOTICE_COLLEGE, "status": "matched",
                                      "profile_fetched_at": stamp, "profile_stale": False}}},
            {"platform": "zdbk_public", "capability": "notices",
             "scope": {"limit": 6},
             "result": {"records": [
                 {"id": item["id"], "title": item["title"], "published_at": item["published_at"],
                  "source": "https://zdbk.zju.edu.cn/jwglxt/xtgl/xwck_ckLoginNews.html?"
                            "xwbh=" + item["id"] + "&doType=save",
                  "fetched_at": stamp, "stale": False} for item in BOARD_NOTICES],
                 "source": "notices", "platform": "zdbk_public", "fetched_at": stamp,
                 "last_attempt_at": stamp, "freshness": "fresh", "completeness": "partial",
                 "next_cursor": None,
                 "warnings": [{"code": "LIMITED_SCOPE", "message": "仅公开登录页近期/置顶通知摘要；未读取正文"}],
                 "error": None}}]
    elif task == "T3":
        today = local_now().date()
        calendar = calendar_fixture(today)
        payload["snapshot"] = {"profile": None, "grades": None,
                               "courses": {"year": T3_CAMPUS_YEAR, "semester": T3_CAMPUS_SEMESTER,
                                           "term": T3_CLASS_TERM, "stale": False, "complete": True,
                                           "fetched_at": stamp,
                                           "terms": [{"year": T3_CAMPUS_YEAR,
                                                      "semester": T3_CAMPUS_SEMESTER,
                                                      "term": T3_CLASS_TERM, "stale": False,
                                                      "fetched_at": stamp,
                                                      "data": timetable_rows(stamp)}]},
                               "calendars": {T3_CAMPUS_TERM_KEY: calendar}}
    return payload


def school_payload(task: str) -> dict:
    """The synthetic platform transport's fixed data."""
    school = {"course_pages": [], "courses": {}}
    if task in {"T2", "T4"}:
        school["course_pages"] = PLATFORM_PAGES
        known_due = (local_now() + timedelta(days=10)).replace(hour=23, minute=59, second=0,
                                                              microsecond=0).isoformat()
        school["courses"] = {
            TARGET_COURSE_ID: {
                "resources": [{"kind": "document", "title": MATERIAL_TITLE,
                               "external_id": MATERIAL_EXTERNAL_ID, "mime_type": "text/markdown",
                               "size_bytes": 2048, "access_level": "full"},
                              {"kind": "document", "title": PENDING_RESOURCE_TITLE,
                               "external_id": PENDING_EXTERNAL_ID, "mime_type": "application/pdf",
                               "size_bytes": 4096, "access_level": "full"}],
                "assignments": [
                    {"title": UNKNOWN_ASSIGNMENT_TITLE, "external_id": "xzzd-asg-unknown",
                     "instructions": UNKNOWN_ASSIGNMENT_INSTRUCTIONS, "due_precision": "unknown",
                     "submission_status": "unknown"},
                    {"title": KNOWN_ASSIGNMENT_TITLE, "external_id": "xzzd-asg-known",
                     "instructions": KNOWN_ASSIGNMENT_INSTRUCTIONS, "due_at": known_due,
                     "due_precision": "minute", "submission_status": "unknown"}]}}
    return school


def write_adapter_module(home: Path, task: str) -> str:
    target = home / "campus_readonly_synthetic.py"
    # Embedded verbatim inside the module's raw triple-quoted literal: json.dumps never emits three
    # consecutive unescaped quotes, and the fixture contains no backslash-terminated tail.
    source = ADAPTER_SOURCE.replace("__SCHOOL_JSON__", json.dumps(school_payload(task), ensure_ascii=False))
    target.write_text(source, encoding="utf-8")
    return "campus_readonly_synthetic:SyntheticSchool"


def run_seed(home: Path, script_name: str, script: str, env: dict, name: str) -> None:
    path = home / script_name
    path.write_text(script, encoding="utf-8")
    code = live.run_logged([sys.executable, str(path)], API, env, home / "logs" / (name + ".log"), 120)
    if code != 0:
        raise RuntimeError(f"{name} seed failed: " + live.read_log(home / "logs" / (name + ".log")))


def prepare_session_home(home: Path, provider: dict, task: str) -> dict:
    """live.prepare_home's isolation + provider seeding, then this tool's synthetic school."""
    prepared = live.prepare_home(home, provider, "t1")
    env = prepared["env"]
    env["STUDY_SYNC_ADAPTERS"] = json.dumps({PLATFORM: write_adapter_module(home, task)}, ensure_ascii=False)
    env["PYTHONPATH"] = os.pathsep.join([str(home), str(ROOT), str(API)])
    env["CAMPUS_RUNNER_STDERR"] = str(home / "logs" / "runner-stderr.log")
    env["CAMPUS_M3_DOMAIN_JSON"] = json.dumps(domain_payload(task), ensure_ascii=False)
    env["CAMPUS_M3_CAMPUS_JSON"] = json.dumps(campus_payload(task), ensure_ascii=False)
    run_seed(home, "seed_domain.py", DOMAIN_SEED_SCRIPT, env, "seed-domain")
    run_seed(home, "seed_campus.py", CAMPUS_SEED_SCRIPT, env, "seed-campus")
    return prepared


# ───────────────────────────────── session driver ─────────────────────────────────

def turn_budget(limits: dict, duration: float) -> dict:
    measured = {"model_requests": limits.get("model_calls"), "tool_calls": limits.get("tool_calls"),
                "seconds": round(duration, 3)}
    exceeded = {}
    for key, value in measured.items():
        exceeded[key] = None if value is None else bool(value > BUDGET[key])
    return {"limits": dict(BUDGET), "measured": measured, "exceeded": exceeded,
            "source": "journal.sqlite turns.model_calls / tool_calls+native_tool_leases (server-enforced too)"}


def native_slice(prior: list, current: list) -> list:
    if len(current) >= len(prior):
        return current[len(prior):]
    return current


def native_sequence(events: list) -> list:
    """The turn's real order: request headers, tool calls/results and model responses as recorded."""
    sequence = []
    for event in events:
        kind, data = event.get("type"), event.get("data") or {}
        if kind == "request/header":
            header = data.get("header") if isinstance(data.get("header"), dict) else {}
            tools = header.get("tools") if isinstance(header.get("tools"), list) else []
            sequence.append({"kind": "model_request", "visible_tools": len(tools)})
        elif kind == "assistant/message":
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            sequence.append({"kind": "model_response", "usage": usage})
        elif kind == "tool/call":
            sequence.append({"kind": "tool_call", "call_id": str(data.get("callId")), "name": data.get("name")})
        elif kind == "tool/result":
            message = data.get("message") or {}
            sequence.append({"kind": "tool_result", "call_id": str(message.get("toolCallId")),
                             "is_error": bool(message.get("isError")), "error": data.get("error")})
    return sequence


def run_one_turn(api, home: Path, run_id: str, conversation_id: str, prompt: str, index: int,
                 prior_native: list) -> tuple[dict, list]:
    started = time.monotonic()
    posted = api.post(f"/api/v1/agent/conversations/{conversation_id}/turns",
                      json={"text": prompt, "attachment_ids": []},
                      headers={"Idempotency-Key": f"campus-m3-{run_id}-turn-{index}"}, timeout=30)
    if not posted.ok:
        raise RuntimeError(f"turn create failed: {posted.status_code} {posted.text[:300]}")
    turn_id = posted.json()["id"]
    view, events, exhausted = live.poll_turn(api, turn_id, started + BUDGET["seconds"])
    duration = time.monotonic() - started
    limits = live.journal_facts(home / "agent", turn_id)
    _, native_events = live.find_session_jsonl(home / "agent", conversation_id)
    fresh = native_slice(prior_native, native_events)
    usage = live.usage_facts(fresh)
    tool_calls = extract_tool_calls(events)
    native_calls = [{"position": item["position"], "call_id": item["call_id"], "name": item["name"],
                     "is_error": bool((item.get("result") or {}).get("is_error"))}
                    for item in live.native_tool_trace(fresh)]
    turn = {
        "index": index, "turn_id": turn_id, "prompt": prompt,
        "status": view.get("status"), "error": view.get("error"),
        "duration_seconds": round(duration, 3),
        "final_answer": view.get("response") or "",
        "answer_chars": len(view.get("response") or ""),
        "budget": turn_budget(limits, duration),
        "driver_cancelled_at_deadline": bool(exhausted),
        "tool_calls": tool_calls,
        "native_tool_calls": native_calls,
        "native_sequence": native_sequence(fresh),
        "usage": {"totals": usage["totals"], "model_responses_with_usage": usage["model_responses_with_usage"],
                  "per_call": usage["per_call"]},
        "api_event_kinds": {},
    }
    for event in events:
        kind = event.get("kind")
        turn["api_event_kinds"][kind] = turn["api_event_kinds"].get(kind, 0) + 1
    turn["raw_api_events"] = persist_raw_events(home, f"api-events-turn{index}", events)
    return turn, native_events


def persist_raw_events(home: Path, name: str, events: list) -> str:
    """Keep the turn's raw API journal in the isolated home for the independent verifier (redacted)."""
    counter = {"redactions": 0}
    path = home / (name + ".json")
    live.write_json(path, redact_tree(events, counter))
    return str(path.relative_to(ROOT).as_posix()) + (f" (redactions={counter['redactions']})"
                                                     if counter["redactions"] else "")


def collect_turn(api, home: Path, conversation_id: str, turn_id: str, prior_native: list) -> tuple[dict, list]:
    """Collect a turn the SERVER enqueued (trusted automatic recovery continuation)."""
    started = time.monotonic()
    view, events, exhausted = live.poll_turn(api, turn_id, started + BUDGET["seconds"])
    duration = time.monotonic() - started
    limits = live.journal_facts(home / "agent", turn_id)
    _, native_events = live.find_session_jsonl(home / "agent", conversation_id)
    fresh = native_slice(prior_native, native_events)
    usage = live.usage_facts(fresh)
    turn = {"index": None, "turn_id": turn_id, "prompt": None, "server_enqueued": True,
            "status": view.get("status"), "error": view.get("error"),
            "duration_seconds": round(duration, 3),
            "final_answer": view.get("response") or "", "answer_chars": len(view.get("response") or ""),
            "budget": turn_budget(limits, duration), "driver_cancelled_at_deadline": bool(exhausted),
            "tool_calls": extract_tool_calls(events),
            "native_tool_calls": [{"position": item["position"], "call_id": item["call_id"],
                                   "name": item["name"], "is_error": bool((item.get("result") or {}).get("is_error"))}
                                  for item in live.native_tool_trace(fresh)],
            "native_sequence": native_sequence(fresh),
            "usage": {"totals": usage["totals"],
                      "model_responses_with_usage": usage["model_responses_with_usage"],
                      "per_call": usage["per_call"]},
            "api_event_kinds": {}}
    for event in events:
        kind = event.get("kind")
        turn["api_event_kinds"][kind] = turn["api_event_kinds"].get(kind, 0) + 1
    turn["raw_api_events"] = persist_raw_events(home, f"api-events-{turn_id}", events)
    return turn, native_events


def wait_for_server_turn(api, conversation_id: str, known_ids: set, seconds: float) -> str | None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            state = conversation_scope(api, conversation_id)
        except Exception:  # noqa: BLE001 - a transient read must not invent a continuation
            time.sleep(0.5)
            continue
        for turn_id in state["turn_ids"]:
            if turn_id not in known_ids:
                return turn_id
        time.sleep(0.5)
    return None


def persist_native_session(home: Path, conversation_id: str) -> str | None:
    """Copy this conversation's Harness JSONL into the isolated home for audit."""
    path, events = live.find_session_jsonl(home / "agent", conversation_id, retries=2)
    if not events:
        return None
    target = home / "harness-session.jsonl"
    target.write_text("\n".join(json.dumps(item, ensure_ascii=False, default=str) for item in events),
                      encoding="utf-8")
    return str(target.relative_to(ROOT).as_posix())


def domain_facts(db_path: Path, task: str) -> dict:
    """Post-session domain rows: what the real worker/repository really wrote."""
    if not db_path.is_file():
        return {"available": False}
    facts = {"available": True,
             "jobs": db_rows(db_path, "select id,type,status,phase,errors,result_refs,payload from jobs "
                                      "order by created_at"),
             "resources": db_rows(db_path, "select r.id,r.course_id,r.title,r.version,r.parse_status,"
                                           "(select count(*) from content_chunks c where c.resource_id=r.id "
                                           "and c.resource_version=r.version) as chunk_count "
                                           "from resources r order by r.id"),
             "assignments": db_rows(db_path, "select id,course_id,title,due_at,due_precision,"
                                             "submission_status,length(coalesce(instructions,'')) as instructions_chars "
                                             "from assignments order by id"),
             "learning_tasks": db_rows(db_path, "select id,course_id,title,estimated_minutes,due_at,"
                                                "due_precision,status from learning_tasks order by id"),
             "plans": db_rows(db_path, "select id,version,status,horizon_start,horizon_end from plans "
                                       "order by version"),
             "plan_actions": db_rows(db_path, "select id,kind,status,expires_at from agent_plan_actions "
                                              "order by created_at"),
             "plan_blocks": db_rows(db_path, "select plan_id,task_id,starts_at,ends_at,locked from plan_blocks "
                                             "order by plan_id,starts_at"),
             "plan_warnings": db_rows(db_path, "select plan_id,code,message from plan_warnings order by plan_id")}
    if task == "T2":
        facts["connections"] = db_rows(db_path, "select id,platform,status,last_error from connections")
    return facts


def campus_facts(home: Path, task: str) -> dict:
    """The synthetic school's own cache state (proof that the only notice/课表 source was local)."""
    path = home / "agent" / "campus" / "workspace.sqlite3"
    facts = {"cache_db": str(path.relative_to(ROOT).as_posix()) if path.exists() else None}
    if path.is_file():
        try:
            facts["connector_cache"] = db_rows(path, "select platform,capability,scope,length(result) as result_chars "
                                                     "from connector_cache order by platform,capability")
            facts["snapshot_keys"] = [row["json_key"] for row in db_rows(
                path, "select json_extract(snapshot,'$.profile') is not null as has_profile,"
                      "'snapshot' as json_key from campus_snapshots")]
            facts["browser_grants"] = db_rows(path, "select owner,scope,length(grant_id) as grant_id_chars "
                                                    "from campus_browser_grants")
        except sqlite3.Error as exc:
            facts["error"] = f"{type(exc).__name__}: {exc}"
    facts["synthetic_transport"] = {
        "module": "campus_readonly_synthetic:SyntheticSchool",
        "file": str((home / "campus_readonly_synthetic.py").relative_to(ROOT).as_posix()),
        "sha256": live.sha256_file(home / "campus_readonly_synthetic.py")
        if (home / "campus_readonly_synthetic.py").is_file() else None,
        "note": "registered through STUDY_SYNC_ADAPTERS; every answer is the fixed local fixture",
    }
    return facts


def judge_session(record: dict) -> dict:
    task = record["task"]
    if task == "T1":
        criteria = judge_t1(record)
    elif task == "T2":
        criteria = judge_t2(record)
    elif task == "T3":
        criteria = judge_t3(record)
    else:
        criteria = judge_t4(record)
    achieved = all(bool(item.get("passed")) for item in criteria.values())
    return {"achieved": achieved, "criteria": criteria, "judge_version": JUDGE_VERSION,
            "failed_criteria": sorted(name for name, item in criteria.items() if not item.get("passed")),
            "note": ("judge pass means the recorded trajectory satisfied these checks; four sessions is a "
                     "small diagnostic sample, not a success rate")}


def execute_session(task: str, index: int, scratch: Path, provider: dict) -> dict:
    run_id = f"{task.lower()}-{index}-{uuid.uuid4().hex[:8]}"
    home = scratch / "runs" / run_id
    spec = TASKS[task]
    record = {
        "run_id": run_id, "task": task, "session_index": index, "task_title": spec["title"],
        "mode": "real-official-model/synthetic-school-readonly",
        "provider": {"id": provider["id"], "name": provider["name"], "model": provider["model"],
                     "protocol": provider["protocol"],
                     "base_url_host": provider["base_url"].split("/")[2],
                     "base_url_scheme": provider["base_url"].split(":")[0]},
        "started_at": now_iso(), "finished_at": None, "status": "running",
        "scratch_home": str(home.relative_to(ROOT).as_posix()),
        "budget": dict(BUDGET), "max_recovery_turns": MAX_RECOVERY_TURNS,
        "cost": COST, "cost_basis": COST_BASIS,
        "human_interactions": [], "human_interaction_count": 0, "human_wait_seconds": 0.0,
        "driver_error": None, "goal": {"achieved": False, "criteria": {}},
        "notes": list(NOTES_UNCONTROLLED), "redactions": 0,
    }
    if task == "T4":
        record["notes"] = list(NOTES_UNCONTROLLED) + [
            "T4: the automatic continuation is produced by the server's own trusted probe; when the probe "
            "cannot be confirmed offline the record says so and the verdict is not a pass"]
    api = live.Api(home, {}, "")
    started = time.monotonic()
    try:
        prepared = prepare_session_home(home, provider, task)
        api = live.Api(home, prepared["env"], prepared["base"])
        record["isolation"] = {
            "session_home": str(home.relative_to(ROOT).as_posix()),
            "workspace": str((home / "workspace").relative_to(ROOT).as_posix()),
            "domain_db": str((home / "domain.sqlite").relative_to(ROOT).as_posix()),
            "files_dir": str((home / "files").relative_to(ROOT).as_posix()),
            "harness_home": str((home / "agent").relative_to(ROOT).as_posix()),
            "campus_dir": str((home / "campus").relative_to(ROOT).as_posix()),
            "secrets": "isolated dir holding only a copy of the DPAPI ciphertext; deleted after the session",
            "personal_db": "read-only provider row; never written or copied",
            "forbidden_writes": ["var/personal.db", "var/files", "var/secrets", "var/agent", "var/workspace"],
            "school_contacted": False,
        }
        api.start()
        if task == "T2":
            api.start_worker()
        created = api.post("/api/v1/agent/conversations", json={"provider_id": provider["id"]}, timeout=30)
        if not created.ok:
            raise RuntimeError(f"conversation create failed: {created.status_code} {created.text[:300]}")
        conversation_id = created.json()["id"]
        record["conversation_id"] = conversation_id
        scope = conversation_scope(api, conversation_id)
        record["conversation_scope"] = scope
        record["conversation_settings"] = journal_conversation_settings(home / "agent", conversation_id)

        turns, native = [], []
        if task == "T4":
            turns, native, t4 = drive_t4(api, home, run_id, conversation_id, record)
            record["auth_recovery"] = t4
        else:
            turn, native = run_one_turn(api, home, run_id, conversation_id, spec["prompt"], 1, native)
            turns.append(turn)

        record["turns"] = turns
        record["trajectory"] = merge_trajectory(turns)
        record["final_answer"] = turns[-1]["final_answer"] if turns else ""
        record["answer_chars"] = len(record["final_answer"])
        record["duration_seconds"] = round(time.monotonic() - started, 3)
        record["limits"] = session_limits(turns)
        record["usage"] = session_usage(turns)
        record["evidence"] = {
            "raw_api_events": [turn.get("raw_api_events") for turn in turns if turn.get("raw_api_events")],
            "harness_session_copy": persist_native_session(home, conversation_id),
            "api_log": str((home / "logs" / "api.log").relative_to(ROOT).as_posix()),
            "worker_log": str((home / "logs" / "worker.log").relative_to(ROOT).as_posix()),
            "runner_stderr": str((home / "logs" / "runner-stderr.log").relative_to(ROOT).as_posix()),
            "note": ("raw journals stay in this session's isolated home; the JSON record itself is redacted "
                     "and carries no cookies/tickets/signed URLs"),
        }
        record["domain_evidence"] = domain_facts(home / "domain.sqlite", task)
        record["campus_evidence"] = campus_facts(home, task)
        record["goal"] = judge_session(record)
        record["status"] = "completed" if all((turn.get("status") == "succeeded") for turn in turns) \
            else "turn_failed"
        if record["limits"]["exceeded_any"]:
            record["status"] = "budget_exceeded"
        return record
    except Exception as exc:  # noqa: BLE001 - a driver fault is recorded, never hidden
        record["driver_error"] = f"{type(exc).__name__}: {exc}"
        record["status"] = "driver_error"
        record["logs"] = {"api": live.read_log(home / "logs" / "api.log", 4000),
                          "worker": live.read_log(home / "logs" / "worker.log", 4000),
                          "runner": live.read_log(home / "logs" / "runner-stderr.log", 4000)}
        return record
    finally:
        try:
            api.stop()
        finally:
            live.remove_secret_copy(home)
            record["finished_at"] = now_iso()
            record["duration_seconds"] = round(time.monotonic() - started, 3)
            if record.get("status") == "running":
                record["status"] = "incomplete"
            if not record.get("goal", {}).get("criteria"):
                record["goal"] = judge_session(record)


def drive_t4(api, home: Path, run_id: str, conversation_id: str, record: dict) -> tuple[list, list, dict]:
    """Authentication-expiry continuation: at most two separately metered turns."""
    spec = TASKS["T4"]
    turns, native = [], []
    recorded_turns = set(conversation_scope(api, conversation_id)["turn_ids"])
    turn1, native = run_one_turn(api, home, run_id, conversation_id, spec["prompt"], 1, native)
    turns.append(turn1)
    recorded_turns.add(turn1["turn_id"])

    recovery_api = api.get(f"/api/v1/agent/conversations/{conversation_id}/recovery", timeout=30).json()
    checkpoints = journal_auth_recovery(home / "agent")
    recovery = {"api_view_before_notification": recovery_api, "checkpoints_before_notification": checkpoints,
                "conversation_scope_before": record.get("conversation_scope")}

    # Human waiting time is metered separately from model time: it starts when the auth-blocked turn
    # settled and ends when the trusted notification is delivered. This is a SIMULATED human (the driver
    # performs the notification immediately), so the value is small and honest.
    wait_started = time.monotonic()
    notification = api.post(f"/api/v1/connections/{CONNECTION_ID}/check", timeout=60)
    recovery["human_wait_seconds"] = round(time.monotonic() - wait_started, 3)
    recovery["trusted_entry"] = {
        "endpoint": f"POST /api/v1/connections/{CONNECTION_ID}/check",
        "why": ("the app's own trusted entry: a validated successful connection check commits status=ok and "
                "calls get_runtime().resume_authenticated_tasks() (routers/connections.py:"
                "_resume_after_connected_check). POST /api/v1/agent/campus/connect-browser is the other "
                "trusted entry, but it requires an owned logged-in browser runtime (real school cookies), "
                "which this synthetic run must not create, so it is not exercised."),
        "status_code": notification.status_code,
        "ok": bool(notification.ok),
        "body": clip(notification.text, 600),
    }
    record["human_interactions"].append({"kind": "trusted_human_auth_notification",
                                         "endpoint": recovery["trusted_entry"]["endpoint"],
                                         "status_code": notification.status_code,
                                         "at": now_iso()})
    record["human_wait_seconds"] = recovery["human_wait_seconds"]

    # The server-owned monitor probes once after the notification; a successful probe enqueues the
    # continuation turn itself. Wait for it inside the run's own envelope.
    continuation_id = wait_for_server_turn(api, conversation_id, recorded_turns, 45.0)
    recovery["continuation_turn_id"] = continuation_id
    recovery["checkpoints_after_notification"] = journal_auth_recovery(home / "agent")
    recovery["api_view_after_notification"] = api.get(
        f"/api/v1/agent/conversations/{conversation_id}/recovery", timeout=30).json()
    probe = next((item for item in recovery["checkpoints_after_notification"]
                  if item.get("status") or item.get("error")), None)
    recovery["probe_outcome"] = {
        "status": (probe or {}).get("status"),
        "error_code": ((probe or {}).get("error") or {}).get("code"),
        "probe_result_present": (probe or {}).get("probe_result_present"),
        "continuation_turn_id": (probe or {}).get("continuation_turn_id"),
        "automatic_continuation": continuation_id is not None,
    }

    if continuation_id is not None:
        recovery["continuation_mode"] = "trusted_auto_resume"
        turn2, native = collect_turn(api, home, conversation_id, continuation_id, native)
        turn2["index"] = 2
        turns.append(turn2)
        recorded_turns.add(continuation_id)
    else:
        recovery["continuation_mode"] = "manual_after_probe_failure"
        turn2, native = run_one_turn(api, home, run_id, conversation_id, T4_RESUME_PROMPT, 2, native)
        turns.append(turn2)
        recorded_turns.add(turn2["turn_id"])
        record["human_interactions"].append({"kind": "manual_continuation_request",
                                             "endpoint": f"POST /api/v1/agent/conversations/{conversation_id}/turns",
                                             "turn_id": turn2["turn_id"], "at": now_iso()})
    recovery["conversation_scope_after"] = conversation_scope(api, conversation_id)
    record["human_interaction_count"] = len(record["human_interactions"])
    return turns, native, recovery


def merge_trajectory(turns: list) -> list:
    """One ordered list per session.

    The Harness session JSONL preserves the true order (model request -> tool call -> result -> next
    request); the API journal supplies each domain tool call's scrubbed arguments, returned status and
    compacted evidence, matched by call id. When the JSONL is unavailable the API order is used alone.
    """
    trajectory = []
    for turn in turns:
        index = turn.get("index")
        api_calls = {call.get("call_id"): call for call in turn.get("tool_calls") or []}
        sequence = turn.get("native_sequence") or []
        if not sequence:
            for position, call in enumerate(turn.get("tool_calls") or [], 1):
                trajectory.append({"turn": index, "kind": "tool_call", "position": position,
                                   "call_id": call.get("call_id"), "name": call.get("name"),
                                   "arguments": call.get("arguments"), "status": call.get("status"),
                                   "error": call.get("error"), "evidence": call.get("evidence")})
            continue
        for position, item in enumerate(sequence, 1):
            entry = {"turn": index, "position": position, "kind": item.get("kind")}
            if item.get("kind") in {"tool_call", "tool_result"}:
                call = api_calls.get(item.get("call_id")) or {}
                entry.update({"call_id": item.get("call_id"), "name": item.get("name") or call.get("name"),
                              "arguments": call.get("arguments"), "status": call.get("status"),
                              "error": call.get("error") or item.get("error"),
                              "is_error": item.get("is_error"), "evidence": call.get("evidence")})
            elif item.get("kind") == "model_response":
                entry["usage"] = item.get("usage")
            elif item.get("kind") == "model_request":
                entry["visible_tools"] = item.get("visible_tools")
            trajectory.append(entry)
    return trajectory


def session_limits(turns: list) -> dict:
    model_total = sum(int((turn["budget"]["measured"].get("model_requests") or 0)) for turn in turns)
    tool_total = sum(int((turn["budget"]["measured"].get("tool_calls") or 0)) for turn in turns)
    seconds = round(sum(float(turn.get("duration_seconds") or 0) for turn in turns), 3)
    exceeded = {key: any(bool((turn["budget"]["exceeded"] or {}).get(key)) for turn in turns)
                for key in BUDGET}
    return {"model_requests": model_total, "tool_calls": tool_total, "seconds": seconds,
            "per_turn": [{"turn": turn.get("index"), "turn_id": turn.get("turn_id"),
                          "measured": turn["budget"]["measured"], "exceeded": turn["budget"]["exceeded"]}
                         for turn in turns],
            "exceeded": exceeded, "exceeded_any": any(value for value in exceeded.values()),
            "note": ("the API process also enforces CAMPUS_MAX_MODEL_CALLS/CAMPUS_MAX_TOOL_CALLS/"
                     "CAMPUS_TURN_TIMEOUT_SECONDS at these same values, so a turn that runs out fails with "
                     "BUDGET_EXCEEDED instead of being silently truncated")}


def session_usage(turns: list) -> dict:
    keys = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "totalTokens")
    totals = {key: 0 for key in keys}
    for turn in turns:
        for key in keys:
            totals[key] += int((turn.get("usage", {}).get("totals") or {}).get(key) or 0)
    return {"totals": totals, "per_turn": [{"turn": turn.get("index"), "totals": turn.get("usage", {}).get("totals"),
                                            "model_responses_with_usage":
                                                turn.get("usage", {}).get("model_responses_with_usage")}
                                           for turn in turns],
            "note": "provider-reported token usage of the main agent; no price basis, so cost stays unknown"}


# ───────────────────────────────── deterministic judge (v1) ─────────────────────────────────

WHOLE_SCHOOL_CLAIMS = ("全校", "所有学院", "全部学院", "面向全体学生", "覆盖全部学院")
# Clause boundaries used by the whole-school-claim check: the claim is affirmative only when
# its own clause does not negate/limit it.
_CLAUSE_BREAK = re.compile(r"[。！？!?；;\n，,、：:]+")
_MARKDOWN_EMPHASIS = re.compile(r"[*_`~]+")
TERMINAL_JOB_STATES = frozenset({"succeeded", "partial_failed", "failed", "cancelled"})
NEGATION_MARKERS = ("不", "未", "非", "无", "仅", "只", "没", "并非", "不代表", "不能", "勿")
SUMMARY_QUALIFIERS = ("摘要", "仅标题", "只看到标题", "标题", "未见正文", "未读正文", "未获取正文", "列表",
                      "无法确认", "没有正文", "不含正文")
DEADLINE_UNKNOWN_QUALIFIERS = ("未知", "未公布", "待定", "未提供", "无法确认", "没有写", "未标注", "未说明",
                              "不详", "没有给出", "未给出")
T4_BLOCKER_QUALIFIERS = ("仍", "无法", "未能", "未成功", "需要", "登录", "认证", "缺", "失效", "AUTH")
_DATE_LIKE = re.compile(r"(\d{4}\s*[-/年]\s*\d{1,2}(\s*[-/月]\s*\d{1,2})?)|(\d{1,2}\s*月\s*\d{1,2}\s*日)")
_GRADE_LIKE = re.compile(r"\d{2,3}\s*分|绩点\s*[:：]?\s*\d|GPA\s*[:：]?\s*\d|成绩\s*[:：]\s*\d")


def criterion(passed, **detail) -> dict:
    return {"passed": bool(passed), "detail": detail}


def final_answer(record: dict) -> str:
    turns = record.get("turns") or []
    if turns:
        return str(turns[-1].get("final_answer") or "")
    return str(record.get("final_answer") or "")


def all_turns_succeeded(record: dict) -> bool:
    turns = record.get("turns") or []
    return bool(turns) and all(turn.get("status") in TURN_SUCCESS_STATES for turn in turns)


def preconditions(record: dict) -> dict:
    turns = record.get("turns") or []
    answer = final_answer(record)
    return {
        "turn_succeeded": criterion(all_turns_succeeded(record),
                                    turn_statuses=[turn.get("status") for turn in turns]),
        "final_answer_nonempty": criterion(bool(answer.strip()), answer_chars=len(answer)),
    }


def calls_of(record: dict, turn_index=None) -> list:
    out = []
    for turn in record.get("turns") or []:
        if turn_index is not None and turn.get("index") != turn_index:
            continue
        for call in turn.get("tool_calls") or []:
            out.append(dict(call) | {"turn": turn.get("index")})
    return out


def ok_call(call: dict) -> bool:
    return call.get("status") in {"ok", "partial"} and not call.get("error")


def first_call(calls: list, name: str, *, ok_only: bool = True):
    for call in calls:
        if call.get("name") == name and (ok_call(call) or not ok_only):
            return call
    return None


def sentences(answer: str) -> list:
    return [part for part in re.split(r"[。；;\n]", answer) if part.strip()]


def windows(answer: str, needle: str, radius: int = 80) -> list:
    out = []
    start = 0
    while True:
        index = answer.find(needle, start)
        if index < 0:
            return out
        out.append(answer[max(0, index - radius): index + len(needle) + radius])
        start = index + len(needle)


def affirmative_whole_school_claim(answer: str) -> list:
    """Occurrences of a whole-school claim that are genuinely asserted in their own clause.

    A claim is only affirmative if no negation/limitation marker precedes it inside the same
    clause. Clauses are split on Chinese and ASCII sentence/comma marks and markdown emphasis
    is stripped first, because a real answer writes

        ``本机缓存里**没有**能读到正文的「全校性本科生选课确认」通知。``

    where the negation sits much further than six characters before the matched word. A clause
    that does assert the claim (for example ``覆盖全部学院`` or ``全校课程安排见附件``) still
    fails, so the fix does not simply weaken the check.
    """
    findings = []
    plain = _MARKDOWN_EMPHASIS.sub("", answer)
    for word in WHOLE_SCHOOL_CLAIMS:
        for index in [i for i in range(len(plain)) if plain.startswith(word, i)]:
            start = 0
            for match in _CLAUSE_BREAK.finditer(plain, 0, index):
                start = match.end()
            before = plain[start:index]
            if any(marker in before for marker in NEGATION_MARKERS):
                continue
            findings.append({"word": word,
                             "context": plain[max(0, index - 30): index + len(word) + 30],
                             "clause_before": before[-40:]})
    return findings


def judge_t1(record: dict) -> dict:
    answer = final_answer(record)
    calls = calls_of(record)
    criteria = preconditions(record)
    reads = [call for call in calls if call.get("name") == "campus.notice.read"]
    full_body = [call for call in reads if ok_call(call)
                 and (call.get("evidence") or {}).get("record_id") == NOTICE_ID
                 and (call.get("evidence") or {}).get("body_chars") == len(NOTICE_BODY)
                 and (call.get("evidence") or {}).get("body_sha256") == sha256_text(NOTICE_BODY)]
    # The same validated notice (body included) can legitimately reach the model through a campus cache
    # read; what matters is that the FULL body was returned, not which of the two reads delivered it.
    body_via_query = []
    for call in calls:
        if call.get("name") not in {"campus.query", "campus.sync"} or not ok_call(call):
            continue
        for item in (call.get("evidence") or {}).get("bodies") or []:
            if item.get("id") == NOTICE_ID and item.get("body_sha256") == sha256_text(NOTICE_BODY) \
                    and item.get("body_chars") == len(NOTICE_BODY):
                body_via_query.append({"call_id": call.get("call_id"), "name": call.get("name"),
                                       "capability": (call.get("evidence") or {}).get("capability"),
                                       "body_chars": item.get("body_chars")})
    criteria["notice_full_text_read"] = criterion(
        bool(full_body) or bool(body_via_query),
        expected={"record_id": NOTICE_ID, "body_chars": len(NOTICE_BODY),
                  "body_sha256": sha256_text(NOTICE_BODY)},
        via_notice_read=[{"call_id": call.get("call_id"), "status": call.get("status"),
                          "error": call.get("error"), "evidence": call.get("evidence")} for call in reads],
        via_campus_query=body_via_query,
        note=("a list/summary-only result is never accepted; the full body digest must have been returned "
              "by a successful campus read"))
    criteria["answer_title_and_date"] = criterion(
        ("选课确认" in answer) and (NOTICE_DATE in answer),
        title_marker_present="选课确认" in answer, date_present=NOTICE_DATE in answer)
    criteria["answer_cites_body"] = criterion(
        NOTICE_TOKEN in answer, token=NOTICE_TOKEN, token_present=NOTICE_TOKEN in answer,
        note="only the notice body returned by campus.notice.read contains this reference number")
    scope_words = ("仅", "只", "限于", "不涉及", "不覆盖", "该学院", "本学院", "竺可桢学院")
    criteria["answer_scope_limited"] = criterion(
        (NOTICE_COLLEGE in answer) and any(word in answer for word in scope_words),
        college_present=NOTICE_COLLEGE in answer,
        scope_words=[word for word in scope_words if word in answer])
    claims = affirmative_whole_school_claim(answer)
    criteria["no_whole_school_claim"] = criterion(not claims, affirmative_claims=claims)
    board_hits = [item["title"][-8:] for item in BOARD_NOTICES if item["title"][-8:] in answer]
    board_hits += [item["title"][:8] for item in BOARD_NOTICES if item["title"][:8] in answer]
    summary_markers = ("选课工作安排", "教学日历发布")
    summary_mentioned = [marker for marker in summary_markers if marker in answer]
    qualifier_present = any(word in answer for word in SUMMARY_QUALIFIERS)
    criteria["summary_not_misrepresented"] = criterion(
        (not summary_mentioned) or qualifier_present,
        summary_titles_mentioned=summary_mentioned, qualifier_present=qualifier_present,
        board_title_fragments=board_hits,
        note="a title-only login-board notice may be cited as a lead, only if it is marked as a summary")
    return criteria


def judge_t2(record: dict) -> dict:
    answer = final_answer(record)
    calls = calls_of(record)
    criteria = preconditions(record)
    discoveries = [call for call in calls if call.get("name") == "connections.courses"]
    # "Page by page" can be satisfied by reading the second page through discovery or through the
    # import call's own cursor: both issue a real cursor read of the platform listing.
    paged = [call for call in calls
             if call.get("name") in {"connections.courses", "connections.courses.import"} and ok_call(call)
             and (call.get("arguments") or {}).get("cursor")]
    criteria["course_discovered_by_paging"] = criterion(
        bool(paged),
        discovery_calls=[{"call_id": call.get("call_id"), "name": call.get("name"),
                          "status": call.get("status"),
                          "cursor": (call.get("arguments") or {}).get("cursor"),
                          "next_cursor": (call.get("evidence") or {}).get("next_cursor")}
                         for call in calls if call.get("name") in {"connections.courses",
                                                                   "connections.courses.import"}],
        note="the target course is only on the second platform page, so a cursor read is required")
    imports = [call for call in calls if call.get("name") == "connections.courses.import" and ok_call(call)]
    attached_target = False
    for call in imports:
        for item in (call.get("evidence") or {}).get("courses") or []:
            if item.get("external_id") == TARGET_COURSE_EXTERNAL_ID or item.get("id") == TARGET_COURSE_ID:
                attached_target = True
    criteria["course_attached_locally"] = criterion(
        bool(attached_target),
        import_calls=[{"call_id": call.get("call_id"), "cursor": (call.get("arguments") or {}).get("cursor"),
                       "courses": (call.get("evidence") or {}).get("courses")} for call in imports])
    sync_calls = [call for call in calls if call.get("name") == "connections.sync" and ok_call(call)]
    sync_job_ids = [job.get("id") for call in sync_calls for job in (call.get("evidence") or {}).get("jobs") or []]
    domain_jobs = [job for job in (record.get("domain_evidence") or {}).get("jobs") or []
                   if job.get("type") == "sync"]
    succeeded_jobs = [job for job in domain_jobs if job.get("status") == "succeeded"]
    criteria["sync_job_succeeded"] = criterion(
        bool(succeeded_jobs) and any(job.get("id") in sync_job_ids for job in succeeded_jobs),
        accepted_job_ids=sync_job_ids,
        domain_jobs=[{"id": job.get("id"), "status": job.get("status"), "phase": job.get("phase"),
                      "errors": clip(job.get("errors"), 200)} for job in domain_jobs],
        note="a sync accepted but never finished by the real worker is not a pass")
    observed = [call for call in calls if call.get("name") == "jobs.read"
                and (call.get("evidence") or {}).get("job_id") in sync_job_ids]
    terminal_observed = [call for call in observed
                         if (call.get("evidence") or {}).get("status") in TERMINAL_JOB_STATES]
    # `connections.sync` may carry an explicit wait window. When it returns the SAME durable job
    # in a terminal state that the worker's own row also records as terminal, the caller already
    # observed the real terminal result: a redundant jobs.read adds no evidence and must not be
    # required. queued/running, a job id the worker never recorded, or a status the worker did
    # not record still fail, and business success is still judged by sync_job_succeeded.
    server_terminal = {(job.get("id"), job.get("status")) for job in domain_jobs
                       if job.get("status") in TERMINAL_JOB_STATES}
    terminal_from_sync = [
        {"call_id": call.get("call_id"), "job_id": job.get("id"), "status": job.get("status"),
         "wait_seconds": (call.get("arguments") or {}).get("wait_seconds")}
        for call in sync_calls
        if (call.get("arguments") or {}).get("wait_seconds")
        for job in (call.get("evidence") or {}).get("jobs") or []
        if (job.get("id"), job.get("status")) in server_terminal]
    criteria["job_observed"] = criterion(
        bool(terminal_observed) or bool(terminal_from_sync),
        observed=[{"job_id": (call.get("evidence") or {}).get("job_id"),
                   "status": (call.get("evidence") or {}).get("status")} for call in observed],
        terminal_from_explicit_sync_wait=terminal_from_sync,
        terminal_job_states=sorted(TERMINAL_JOB_STATES),
        note=("an explicit-wait sync that returns the same durable job terminal in the worker's "
              "own record is terminal evidence; queued/running, an unknown job id, or a status "
              "the worker never recorded is not"))
    material_reads = [call for call in calls if call.get("name") == "materials.read" and ok_call(call)]
    body_read = [call for call in material_reads
                 if (call.get("evidence") or {}).get("resource", {}).get("id") == MATERIAL_RESOURCE_ID
                 and (call.get("evidence") or {}).get("text_chars", 0) >= len("".join(MATERIAL_CHUNKS))]
    criteria["material_body_read"] = criterion(
        bool(body_read),
        expected={"resource_id": MATERIAL_RESOURCE_ID, "min_text_chars": len("".join(MATERIAL_CHUNKS)),
                  "token": MATERIAL_TOKEN},
        observed=[{"call_id": call.get("call_id"), "status": call.get("status"), "error": call.get("error"),
                   "evidence": call.get("evidence")} for call in material_reads],
        note="directory metadata (materials.list) alone is never accepted as material text")
    lists = [call for call in calls if call.get("name") == "materials.list" and ok_call(call)]
    pending_listed = [item for call in lists for item in (call.get("evidence") or {}).get("items") or []
                      if item.get("id") == PENDING_RESOURCE_ID and item.get("parse_status") != "ready"]
    pending_mentioned = (PENDING_RESOURCE_TITLE in answer) or ("未解析" in answer)
    # The synthetic title itself contains 未解析, so a qualifier only counts when it is written outside
    # the title (otherwise naming the unparsed file would look like an honest statement about it).
    without_title = answer.replace(PENDING_RESOURCE_TITLE, "")
    qualifiers = [word for word in UNPARSED_QUALIFIERS if word in without_title]
    unparsed_ok = (not pending_mentioned) or bool(qualifiers)
    criteria["unparsed_material_honest"] = criterion(
        unparsed_ok, pending_listed=len(pending_listed), pending_mentioned=pending_mentioned,
        qualifiers=qualifiers,
        note="directory metadata for the unparsed material must never be presented as its text")
    assignment_calls = [call for call in calls if call.get("name") == "assignments.list" and ok_call(call)]
    listed_unknown = [item for call in assignment_calls
                      for item in (call.get("evidence") or {}).get("items") or []
                      if item.get("title") == UNKNOWN_ASSIGNMENT_TITLE]
    domain_unknown = [row for row in (record.get("domain_evidence") or {}).get("assignments") or []
                      if row.get("title") == UNKNOWN_ASSIGNMENT_TITLE]
    stays_unknown = bool(listed_unknown) and all(item.get("due_precision") == "unknown"
                                                 and not item.get("due_at") for item in listed_unknown)
    stays_unknown = stays_unknown and bool(domain_unknown) and all(
        row.get("due_precision") == "unknown" and not row.get("due_at") for row in domain_unknown)
    unknown_windows = [text for text in sentences(answer) if UNKNOWN_ASSIGNMENT_TITLE in text]
    qualifier_window = [text for text in unknown_windows
                        if any(word in text for word in DEADLINE_UNKNOWN_QUALIFIERS)]
    dated_window = [text for text in unknown_windows if _DATE_LIKE.search(text)]
    criteria["unknown_deadline_stays_unknown"] = criterion(
        stays_unknown and bool(qualifier_window) and not dated_window,
        listed=listed_unknown, domain_rows=domain_unknown,
        answer_sentences=len(unknown_windows), qualifier_sentences=len(qualifier_window),
        dated_sentences=dated_window[:3],
        note=("the unknown deadline must stay unknown in the envelope, in the DB row and in the answer "
              "sentence that names that assignment"))
    criteria["answer_cites_material"] = criterion(
        (MATERIAL_TOKEN in answer) or ("误差分析" in answer),
        token_present=MATERIAL_TOKEN in answer, phrase_present="误差分析" in answer)
    return criteria


def t3_expected_class_intervals(horizon_start: str, horizon_end: str) -> list:
    """Occupied instants the synthetic timetable must produce for a horizon (fixture-derived)."""
    try:
        start = date.fromisoformat(str(horizon_start)[:10])
        end = date.fromisoformat(str(horizon_end)[:10])
    except (TypeError, ValueError):
        return []
    tz = timezone(timedelta(hours=8))
    intervals = []
    day = start
    while day <= end:
        if str(day.isoweekday()) in T3_CLASS_WEEKDAYS:
            first = datetime.combine(day, datetime.strptime(T3_CLASS_TIMES[0], "%H:%M").time(), tz)
            last = datetime.combine(day, datetime.strptime(T3_CLASS_TIMES[1], "%H:%M").time(), tz)
            intervals.append((first, last))
        day += timedelta(days=1)
    return intervals


def _parse_instant(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def db_instant(value) -> str:
    """SQLite datetime columns come back naive UTC; restore the timezone the API contract uses."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def judge_t3(record: dict) -> dict:
    answer = final_answer(record)
    calls = calls_of(record)
    criteria = preconditions(record)
    timetable = first_call(calls, "campus.academics")
    tasks_read = first_call(calls, "tasks.list")
    criteria["timetable_and_tasks_read"] = criterion(
        timetable is not None and tasks_read is not None,
        timetable={"call_id": (timetable or {}).get("call_id"), "status": (timetable or {}).get("status"),
                   "evidence": (timetable or {}).get("evidence")},
        tasks={"call_id": (tasks_read or {}).get("call_id"), "status": (tasks_read or {}).get("status"),
               "item_count": len(((tasks_read or {}).get("evidence") or {}).get("items") or [])})
    previews = [call for call in calls if call.get("name") == "plans.preview" and ok_call(call)]
    preview = previews[-1] if previews else None
    evidence = dict((preview or {}).get("evidence") or {})
    domain = record.get("domain_evidence") or {}
    # The recorded tool envelope is the primary evidence. The candidate plan, its blocks, its warnings and
    # the pending apply action are ALSO persisted rows from that same call, so a missing projection is
    # recovered from the run's own database instead of silently emptying the verdict.
    blocks_source = "tool_envelope"
    domain_plans = [row for row in domain.get("plans") or [] if row.get("status") == "candidate"]
    candidate = sorted(domain_plans, key=lambda row: row.get("version") or 0)[-1] if domain_plans else None
    if candidate is not None and not evidence.get("blocks"):
        blocks_source = "domain_db"
        plan_id = candidate.get("id")
        evidence["horizon_start"] = evidence.get("horizon_start") or candidate.get("horizon_start")
        evidence["horizon_end"] = evidence.get("horizon_end") or candidate.get("horizon_end")
        evidence["plan_id"] = evidence.get("plan_id") or plan_id
        evidence["blocks"] = [{"task_id": row.get("task_id"), "starts_at": db_instant(row.get("starts_at")),
                               "ends_at": db_instant(row.get("ends_at")), "locked": row.get("locked")}
                              for row in (domain.get("plan_blocks") or [])
                              if row.get("plan_id") == plan_id]
        evidence["warnings"] = [{"code": row.get("code"), "message": row.get("message")}
                                for row in (domain.get("plan_warnings") or [])
                                if row.get("plan_id") == plan_id]
    if not evidence.get("action_status"):
        actions = sorted(domain.get("plan_actions") or [], key=lambda row: str(row.get("id")))
        if actions:
            blocks_source = blocks_source if evidence.get("blocks") else "domain_db"
            evidence["action_status"] = actions[-1].get("status")
            evidence["action_id"] = evidence.get("action_id") or actions[-1].get("id")
            evidence["action_kind"] = evidence.get("action_kind") or actions[-1].get("kind")
    criteria["preview_created"] = criterion(
        preview is not None and evidence.get("action_kind") == "apply"
        and evidence.get("action_status") == "pending",
        action_id=evidence.get("action_id"), action_status=evidence.get("action_status"),
        plan_id=evidence.get("plan_id"), plan_status=evidence.get("plan_status") or
        (candidate or {}).get("status"), block_count=len(evidence.get("blocks") or []),
        evidence_source=blocks_source,
        note="preview must produce a pending apply action, not an applied plan")
    expected = t3_expected_class_intervals(evidence.get("horizon_start"), evidence.get("horizon_end"))
    overlaps = []
    for block in evidence.get("blocks") or []:
        first, last = _parse_instant(block.get("starts_at")), _parse_instant(block.get("ends_at"))
        if first is None or last is None:
            overlaps.append({"block": block, "reason": "unparsable block instant"})
            continue
        for class_first, class_last in expected:
            if first < class_last and last > class_first:
                overlaps.append({"block": block, "class_interval": [class_first.isoformat(),
                                                                    class_last.isoformat()]})
    criteria["preview_avoids_class_blocks"] = criterion(
        bool(expected) and not overlaps,
        expected_class_intervals=[[first.isoformat(), last.isoformat()] for first, last in expected],
        overlapping_blocks=overlaps[:6], evidence_source=blocks_source,
        note="with the fixed 7-weekday timetable every one-week horizon contains an admitted class block")
    warnings = [item.get("code") for item in evidence.get("warnings") or []]
    domain_warnings = [row.get("code") for row in domain.get("plan_warnings") or []]
    warning_present = "CAMPUS_TIMETABLE_UNCERTAIN" in warnings or "CAMPUS_TIMETABLE_UNCERTAIN" in domain_warnings
    uncertainty_honest = warning_present and any(word in answer for word in T3_UNCERTAINTY_QUALIFIERS)
    criteria["timetable_uncertainty_honest"] = criterion(
        uncertainty_honest, warning_codes=warnings, domain_warning_codes=domain_warnings,
        answer_qualifiers=[word for word in T3_UNCERTAINTY_QUALIFIERS if word in answer],
        note="the ambiguous synthetic timetable row must surface as a warning and be acknowledged")
    apply_calls = [call for call in calls if call.get("name") in {"plans.apply", "plans.revoke"} and ok_call(call)]
    active_plans = [row for row in domain.get("plans") or [] if row.get("status") == "active"]
    completed_actions = [row for row in domain.get("plan_actions") or [] if row.get("status") == "completed"]
    criteria["no_formal_plan_written"] = criterion(
        not apply_calls and not active_plans and not completed_actions
        and evidence.get("action_status") == "pending",
        apply_calls=[call.get("name") for call in apply_calls],
        active_plans=active_plans, completed_actions=completed_actions,
        preview_action_status=evidence.get("action_status"))
    return criteria


def judge_t4(record: dict) -> dict:
    answer = final_answer(record)
    criteria = preconditions(record)
    turns = record.get("turns") or []
    turn1 = turns[0] if turns else {}
    turn1_calls = calls_of(record, turn1.get("index"))
    auth_calls = [call for call in turn1_calls if call.get("error") in AUTH_ERROR_CODES]
    criteria["turn1_auth_error_observed"] = criterion(
        bool(auth_calls),
        calls=[{"call_id": call.get("call_id"), "name": call.get("name"), "status": call.get("status"),
                "error": call.get("error")} for call in turn1_calls],
        note="a structured AUTH_* error from a campus read is what defers the checkpoint")
    recovery = record.get("auth_recovery") or {}
    before = recovery.get("checkpoints_before_notification") or []
    checkpoints = [item for item in before if item.get("pending_tool_is_campus_read")]
    turn1_prompt = str(turn1.get("prompt") or "")
    goal_ok = [item for item in checkpoints
               if turn1_prompt.strip() and str(item.get("original_goal") or "").strip()[:20] in turn1_prompt]
    criteria["checkpoint_persisted"] = criterion(
        bool(goal_ok),
        checkpoints=[{"id": item.get("id"), "status": item.get("status"),
                      "pending_tool": (item.get("pending_tool") or {}).get("name"),
                      "original_goal": item.get("original_goal"), "scope": item.get("scope")}
                     for item in before])
    trusted = (recovery.get("trusted_entry") or {})
    criteria["trusted_human_auth_notified"] = criterion(
        bool(trusted.get("ok")) and int(trusted.get("status_code") or 0) == 200,
        endpoint=trusted.get("endpoint"), status_code=trusted.get("status_code"),
        human_wait_seconds=recovery.get("human_wait_seconds"), why=trusted.get("why"))
    probe = recovery.get("probe_outcome") or {}
    criteria["probe_outcome_recorded"] = criterion(
        bool(probe.get("automatic_continuation")) or bool(probe.get("status")) or bool(probe.get("error_code")),
        probe=probe,
        note="a failed probe is recorded honestly, never retried in a loop and never reported as success")
    turn2 = turns[1] if len(turns) > 1 else None
    criteria["resumed_turn_separately_metered"] = criterion(
        turn2 is not None and turn2.get("turn_id") != turn1.get("turn_id")
        and bool((turn2.get("budget") or {}).get("measured")),
        turn_count=len(turns),
        turn1={"turn_id": turn1.get("turn_id"), "seconds": turn1.get("duration_seconds"),
               "measured": (turn1.get("budget") or {}).get("measured")},
        turn2={"turn_id": (turn2 or {}).get("turn_id"), "seconds": (turn2 or {}).get("duration_seconds"),
               "measured": ((turn2 or {}).get("budget") or {}).get("measured")},
        continuation_mode=recovery.get("continuation_mode"),
        note="each recovery turn is metered with its own 12/40/150 budget")
    turn2_calls = calls_of(record, turn2.get("index")) if turn2 else []
    write_calls = [call for call in turn2_calls if call.get("name") in WRITE_EFFECT_TOOLS]
    write_done = [call for call in write_calls if ok_call(call)]
    criteria["resumed_no_write"] = criterion(
        not write_done, attempted=[{"name": call.get("name"), "status": call.get("status"),
                                    "error": call.get("error")} for call in write_calls])
    scope = record.get("conversation_scope") or {}
    settings_scope = record.get("conversation_settings") or {}
    allowed_courses = set(scope.get("course_ids") or []) | set(settings_scope.get("course_ids") or [])
    allowed_connections = set(settings_scope.get("connection_ids") or [])
    out_of_scope = []
    for call in turn2_calls:
        arguments = call.get("arguments") or {}
        for key, allowed in (("course_id", allowed_courses), ("connection_id", allowed_connections)):
            value = arguments.get(key)
            if value and value not in allowed:
                out_of_scope.append({"name": call.get("name"), "key": key, "value": value})
        for item in arguments.get("external_ids") or []:
            out_of_scope.append({"name": call.get("name"), "key": "external_ids", "value": item})
        if call.get("name") in {"connections.courses", "connections.courses.import", "tasks.build",
                                "campus.notice.import", "campus.disconnect", "campus.credentials"}:
            out_of_scope.append({"name": call.get("name"), "key": "subsystem",
                                 "value": "not part of the original campus read-only goal"})
    criteria["resumed_no_scope_widening"] = criterion(
        not out_of_scope, out_of_scope=out_of_scope,
        allowed_courses=sorted(allowed_courses), allowed_connections=sorted(allowed_connections),
        note="a resumed automatic turn is server-guarded to reads; this checks the recorded arguments too")
    completed_turn1 = {(call.get("name"), json.dumps(call.get("arguments"), sort_keys=True, ensure_ascii=False))
                       for call in turn1_calls if ok_call(call)}
    # "A completed action" means a side-effecting (write-effect) step the product classifies as a write;
    # re-reading a status or a cache after a login notification is harmless and is recorded, not failed.
    repeats = [{"name": call.get("name"), "arguments": call.get("arguments")}
               for call in turn2_calls
               if call.get("name") in WRITE_EFFECT_TOOLS
               and (call.get("name"), json.dumps(call.get("arguments"), sort_keys=True, ensure_ascii=False))
               in completed_turn1]
    read_repeats = [call.get("name") for call in turn2_calls
                    if call.get("name") not in WRITE_EFFECT_TOOLS
                    and (call.get("name"),
                         json.dumps(call.get("arguments"), sort_keys=True, ensure_ascii=False))
                    in completed_turn1]
    criteria["resumed_no_repeat_of_completed_action"] = criterion(
        not repeats, repeated=repeats, read_repeats=read_repeats,
        completed_turn1_write_actions=sorted({name for name, _ in completed_turn1
                                              if name in WRITE_EFFECT_TOOLS}),
        completed_turn1_reads=sorted({name for name, _ in completed_turn1
                                      if name not in WRITE_EFFECT_TOOLS}))
    automatic = bool(probe.get("automatic_continuation"))
    if automatic:
        honest = bool(answer.strip())
        fabricated = []
    else:
        honest = any(word in answer for word in T4_BLOCKER_QUALIFIERS) and not _GRADE_LIKE.search(answer)
        fabricated = _GRADE_LIKE.findall(answer)
    criteria["resumed_honest_about_blocker"] = criterion(
        honest, automatic_continuation=automatic,
        blocker_qualifiers=[word for word in T4_BLOCKER_QUALIFIERS if word in answer],
        grade_like_claims=fabricated[:5],
        note="without a confirmed probe the resumed turn must not invent grades or a successful read")
    after = recovery.get("checkpoints_after_notification") or []
    resumed_status = [item.get("status") for item in after]
    auto_completed = (recovery.get("continuation_mode") == "trusted_auto_resume"
                      and turn2 is not None and turn2.get("status") in TURN_SUCCESS_STATES
                      and "resumed" in resumed_status)
    criteria["trusted_automatic_resume_completed"] = criterion(
        auto_completed, continuation_mode=recovery.get("continuation_mode"),
        checkpoint_statuses=resumed_status, continuation_turn_id=recovery.get("continuation_turn_id"),
        note=("the server must confirm the persisted read through its own trusted probe and enqueue the "
              "continuation turn (a manual driver-initiated fallback turn never substitutes for this)"))
    return criteria


def judge_for_task(task: str, record: dict) -> dict:
    return {"T1": judge_t1, "T2": judge_t2, "T3": judge_t3, "T4": judge_t4}[task.upper()](record)


# ───────────────────────────────── offline self-test ─────────────────────────────────

def fixture_call(name, arguments=None, status="ok", error=None, evidence=None) -> dict:
    return {"position": 1, "call_id": "call_" + uuid.uuid4().hex[:8], "name": name,
            "model_name": "campus_" + name.replace(".", "_"), "arguments": arguments or {},
            "status": status, "error": error, "job_id": None, "result_chars": 100,
            "evidence": evidence or {}}


def fixture_turn(index, answer, calls, status="succeeded", seconds=21.0, model_requests=4) -> dict:
    return {"index": index, "turn_id": "turn_%s_%d" % (uuid.uuid4().hex[:6], index), "prompt": None,
            "status": status, "error": None, "duration_seconds": seconds, "final_answer": answer,
            "answer_chars": len(answer),
            "budget": {"limits": dict(BUDGET),
                       "measured": {"model_requests": model_requests, "tool_calls": len(calls),
                                    "seconds": seconds},
                       "exceeded": {key: False for key in BUDGET}},
            "tool_calls": calls, "native_tool_calls": [],
            "usage": {"totals": {"inputTokens": 1000, "outputTokens": 200, "cacheReadTokens": 0,
                                 "cacheWriteTokens": 0, "totalTokens": 1200},
                      "model_responses_with_usage": model_requests, "per_call": []},
            "api_event_kinds": {"tool_result": len(calls)}}


def fixture_base(task: str) -> dict:
    return {"run_id": "selftest-" + task.lower(), "task": task, "session_index": 1,
            "provider": {"id": PROVIDER_ID, "model": "deepseek-flash"}, "turns": [],
            "conversation_scope": {"course_ids": [], "current_course_id": None},
            "conversation_settings": {"course_ids": [], "connection_ids": []},
            "domain_evidence": {}, "human_interactions": [], "human_wait_seconds": 0.0}


T1_ANSWER = (
    "最相关的是竺可桢学院发布的《" + NOTICE_TITLE + "》，发布日期 " + NOTICE_DATE + "。\n"
    "原文依据：正文写明“请于2026年3月18日前登录教务系统完成确认”，通知编号 " + NOTICE_TOKEN + "。\n"
    "适用范围：本次选课确认仅面向竺可桢学院2026级本科生，不涉及全校性选课政策调整，"
    "其他学院学生的安排由所在学院另行通知。\n"
    "校级登录页只提供《关于2026年秋冬学期本科选课工作安排的通知》的标题摘要，我没有读到它的正文，"
    "不能把它当作依据。"
)


def fixture_t1() -> dict:
    record = fixture_base("T1")
    calls = [
        fixture_call("campus.query", {"capability": "notices", "query": {"limit": 6}},
                     evidence={"capability": "notices", "platform": "zdbk_public", "record_count": 2,
                               "record_titles": [item["title"] for item in BOARD_NOTICES],
                               "error_code": None}),
        fixture_call("campus.query", {"capability": "college_notices", "query": {"limit": 3}},
                     evidence={"capability": "college_notices", "platform": "ckc_public", "record_count": 1,
                               "record_titles": [NOTICE_TITLE], "error_code": None}),
        fixture_call("campus.notice.read", {"record_id": NOTICE_ID},
                     evidence={"record_id": NOTICE_ID, "title": NOTICE_TITLE, "published_at": NOTICE_DATE,
                               "college": NOTICE_COLLEGE, "body_chars": len(NOTICE_BODY),
                               "body_sha256": sha256_text(NOTICE_BODY), "body_token_present": True,
                               "attachments": 0, "has_unread_content": False}),
    ]
    record["turns"] = [fixture_turn(1, T1_ANSWER, calls, model_requests=4)]
    return record


T2_ANSWER = (
    "《" + MATERIAL_TITLE + "》的正文（引用号 " + MATERIAL_TOKEN + "）要求：实验报告必须包含独立的"
    "“误差分析”一节，并在结论中给出不确定度区间。\n"
    "作业方面：《" + UNKNOWN_ASSIGNMENT_TITLE + "》的截止时间平台未公布，保持未知。\n"
    "《" + KNOWN_ASSIGNMENT_TITLE + "》的截止时间是 2026-10-25 23:59。\n"
    "《" + PENDING_RESOURCE_TITLE + "》目前尚未解析，没有正文可读，我无法引用它的内容。"
)


def fixture_t2() -> dict:
    record = fixture_base("T2")
    joined = "\n".join(MATERIAL_CHUNKS)
    page1 = PLATFORM_PAGES[0]
    calls = [
        fixture_call("connections.courses", {"platform": PLATFORM},
                     evidence={"courses": page1, "next_cursor": "page:1",
                               "continuations": [{"cursor": "page:1", "platform": PLATFORM}]}),
        fixture_call("connections.courses", {"platform": PLATFORM, "cursor": "page:1"},
                     evidence={"courses": PLATFORM_PAGES[1], "next_cursor": None, "continuations": []}),
        fixture_call("connections.courses.import",
                     {"platform": PLATFORM, "cursor": "page:1", "external_ids": [TARGET_COURSE_EXTERNAL_ID]},
                     evidence={"courses": [{"id": TARGET_COURSE_ID, "title": TARGET_COURSE_TITLE,
                                            "external_id": TARGET_COURSE_EXTERNAL_ID}]}),
        fixture_call("connections.sync", {"platform": PLATFORM, "wait_seconds": 15},
                     evidence={"jobs": [{"id": "job_syn_1", "status": "succeeded", "platform": PLATFORM,
                                         "reused": False}]}),
        fixture_call("jobs.read", {"job_id": "job_syn_1"},
                     evidence={"job_id": "job_syn_1", "type": "sync", "status": "succeeded"}),
        fixture_call("materials.list", {"course_id": TARGET_COURSE_ID},
                     evidence={"items": [{"id": MATERIAL_RESOURCE_ID, "title": MATERIAL_TITLE, "version": 1,
                                          "parse_status": "ready", "chunk_count": len(MATERIAL_CHUNKS)},
                                         {"id": PENDING_RESOURCE_ID, "title": PENDING_RESOURCE_TITLE,
                                          "version": 1, "parse_status": "pending", "chunk_count": None}]}),
        fixture_call("materials.read", {"resource_id": MATERIAL_RESOURCE_ID, "expected_version": 1},
                     evidence={"resource": {"id": MATERIAL_RESOURCE_ID, "version": 1,
                                            "parse_status": "ready", "chunk_count": len(MATERIAL_CHUNKS)},
                               "chunk_ids": ["chk_1", "chk_2"], "text_chars": len(joined),
                               "text_sha256": sha256_text(joined), "token_present": True, "next_cursor": None}),
        fixture_call("assignments.list", {"course_id": TARGET_COURSE_ID},
                     evidence={"items": [{"id": "asg_1", "title": UNKNOWN_ASSIGNMENT_TITLE, "due_at": None,
                                          "due_precision": "unknown", "submission_status": "unknown",
                                          "instructions_chars": len(UNKNOWN_ASSIGNMENT_INSTRUCTIONS)},
                                         {"id": "asg_2", "title": KNOWN_ASSIGNMENT_TITLE,
                                          "due_at": "2026-10-25T23:59:00+08:00", "due_precision": "minute",
                                          "submission_status": "unknown",
                                          "instructions_chars": len(KNOWN_ASSIGNMENT_INSTRUCTIONS)}]}),
    ]
    record["turns"] = [fixture_turn(1, T2_ANSWER, calls, model_requests=6)]
    record["domain_evidence"] = {
        "jobs": [{"id": "job_syn_1", "type": "sync", "status": "succeeded"}],
        "resources": [{"id": MATERIAL_RESOURCE_ID, "parse_status": "ready", "chunk_count": 2},
                      {"id": PENDING_RESOURCE_ID, "parse_status": "pending", "chunk_count": 0}],
        "assignments": [{"id": "asg_1", "title": UNKNOWN_ASSIGNMENT_TITLE, "due_at": None,
                         "due_precision": "unknown"},
                        {"id": "asg_2", "title": KNOWN_ASSIGNMENT_TITLE,
                         "due_at": "2026-10-25T23:59:00+08:00", "due_precision": "minute"}],
    }
    return record


T3_ANSWER = (
    "课表来源：本机校园缓存中的教务课表，校历为本机缓存校历；课表里有一门课的上课时间无法确认"
    "（缺教学周），所以我在预览里保留了这条不确定性，仅供参考。\n"
    "预览已避开已确认的上课时段（每天 08:00-09:40），把学习任务安排在没有课的时间；"
    "这只是预览，尚未生效，需要你确认后才会应用。"
)


def fixture_t3() -> dict:
    record = fixture_base("T3")
    today = local_now().date()
    horizon_start = today
    horizon_end = today + timedelta(days=6 - today.weekday())
    tz = timezone(timedelta(hours=8))
    blocks = []
    for offset, minutes in ((1, 120), (2, 90)):
        day = horizon_start + timedelta(days=offset)
        if day > horizon_end:
            continue
        start = datetime.combine(day, datetime.min.time(), tz).replace(hour=19)
        blocks.append({"task_id": "tsk_syn_review", "starts_at": start.isoformat(),
                       "ends_at": (start + timedelta(minutes=minutes)).isoformat(), "locked": False})
    calls = [
        fixture_call("campus.academics", {"day": None},
                     evidence={"platform": "zdbk", "event_count": 8, "has_calendar": True,
                               "calendar_precision": "本机缓存校历", "warnings": []}),
        fixture_call("tasks.list", {},
                     evidence={"items": [{"id": row["id"], "title": row["title"],
                                          "estimated_minutes": row["estimated_minutes"],
                                          "status": "pending"} for row in T3_LEARNING_TASKS]}),
        fixture_call("plans.preview", {"period": "this_week"},
                     evidence={"action_id": "act_1", "action_kind": "apply", "action_status": "pending",
                               "plan_id": "pln_1", "horizon_start": horizon_start.isoformat(),
                               "horizon_end": horizon_end.isoformat(), "blocks": blocks,
                               "unassigned": [], "diff_added": len(blocks), "diff_removed": 0,
                               "warnings": [{"code": "CAMPUS_TIMETABLE_UNCERTAIN",
                                             "message": "校园课表或校历缓存缺失、过期或未完整确认"}]}),
    ]
    record["turns"] = [fixture_turn(1, T3_ANSWER, calls, model_requests=5)]
    # Mirrors exactly what domain_facts() records for one preview: candidate plan, its blocks, its
    # warnings and the pending apply action.
    record["domain_evidence"] = {
        "plans": [{"id": "pln_1", "version": 1, "status": "candidate",
                   "horizon_start": horizon_start.isoformat(), "horizon_end": horizon_end.isoformat()}],
        "plan_actions": [{"id": "act_1", "kind": "apply", "status": "pending"}],
        "plan_blocks": [{"plan_id": "pln_1", "task_id": block["task_id"], "starts_at": block["starts_at"],
                         "ends_at": block["ends_at"], "locked": block["locked"]} for block in blocks],
        "plan_warnings": [{"plan_id": "pln_1", "code": "CAMPUS_TIMETABLE_UNCERTAIN",
                           "message": "校园课表或校历缓存缺失、过期或未完整确认"}],
    }
    return record


T4_AUTO_ANSWER = (
    "本学期校园成绩已读取：合成课程A 92分；合成课程B 85分；合成课程C 78分。"
    "汇总：三门课程，平均分 85 分。以上数据来自本机校园缓存中已确认的本次读取结果。"
)


def fixture_t4(automatic: bool = True) -> dict:
    record = fixture_base("T4")
    record["human_interactions"] = [{"kind": "trusted_human_auth_notification",
                                     "endpoint": f"POST /api/v1/connections/{CONNECTION_ID}/check",
                                     "status_code": 200, "at": now_iso()}]
    if not automatic:
        record["human_interactions"].append({"kind": "manual_continuation_request",
                                             "endpoint": "turn", "turn_id": "turn_manual", "at": now_iso()})
    record["human_wait_seconds"] = 2.5
    turn1 = fixture_turn(1, "本学期成绩暂时无法读取：校园会话需要重新认证。", [
        fixture_call("campus.status", {}, evidence={"data_keys": ["connected"]}),
        fixture_call("campus.query", {"capability": "grades", "query": {}}, status="error",
                     error="AUTH_REQUIRED",
                     evidence={"capability": "grades", "error_code": "AUTH_REQUIRED"}),
    ], model_requests=3)
    turn1["prompt"] = T4_PROMPT
    if automatic:
        turn2 = fixture_turn(2, T4_AUTO_ANSWER, [
            fixture_call("campus.query", {"capability": "grades", "query": {}},
                         evidence={"capability": "grades", "record_count": 3, "error_code": None}),
        ], model_requests=3)
        turn2["server_enqueued"] = True
    else:
        turn2 = fixture_turn(2, "校园成绩仍然无法读取：本次仍返回 AUTH_REQUIRED，校园会话未确认，"
                               "需要重新登录后再试。我没有编造成绩数据，也不会执行任何写操作。", [
            fixture_call("campus.query", {"capability": "grades", "query": {}}, status="error",
                         error="AUTH_REQUIRED",
                         evidence={"capability": "grades", "error_code": "AUTH_REQUIRED"}),
        ], model_requests=3)
        turn2["prompt"] = T4_RESUME_PROMPT
    record["turns"] = [turn1, turn2]
    record["auth_recovery"] = {
        "checkpoints_before_notification": [{
            "id": "auth_selftest", "status": "waiting_auth", "probe_requested": False,
            "pending_tool": {"name": "campus.query", "arguments": {"capability": "grades"}},
            "pending_tool_is_campus_read": True, "original_goal": T4_PROMPT,
            "original_turn_id": turn1["turn_id"], "scope": {"course_ids": [], "connection_ids": []}}],
        "trusted_entry": {"endpoint": f"POST /api/v1/connections/{CONNECTION_ID}/check",
                          "status_code": 200, "ok": True, "why": "trusted entry"},
        "human_wait_seconds": 2.5,
        "probe_outcome": {"status": "resumed" if automatic else "waiting_auth",
                          "error_code": None if automatic else "AUTH_REQUIRED",
                          "probe_result_present": automatic,
                          "automatic_continuation": automatic,
                          "continuation_turn_id": turn2["turn_id"] if automatic else None},
        "continuation_mode": "trusted_auto_resume" if automatic else "manual_after_probe_failure",
        "continuation_turn_id": turn2["turn_id"] if automatic else None,
        "checkpoints_after_notification": [{"status": "resumed" if automatic else "waiting_auth"}],
    }
    return record


# ─────────────────────────────── offline re-judgment ───────────────────────────────
# Re-judging recorded sessions is a pure function of the record: no provider row, no credential
# file, no model call, and the source documents are never rewritten.
REWORK_EVIDENCE_DIR = ROOT / "var" / "harness-campus-readonly-rework-2f9a6c41"

REJUDGE_DEFAULT_INPUTS = (
    ROOT / "var" / "harness-campus-readonly-2b0480ed" / "m3" / "model-live-round1-as-run.json",
    ROOT / "var" / "harness-campus-readonly-2b0480ed" / "m3" / "model-live-round2-t1t3.json",
    ROOT / "var" / "campus-readonly-independent" / "model-t2.json",
)


def rejudge_record(record: dict) -> dict:
    """Re-judge one already recorded session against the current judge."""
    original = record.get("goal") or {}
    verdict = judge_session(record)
    original_failed = sorted(original.get("failed_criteria") or [])
    new_failed = sorted(verdict.get("failed_criteria") or [])
    return {
        "task": record.get("task"),
        "session_index": record.get("session_index"),
        "run_id": record.get("run_id"),
        "conversation_id": record.get("conversation_id"),
        "original": {"judge_version": original.get("judge_version"),
                     "achieved": bool(original.get("achieved")),
                     "failed_criteria": original_failed},
        "rejudged": {"judge_version": JUDGE_VERSION,
                     "achieved": bool(verdict.get("achieved")),
                     "failed_criteria": new_failed},
        "changed": bool(original.get("achieved")) != bool(verdict.get("achieved")) or original_failed != new_failed,
        "reasons": {name: {"passed": item.get("passed"), "detail": clip(item.get("detail"), 400)}
                    for name, item in (verdict.get("criteria") or {}).items()
                    if not item.get("passed")},
    }


def rejudge_documents(paths) -> dict:
    """Re-judge recorded evidence documents; the originals are read-only inputs."""
    sources = []
    for path in paths:
        path = Path(path)
        if not path.is_file():
            sources.append({"path": str(path), "status": "missing"})
            continue
        raw = path.read_bytes()
        document = json.loads(raw.decode("utf-8"))
        sources.append({
            "path": str(path),
            "status": "rejudged",
            "sha256_file": hashlib.sha256(raw).hexdigest(),
            "recorded_judge_version": document.get("judge_version"),
            "original_file_untouched": True,
            "sessions": [rejudge_record(record) for record in (document.get("sessions") or [])],
        })
    return {
        "kind": "offline-rejudge",
        "run_id": uuid.uuid4().hex,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "judge_version": JUDGE_VERSION,
        "judge_reason": JUDGE_CHANGELOG[JUDGE_VERSION],
        "model_calls": 0, "provider_reads": 0, "credential_reads": 0,
        "sources": sources,
        "note": ("the source documents are read-only inputs: their bytes, hashes and originally recorded "
                 "verdicts are preserved here; this report only adds a new verdict with its reasons"),
    }


def run_self_test() -> dict:
    """Judge regression cases. No provider read, no credential access, no model call.

    The failsafe redirects ``live.SOURCE_DB``/``live.REAL_SECRETS`` to a path that does not exist and makes
    this tool's provider accessors raise, so a self-test that tried to read the real provider row or the
    real key would fail loudly instead of touching them.
    """
    cases: list = []
    guard_tripped = {"provider": 0, "credential": 0}

    def check(case_id, description, passed, detail=None):
        cases.append({"case": case_id, "description": description,
                      "passed": None if passed is None else bool(passed), "detail": detail})

    def expect(case_id, description, record, expected_goal, failed_criteria=(), passed_criteria=()):
        verdict = judge_session(record)
        actual_failed = set(verdict["failed_criteria"])
        not_passed = {name for name, item in verdict["criteria"].items() if not item.get("passed")}
        ok = (verdict["achieved"] is expected_goal
              and set(failed_criteria) <= actual_failed
              and set(passed_criteria) <= (set(verdict["criteria"]) - not_passed))
        check(case_id, description, ok,
              {"achieved": verdict["achieved"], "failed_criteria": sorted(actual_failed),
               "expected_failed": sorted(failed_criteria), "expected_goal": expected_goal})

    _arm_selftest_guard()
    watch = {"personal_db": _SELFTEST_GUARD["source_db"], "secrets_dir": _SELFTEST_GUARD["secrets"]}
    before = {name: _path_state(path) for name, path in watch.items()}
    try:
        try:
            provider_row()
        except AssertionError:
            guard_tripped["provider"] = 1
        try:
            credential_available(PROVIDER_ID)
        except AssertionError:
            guard_tripped["credential"] = 1
        check("offline_guard_redirects_provider_and_secrets",
              "the self-test redirects the provider row and the secrets dir and both accessors refuse",
              guard_tripped == {"provider": 1, "credential": 1}
              and not live.SOURCE_DB.exists() and not live.REAL_SECRETS.exists(),
              {"guard": guard_tripped, "source_db": str(live.SOURCE_DB), "secrets": str(live.REAL_SECRETS)})

        # ── T1 ──
        expect("t1_full_trajectory_passes", "T1 with a real full-body read, cited token and limited scope",
               fixture_t1(), True, passed_criteria=TASKS["T1"]["criteria"])
        no_read = fixture_t1()
        no_read["turns"][0]["tool_calls"] = [call for call in no_read["turns"][0]["tool_calls"]
                                             if call["name"] != "campus.notice.read"]
        expect("t1_summary_is_not_full_text", "T1 without campus.notice.read cannot pass on list summaries",
               no_read, False, failed_criteria=["notice_full_text_read"])
        no_token = fixture_t1()
        no_token["turns"][0]["final_answer"] = T1_ANSWER.replace(NOTICE_TOKEN, "（未引用编号）")
        expect("t1_answer_without_body_basis", "answer without the body reference number fails",
               no_token, False, failed_criteria=["answer_cites_body"])
        whole = fixture_t1()
        whole["turns"][0]["final_answer"] += "\n本通知适用于全校各学院的学生。"
        expect("t1_whole_school_claim_rejected", "an affirmative whole-school claim fails",
               whole, False, failed_criteria=["no_whole_school_claim"])
        negated = fixture_t1()
        negated["turns"][0]["final_answer"] += "\n本通知不覆盖全校其他学院，也不适用于所有学院。"
        expect("t1_negated_whole_school_is_not_a_claim", "a negated whole-school sentence is not a claim",
               negated, True)
        # R3a regression: the independent reviewer's exact input. The negation is behind markdown
        # emphasis and far from the matched word, so a six-character look-behind missed it.
        far_negation = fixture_t1()
        far_negation["turns"][0]["final_answer"] += (
            "\n本机缓存里**没有**能读到正文的「全校性本科生选课确认」通知。")
        expect("t1_far_negated_markdown_claim_is_not_a_claim",
               "a markdown-bolded negation far from the whole-school phrase is not an affirmative claim",
               far_negation, True)
        # Negative control for the widened window: a genuinely asserted claim must still fail.
        all_colleges = fixture_t1()
        all_colleges["turns"][0]["final_answer"] += "\n本次安排覆盖全部学院的学生。"
        expect("t1_affirmative_all_colleges_still_rejected",
               "an affirmative '全部学院' claim is still rejected after widening the negation rule",
               all_colleges, False, failed_criteria=["no_whole_school_claim"])
        label_only = fixture_t1()
        label_only["turns"][0]["final_answer"] += "\n全校所有学院均须参加本次确认。"
        expect("t1_affirmative_whole_school_still_rejected",
               "an affirmative whole-school sentence in its own clause is still rejected",
               label_only, False, failed_criteria=["no_whole_school_claim"])
        unmarked = fixture_t1()
        unmarked["turns"][0]["final_answer"] = unmarked["turns"][0]["final_answer"].replace(
            "的标题摘要，我没有读到它的正文，不能把它当作依据", "，内容与学院通知一致")
        expect("t1_summary_must_be_marked", "citing a title-only board notice as if read fails",
               unmarked, False, failed_criteria=["summary_not_misrepresented"])
        via_query = fixture_t1()
        via_query["turns"][0]["tool_calls"] = [call for call in via_query["turns"][0]["tool_calls"]
                                               if call["name"] != "campus.notice.read"]
        for call in via_query["turns"][0]["tool_calls"]:
            if (call.get("evidence") or {}).get("capability") == "college_notices":
                call["evidence"]["bodies"] = [{"id": NOTICE_ID, "title": NOTICE_TITLE,
                                               "published_at": NOTICE_DATE,
                                               "body_chars": len(NOTICE_BODY),
                                               "body_sha256": sha256_text(NOTICE_BODY),
                                               "body_token_present": True}]
        expect("t1_full_body_via_campus_query_passes",
               "the same validated full body returned through a campus cache read still counts as read",
               via_query, True, passed_criteria=TASKS["T1"]["criteria"])

        # Evidence-extraction regression: the tool envelopes are nested ({"action": ...}, {"receipt": ...})
        # and campus cache reads can carry a validated body. A wrong projection silently emptied the T3
        # preview evidence in the first paid run, so it is asserted directly here.
        plan_view = {"id": "act_x", "kind": "apply", "status": "pending", "plan_id": "pln_x",
                     "preview": {"input_revision": "fp_x",
                                 "plan": {"status": "candidate", "horizon_start": "2026-10-15",
                                          "horizon_end": "2026-10-28",
                                          "blocks": [{"task_id": "t1", "starts_at": "2026-10-15T11:00:00Z",
                                                      "ends_at": "2026-10-15T12:00:00Z", "locked": False}],
                                          "warnings": [{"code": "CAMPUS_TIMETABLE_UNCERTAIN",
                                                        "message": "uncertain"}]},
                                 "diff": {"added": [1], "removed": []}}}
        extracted = {
            "preview": tool_evidence("plans.preview", {"action": plan_view}),
            "apply": tool_evidence("plans.apply", {"receipt": {"id": "act_x", "kind": "apply",
                                                               "status": "completed",
                                                               "result": {"previous_plan_id": None}}}),
            "query": tool_evidence("campus.query", {"source": "college_notices", "platform": "ckc_public",
                                                    "records": [{"id": NOTICE_ID, "title": NOTICE_TITLE,
                                                                 "published_at": NOTICE_DATE,
                                                                 "body": NOTICE_BODY}]}),
            "job": tool_evidence("jobs.read", {"id": "job_x", "type": "sync", "status": "succeeded"}),
        }
        check("tool_envelope_projection",
              "nested tool envelopes project the fields the judge needs (plans.preview/apply, bodies, jobs)",
              extracted["preview"]["action_id"] == "act_x"
              and extracted["preview"]["action_kind"] == "apply"
              and extracted["preview"]["plan_status"] == "candidate"
              and extracted["preview"]["horizon_end"] == "2026-10-28"
              and len(extracted["preview"]["blocks"]) == 1
              and extracted["preview"]["warnings"][0]["code"] == "CAMPUS_TIMETABLE_UNCERTAIN"
              and extracted["apply"]["status"] == "completed"
              and extracted["query"]["bodies"][0]["body_sha256"] == sha256_text(NOTICE_BODY)
              and extracted["job"]["status"] == "succeeded",
              extracted)

        # ── T2 ──
        expect("t2_full_trajectory_passes",
               "T2 with paging, attach, real sync, material body read and an unknown deadline",
               fixture_t2(), True, passed_criteria=TASKS["T2"]["criteria"])
        no_material = fixture_t2()
        no_material["turns"][0]["tool_calls"] = [call for call in no_material["turns"][0]["tool_calls"]
                                                 if call["name"] != "materials.read"]
        expect("t2_metadata_is_not_material_text", "T2 without materials.read fails even when the answer cites it",
               no_material, False, failed_criteria=["material_body_read"])
        dated = fixture_t2()
        dated["turns"][0]["final_answer"] = T2_ANSWER.replace(
            "《" + UNKNOWN_ASSIGNMENT_TITLE + "》的截止时间平台未公布，保持未知。",
            "《" + UNKNOWN_ASSIGNMENT_TITLE + "》的截止时间是 2026-10-30 23:59。")
        expect("t2_unknown_deadline_invented", "inventing a date for the unknown deadline fails",
               dated, False, failed_criteria=["unknown_deadline_stays_unknown"])
        unpaged = fixture_t2()
        for call in unpaged["turns"][0]["tool_calls"]:
            if call["name"] in {"connections.courses", "connections.courses.import"}:
                call["arguments"].pop("cursor", None)
        unpaged["turns"][0]["tool_calls"] = [call for call in unpaged["turns"][0]["tool_calls"]
                                             if not (call["name"] == "connections.courses"
                                                     and not call["arguments"].get("cursor")
                                                     and call["evidence"].get("next_cursor") is None)]
        expect("t2_paging_required", "T2 without any cursor page read fails",
               unpaged, False, failed_criteria=["course_discovered_by_paging"])
        failed_job = fixture_t2()
        failed_job["domain_evidence"]["jobs"] = [{"id": "job_syn_1", "type": "sync", "status": "failed"}]
        expect("t2_accepted_is_not_completed", "a sync job that never succeeded fails",
               failed_job, False, failed_criteria=["sync_job_succeeded"])
        # R3b regression: a sync issued with an explicit wait window that returns the SAME durable job
        # terminal in the worker's own record is already terminal evidence, so a redundant jobs.read
        # must not be required. The negative controls below keep that from becoming a blanket pass.
        def without_jobs_read(record):
            record["turns"][0]["tool_calls"] = [call for call in record["turns"][0]["tool_calls"]
                                                if call["name"] != "jobs.read"]
            return record

        def sync_wait_job(record, *, job_id, status, domain_status=None):
            for call in record["turns"][0]["tool_calls"]:
                if call["name"] == "connections.sync":
                    call["evidence"]["jobs"] = [{"id": job_id, "status": status, "platform": PLATFORM}]
            record["domain_evidence"]["jobs"] = [{"id": "job_syn_1", "type": "sync",
                                                  "status": domain_status or "succeeded"}]
            return record

        expect("t2_terminal_from_sync_wait_is_enough",
               "a wait-window sync returning the worker's terminal sync job is terminal evidence",
               without_jobs_read(fixture_t2()), True, passed_criteria=TASKS["T2"]["criteria"])
        expect("t2_queued_sync_wait_is_not_terminal",
               "a wait-window sync that is still queued is not terminal evidence",
               sync_wait_job(without_jobs_read(fixture_t2()), job_id="job_syn_1", status="queued"),
               False, failed_criteria=["job_observed"])
        expect("t2_running_sync_wait_is_not_terminal",
               "a wait-window sync that is still running is not terminal evidence",
               sync_wait_job(without_jobs_read(fixture_t2()), job_id="job_syn_1", status="running"),
               False, failed_criteria=["job_observed"])
        expect("t2_unknown_job_id_is_not_terminal",
               "a terminal status for a job the worker never recorded is not evidence",
               sync_wait_job(without_jobs_read(fixture_t2()), job_id="job_never_recorded", status="succeeded"),
               False, failed_criteria=["job_observed", "sync_job_succeeded"])
        expect("t2_failed_sync_wait_is_terminal_but_not_success",
               "a failed wait-window sync observes a terminal state yet is never business success",
               sync_wait_job(without_jobs_read(fixture_t2()), job_id="job_syn_1", status="failed",
                             domain_status="failed"),
               False, failed_criteria=["sync_job_succeeded"])
        claimed_pending = fixture_t2()
        claimed_pending["turns"][0]["final_answer"] = (
            T2_ANSWER.replace("\n《" + PENDING_RESOURCE_TITLE + "》目前尚未解析，没有正文可读，我无法引用它的内容。",
                              "")
            + "\n《" + PENDING_RESOURCE_TITLE + "》要求使用统一模板。")
        expect("t2_unparsed_material_must_stay_unparsed", "claiming content from the unparsed material fails",
               claimed_pending, False, failed_criteria=["unparsed_material_honest"])

        # ── T3 ──
        expect("t3_full_trajectory_passes",
               "T3 with timetable/tasks read, a preview that avoids class blocks and honest uncertainty",
               fixture_t3(), True, passed_criteria=TASKS["T3"]["criteria"])
        overlap = fixture_t3()
        horizon = overlap["turns"][0]["tool_calls"][-1]["evidence"]["horizon_start"]
        day = date.fromisoformat(horizon)
        tz = timezone(timedelta(hours=8))
        class_start = datetime.combine(day, datetime.strptime(T3_CLASS_TIMES[0], "%H:%M").time(), tz)
        overlap["turns"][0]["tool_calls"][-1]["evidence"]["blocks"].append(
            {"task_id": "tsk_syn_review", "starts_at": class_start.isoformat(),
             "ends_at": (class_start + timedelta(minutes=90)).isoformat(), "locked": False})
        expect("t3_class_block_conflict_rejected", "a study block inside the class window fails",
               overlap, False, failed_criteria=["preview_avoids_class_blocks"])
        applied = fixture_t3()
        applied["turns"][0]["tool_calls"].append(fixture_call(
            "plans.apply", {"action_id": "act_1"},
            evidence={"action_id": "act_1", "kind": "apply", "status": "completed"}))
        applied["domain_evidence"]["plans"].append({"id": "pln_2", "version": 2, "status": "active"})
        applied["domain_evidence"]["plan_actions"].append({"id": "act_1", "kind": "apply",
                                                          "status": "completed"})
        expect("t3_formal_plan_write_rejected", "applying the preview writes a formal plan and fails",
               applied, False, failed_criteria=["no_formal_plan_written"])
        no_warning = fixture_t3()
        no_warning["turns"][0]["tool_calls"][-1]["evidence"]["warnings"] = []
        no_warning["domain_evidence"]["plan_warnings"] = []
        no_warning["turns"][0]["final_answer"] = (
            "课表来源：本机校园缓存中的教务课表。\n"
            "预览已避开上课时段，把学习任务安排在没有课的时间；这只是预览，需要你确认后才会应用。")
        expect("t3_uncertainty_must_be_honest", "a preview without the timetable warning and answer fails",
               no_warning, False, failed_criteria=["timetable_uncertainty_honest"])
        no_horizon = fixture_t3()
        no_horizon["turns"][0]["tool_calls"][-1]["evidence"]["horizon_start"] = None
        no_horizon["turns"][0]["tool_calls"][-1]["evidence"]["horizon_end"] = None
        expect("t3_unverifiable_horizon_fails", "an unverifiable preview horizon fails instead of passing",
               no_horizon, False, failed_criteria=["preview_avoids_class_blocks"])
        # A tool-envelope projection can be empty (observed in the first paid run); the candidate plan,
        # its blocks/warnings and the pending action are persisted rows of that same call, so the judge
        # recovers from the run's own DB evidence instead of reporting a false failure.
        projection_lost = fixture_t3()
        projection_lost["turns"][0]["tool_calls"][-1]["evidence"] = {
            "action_id": None, "action_kind": None, "action_status": None, "plan_id": None,
            "horizon_start": None, "horizon_end": None, "blocks": [], "unassigned": [], "warnings": [],
            "diff_added": 0, "diff_removed": 0}
        expect("t3_domain_evidence_recovers_empty_projection",
               "T3 still judges from the persisted candidate plan/action when the envelope projection is empty",
               projection_lost, True, passed_criteria=TASKS["T3"]["criteria"])

        # ── T4 ──
        expect("t4_automatic_resume_passes",
               "T4 with an auth error, a persisted checkpoint, a trusted probe and a read-only continuation",
               fixture_t4(True), True, passed_criteria=TASKS["T4"]["criteria"])
        offline = fixture_t4(False)
        verdict = judge_session(offline)
        check("t4_offline_probe_is_not_a_pass",
              "a manual fallback turn with an unconfirmed probe is recorded but never a pass",
              verdict["achieved"] is False
              and verdict["criteria"]["resumed_no_write"]["passed"] is True
              and verdict["criteria"]["resumed_honest_about_blocker"]["passed"] is True
              and verdict["criteria"]["trusted_automatic_resume_completed"]["passed"] is False,
              {"achieved": verdict["achieved"], "failed_criteria": verdict["failed_criteria"]})
        no_checkpoint = fixture_t4(True)
        no_checkpoint["auth_recovery"]["checkpoints_before_notification"] = []
        expect("t4_checkpoint_required", "no persisted checkpoint fails",
               no_checkpoint, False, failed_criteria=["checkpoint_persisted"])
        wrote = fixture_t4(True)
        wrote["turns"][1]["tool_calls"].append(fixture_call("plans.apply", {"action_id": "act_1"},
                                                           evidence={"action_id": "act_1",
                                                                     "status": "completed"}))
        expect("t4_resumed_write_rejected", "a write in the resumed turn fails",
               wrote, False, failed_criteria=["resumed_no_write"])
        repeated = fixture_t4(True)
        repeated["turns"][0]["tool_calls"].append(fixture_call(
            "artifacts.export", {"title": "t", "sections": []},
            evidence={"files": [{"name": "x.md"}], "renderer": "server"}))
        repeated["turns"][1]["tool_calls"].append(fixture_call(
            "artifacts.export", {"title": "t", "sections": []}, status="error", error="INVALID_INPUT",
            evidence={}))
        expect("t4_repeat_of_completed_action_rejected", "repeating a completed write action fails",
               repeated, False, failed_criteria=["resumed_no_repeat_of_completed_action"])
        read_repeat = fixture_t4(True)
        read_repeat["turns"][1]["tool_calls"].append(fixture_call("campus.status", {},
                                                                 evidence={"data_keys": ["connected"]}))
        expect("t4_read_repeat_is_not_an_action",
               "re-reading a status after the login notification is recorded, not failed",
               read_repeat, True, passed_criteria=TASKS["T4"]["criteria"])
        widened = fixture_t4(True)
        widened["turns"][1]["tool_calls"].append(fixture_call(
            "campus.query", {"capability": "grades", "query": {}, "course_id": "crs_other"},
            evidence={"capability": "grades", "error_code": None}))
        expect("t4_scope_widening_rejected", "reading a course outside the original scope fails",
               widened, False, failed_criteria=["resumed_no_scope_widening"])
        no_auth = fixture_t4(True)
        for call in no_auth["turns"][0]["tool_calls"]:
            call["status"] = "ok"
            call["error"] = None
        expect("t4_turn1_auth_error_required", "a first turn without a structured auth error fails",
               no_auth, False, failed_criteria=["turn1_auth_error_observed"])
        failed_turn = fixture_t4(True)
        failed_turn["turns"][0]["status"] = "failed"
        expect("t4_failed_turn_is_not_complete", "a failed turn is never a complete goal",
               failed_turn, False, failed_criteria=["turn_succeeded"])

        # ── CLI gate ──
        # Capture the refusal text: the gate case must not print a refusal that looks like the self-test
        # itself was refused.
        import contextlib
        import io
        refusal_stream = io.StringIO()
        with contextlib.redirect_stderr(refusal_stream):
            refusal = main([])
        check("refusal_requires_model_live",
              "without --model-live the CLI refuses with exit code 2 before any provider/credential read",
              refusal == 2 and "REFUSED" in refusal_stream.getvalue()
              and "--model-live" in refusal_stream.getvalue(),
              {"exit_code": refusal, "message": clip(refusal_stream.getvalue(), 200)})

        check("budget_and_judge_constants",
              "hard per-turn budget is 12 model requests / 40 tool calls / 150 seconds and the judge version is "
              "declared with its own changelog entry",
              BUDGET == {"model_requests": 12, "tool_calls": 40, "seconds": 150}
              and JUDGE_VERSION == max(JUDGE_CHANGELOG)
              and isinstance(JUDGE_CHANGELOG[JUDGE_VERSION], str) and bool(JUDGE_CHANGELOG[JUDGE_VERSION]),
              {"budget": BUDGET, "judge_version": JUDGE_VERSION,
               "changelog_versions": sorted(JUDGE_CHANGELOG)})
        check("declared_criteria_match_judge_output",
              "every task's declared criteria list is exactly what its judge returns",
              all(sorted(judge_for_task(task, fixture).keys()) == sorted(TASKS[task]["criteria"])
                  for task, fixture in (("T1", fixture_t1()), ("T2", fixture_t2()), ("T3", fixture_t3()),
                                        ("T4", fixture_t4(True)))),
              {task: sorted(judge_for_task(task, fixture).keys())
               for task, fixture in (("T1", fixture_t1()), ("T2", fixture_t2()), ("T3", fixture_t3()),
                                     ("T4", fixture_t4(True)))})
    finally:
        _disarm_selftest_guard()

    after = {name: _path_state(path) for name, path in watch.items()}
    check("provider_row_and_secrets_untouched",
          "var/personal.db and var/secrets are byte-identical (size/mtime) before and after the self-test",
          before == after, {"before": before, "after": after,
                            "paths": {name: str(path) for name, path in watch.items()}})

    evaluated = [case for case in cases if case.get("passed") is not None]
    failed = [case["case"] for case in evaluated if not case["passed"]]
    return {"kind": "campus-readonly-live-judge-self-test", "judge_version": JUDGE_VERSION,
            "checked_at": now_iso(), "passed": bool(evaluated) and not failed,
            "cases_passed": sum(1 for case in evaluated if case["passed"]),
            "cases_evaluated": len(evaluated), "cases_skipped": len(cases) - len(evaluated),
            "model_calls": 0, "provider_reads": 0, "credential_reads": 0,
            "failed": failed,
            "note": ("every case is an in-memory trajectory judged by the same deterministic rules; the "
                     "provider row and the secrets dir are redirected to a non-existent sentinel path for "
                     "the whole run and both accessors raise if called"),
            "cases": cases}


# ───────────────────────────────── output assembly ─────────────────────────────────

def _path_state(path: Path) -> dict:
    """Size/mtime only: this never opens the file, so it cannot itself touch the personal DB."""
    try:
        stat = Path(path).stat()
    except OSError:
        return {"exists": False}
    return {"exists": True, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


_SELFTEST_GUARD = {"active": False, "source_db": None, "secrets": None}


def _arm_selftest_guard() -> None:
    _SELFTEST_GUARD["active"] = True
    _SELFTEST_GUARD["source_db"] = live.SOURCE_DB
    _SELFTEST_GUARD["secrets"] = live.REAL_SECRETS
    live.SOURCE_DB = SELFTEST_SENTINEL / "no-personal-db"
    live.REAL_SECRETS = SELFTEST_SENTINEL / "no-secrets-dir"


def _disarm_selftest_guard() -> None:
    live.SOURCE_DB = _SELFTEST_GUARD["source_db"]
    live.REAL_SECRETS = _SELFTEST_GUARD["secrets"]
    _SELFTEST_GUARD["active"] = False


def provider_row() -> dict | None:
    if _SELFTEST_GUARD["active"]:
        raise AssertionError("offline self-test must not read the saved provider row")
    return live.saved_provider()


def credential_available(provider_id: str) -> bool:
    if _SELFTEST_GUARD["active"]:
        raise AssertionError("offline self-test must not touch var/secrets")
    return live.credential_present(provider_id)


def empty_document(label: str | None = None) -> dict:
    return {"kind": "harness-campus-readonly-live-model", "run_id": "campus-readonly-" + uuid.uuid4().hex[:12],
            "status": "not_run", "mode": "real-official-model/synthetic-school-readonly",
            "created_at": now_iso(), "updated_at": now_iso(), "model_live_flag_required": True,
            "evidence_set": label or "campus-readonly-m3",
            "budget": {"per_turn": dict(BUDGET), "max_recovery_turns": MAX_RECOVERY_TURNS,
                       "enforced_by": ["driver measurement", "CAMPUS_MAX_MODEL_CALLS",
                                       "CAMPUS_MAX_TOOL_CALLS", "CAMPUS_TURN_TIMEOUT_SECONDS"]},
            "tasks": {task: {"title": spec["title"], "prompt": spec["prompt"],
                             "prompt_sha256": sha256_text(spec["prompt"]), "criteria": spec["criteria"]}
                      for task, spec in TASKS.items()},
            "judge_version": JUDGE_VERSION, "judge_changelog": {str(key): value
                                                                for key, value in JUDGE_CHANGELOG.items()},
            "cost": COST, "cost_basis": COST_BASIS,
            "notes": list(NOTES_UNCONTROLLED),
            "claims": {"final_acceptance": False, "statistical_success_rate": False,
                       "real_school_validation": False,
                       "note": "four diagnostic real-model sessions on fixed synthetic school data"},
            "sessions": []}


def append_session(out_path: Path, record: dict) -> dict:
    """Never overwrite an earlier record: a later run appends under its own run_id."""
    document = live.load_json(out_path) if out_path.is_file() else empty_document()
    counter = {"redactions": 0}
    scrubbed = redact_tree(record, counter)
    scrubbed["redactions"] = counter.get("redactions", 0)
    document["sessions"] = [item for item in document.get("sessions", [])
                            if item.get("run_id") != scrubbed["run_id"]]
    document["sessions"].append(scrubbed)
    document["sessions"] = sorted(document["sessions"], key=lambda item: (item["task"], item["session_index"]))
    document["updated_at"] = now_iso()
    document["provider"] = scrubbed.get("provider")
    finalise(document)
    live.write_json(out_path, document)
    return document


def finalise(document: dict) -> None:
    document["judge_version"] = JUDGE_VERSION
    sessions = document.get("sessions") or []
    per_task = {}
    for task, spec in TASKS.items():
        subset = [item for item in sessions if item.get("task") == task]
        per_task[task] = {"title": spec["title"], "sessions": len(subset),
                          "goal_achieved": sum(1 for item in subset
                                               if (item.get("goal") or {}).get("achieved")),
                          "run_ids": [item.get("run_id") for item in subset],
                          "failed_criteria": sorted({name for item in subset
                                                     for name in (item.get("goal") or {}).get("failed_criteria") or []})}
    totals = {key: 0 for key in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens",
                                 "totalTokens")}
    for item in sessions:
        for key, value in ((item.get("usage") or {}).get("totals") or {}).items():
            totals[key] = totals.get(key, 0) + int(value or 0)
    document["aggregate"] = {
        "sessions": len(sessions),
        "sessions_achieved": sum(1 for item in sessions if (item.get("goal") or {}).get("achieved")),
        "per_task": per_task, "usage_totals": totals, "cost": COST,
        "note": "four independent sessions is a diagnostic sample; this is not a success rate",
    }
    document["status"] = ("not_run" if not sessions
                          else "recorded" if all((item.get("goal") or {}).get("achieved") for item in sessions)
                          else "recorded_with_failures")
    document["git"] = document.get("git") or git_facts()
    # Top-level convenience copies of the source revision facts the acceptance contract asks for.
    document["git_head"] = (document["git"] or {}).get("git_head")
    document["dirty_files"] = (document["git"] or {}).get("dirty_files")
    document.setdefault("notes", list(NOTES_UNCONTROLLED))
    document.setdefault("cost", COST)
    document.setdefault("cost_basis", COST_BASIS)
    document.setdefault("run_id", "campus-readonly-" + uuid.uuid4().hex[:12])
    document.setdefault("started_at", document.get("created_at") or now_iso())


def write_summary(document: dict, out_path: Path) -> Path:
    lines = ["# 校园只读四项真实模型会话（M3）", "",
             f"- run_id: `{document.get('run_id')}`",
             f"- 判定版本: judge v{JUDGE_VERSION}",
             f"- git HEAD: `{(document.get('git') or {}).get('git_head')}`",
             f"- 工作区脏文件: {(document.get('git') or {}).get('dirty_files_total')} 项"
             f"（截断: {(document.get('git') or {}).get('dirty_files_truncated')}）",
             f"- provider: `{(document.get('provider') or {}).get('id')}` / "
             f"`{(document.get('provider') or {}).get('model')}` / "
             f"`{(document.get('provider') or {}).get('protocol')}` / host "
             f"`{(document.get('provider') or {}).get('base_url_host')}`",
             f"- 每回合硬预算: 模型请求 {BUDGET['model_requests']} / 工具调用 {BUDGET['tool_calls']} / "
             f"{BUDGET['seconds']} 秒；恢复场景最多 {MAX_RECOVERY_TURNS} 回合，分别计量",
             f"- 金额: {COST}（{COST_BASIS}）", "",
             "## 会话结果", "",
             "| 任务 | 会话 | 状态 | 判定 | 未通过判据 | 模型请求 | 工具调用 | 秒 | 人工等待秒 | usage totalTokens | continuation |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for item in document.get("sessions") or []:
        goal = item.get("goal") or {}
        limits = item.get("limits") or {}
        recovery = item.get("auth_recovery") or {}
        lines.append("| {task} {title} | {rid} | {status} | {verdict} | {failed} | {model} | {tools} | {secs} | "
                     "{wait} | {tokens} | {mode} |".format(
                         task=item.get("task"), title=item.get("task_title"), rid=item.get("run_id"),
                         status=item.get("status"), verdict="通过" if goal.get("achieved") else "未通过",
                         failed="、".join(goal.get("failed_criteria") or []) or "—",
                         model=limits.get("model_requests"), tools=limits.get("tool_calls"),
                         secs=limits.get("seconds"),
                         wait=item.get("human_wait_seconds"),
                         tokens=((item.get("usage") or {}).get("totals") or {}).get("totalTokens"),
                         mode=recovery.get("continuation_mode") or "—"))
    lines += ["", "## 判据明细", ""]
    for item in document.get("sessions") or []:
        lines.append(f"### {item.get('task')} {item.get('task_title')} — {item.get('run_id')}")
        lines.append("")
        lines.append(f"- 会话: `{item.get('conversation_id')}`；状态 `{item.get('status')}`；"
                     f"driver_error: `{item.get('driver_error')}`")
        for turn in item.get("turns") or []:
            budget = turn.get("budget") or {}
            lines.append(f"- 回合 {turn.get('index')}: `{turn.get('turn_id')}` status `{turn.get('status')}` "
                         f"模型请求 {(budget.get('measured') or {}).get('model_requests')} / 工具调用 "
                         f"{(budget.get('measured') or {}).get('tool_calls')} / "
                         f"{(budget.get('measured') or {}).get('seconds')}s；答案 {turn.get('answer_chars')} 字")
        for name, detail in (item.get("goal") or {}).get("criteria", {}).items():
            lines.append(f"  - [{'x' if detail.get('passed') else ' '}] {name}")
        answer = (item.get("final_answer") or "").strip().replace("\n", " ")
        lines.append(f"- 最终回答（节选）: {answer[:300]}")
        lines.append("")
    lines += ["## 未受控因素与边界", ""]
    for note in NOTES_UNCONTROLLED:
        lines.append(f"- {note}")
    lines += ["", "## 原始证据", "",
              f"- JSON: `{out_path.as_posix()}`（如为相对路径则相对仓库根）",
              "- 每个会话的隔离目录见 `sessions[].scratch_home`（含 domain.sqlite、harness journal、"
              "会话 JSONL 副本、日志、合成适配器与种子脚本）", ""]
    path = out_path.with_name(out_path.stem + "-summary.md")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ───────────────────────────────── entry point ─────────────────────────────────

REFUSAL = """REFUSED: 真实模型验证需要显式 --model-live 开关。
本次没有读取 provider 配置、没有读取任何密钥文件、没有调用任何模型。
重新运行：
  apps/api/.venv/Scripts/python.exe -X utf8 tools/verify_campus_readonly_live.py --model-live --out {out}"""


def parse_tasks(value: str) -> list:
    items = [item.strip().upper() for item in str(value).replace(" ", ",").split(",") if item.strip()]
    unknown = [item for item in items if item not in TASKS]
    if unknown:
        raise ValueError("unknown tasks: " + ", ".join(unknown))
    ordered = [task for task in TASKS if task in items]
    return ordered or list(TASKS)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model-live", action="store_true",
                        help="explicitly authorize the paid real-model sessions on fixed synthetic school data")
    parser.add_argument("--tasks", default="T1,T2,T3,T4",
                        help="comma-separated subset, e.g. --tasks T1,T4 (default all four)")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--runs", type=int, default=1, help="sessions per task (this round fixes 1)")
    parser.add_argument("--scratch-root", type=Path, default=DEFAULT_SCRATCH_ROOT)
    parser.add_argument("--self-test", action="store_true",
                        help="offline judge regression; no provider row, no credential file, no model call")
    parser.add_argument("--rejudge", action="store_true",
                        help="offline re-judgment of already recorded sessions (no model call, never rewrites the originals)")
    parser.add_argument("--rejudge-input", action="append", type=Path, default=None,
                        help="recorded evidence document to re-judge (repeatable; defaults to this round's records)")
    parser.add_argument("--label", default=None, help="evidence-set label recorded in the document")
    args = parser.parse_args(argv)

    if args.rejudge:
        inputs = args.rejudge_input or list(REJUDGE_DEFAULT_INPUTS)
        report = rejudge_documents([path if path.is_absolute() else (ROOT / path) for path in inputs])
        out_path = args.out if args.out is not None else (REWORK_EVIDENCE_DIR / "m-judge" / "rejudge.json")
        out_path = out_path if out_path.is_absolute() else (ROOT / out_path)
        live.write_json(out_path, report)
        print("REJUDGE " + json.dumps({"judge_version": report["judge_version"],
                                       "model_calls": 0, "provider_reads": 0, "credential_reads": 0,
                                       "sources": [{"path": source["path"], "status": source["status"],
                                                    "recorded_judge_version": source.get("recorded_judge_version"),
                                                    "sessions": [{"task": item["task"],
                                                                  "original": item["original"],
                                                                  "rejudged": item["rejudged"],
                                                                  "changed": item["changed"]}
                                                                 for item in source.get("sessions", [])]}
                                                   for source in report["sources"]],
                                       "report": str(out_path.relative_to(ROOT).as_posix())},
                                      ensure_ascii=False))
        return 0

    if args.self_test:
        report = run_self_test()
        out_path = args.out if args.out is not None else EVIDENCE_DIR / "selftest.json"
        out_path = out_path if out_path.is_absolute() else (ROOT / out_path)
        live.write_json(out_path, report)
        print("SELF-TEST " + json.dumps({"judge_version": report["judge_version"], "passed": report["passed"],
                                         "cases_passed": report["cases_passed"],
                                         "cases_evaluated": report["cases_evaluated"],
                                         "failed": report["failed"], "model_calls": 0,
                                         "provider_reads": 0, "credential_reads": 0,
                                         "report": str(out_path.relative_to(ROOT).as_posix())},
                                        ensure_ascii=False))
        return 0 if report["passed"] else 1

    out_path = args.out if args.out is not None else DEFAULT_OUT
    out_path = out_path if out_path.is_absolute() else (ROOT / out_path)

    # The gate is checked BEFORE any provider row, credential file or model call (same precedent as
    # tools/verify_next_round_live.py:2509-2514).
    if not args.model_live:
        print(REFUSAL.format(out=out_path.as_posix()), file=sys.stderr)
        return 2
    if args.runs != 1:
        print("REFUSED: --runs must be 1 (this round fixes one independent session per task; never raise it).",
              file=sys.stderr)
        return 2
    try:
        tasks = parse_tasks(args.tasks)
    except ValueError as exc:
        print("REFUSED: " + str(exc), file=sys.stderr)
        return 2

    provider = provider_row()
    if provider is None:
        print(f"NOT RUN: saved provider {PROVIDER_ID} not found in var/personal.db (read-only)", file=sys.stderr)
        return 3
    if not provider.get("enabled") or provider.get("protocol") != "openai_chat_completions":
        print(f"NOT RUN: saved provider {PROVIDER_ID} is disabled or not chat-completions", file=sys.stderr)
        return 3
    host = str(provider.get("base_url") or "").split("/")[2] if "/" in str(provider.get("base_url") or "") else ""
    if host != OFFICIAL_PROVIDER_HOST:
        print(f"NOT RUN: configured provider host {host!r} is not the official {OFFICIAL_PROVIDER_HOST!r}; "
              "this half only meters the real official provider.", file=sys.stderr)
        return 3
    if not credential_available(provider["id"]):
        print("NOT RUN: no saved credential file var/secrets/provider-prv_c68d2878.key; this half cannot call a "
              "real model and never asks for a key on the command line.", file=sys.stderr)
        return 3

    scratch_root = args.scratch_root if args.scratch_root.is_absolute() else (ROOT / args.scratch_root)
    scratch = scratch_root / ("campus-readonly-live-" + uuid.uuid4().hex)
    scratch.mkdir(parents=True, exist_ok=True)
    run_started = time.monotonic()
    document = live.load_json(out_path) if out_path.is_file() else empty_document(args.label)
    document.setdefault("started_at", now_iso())
    if args.label:
        document["evidence_set"] = args.label
    document["provider"] = {"id": provider["id"], "name": provider["name"], "model": provider["model"],
                            "protocol": provider["protocol"], "base_url_host": host,
                            "credential": "saved DPAPI file reused read-only; never read, printed or archived"}
    document["git"] = git_facts()
    document["scratch_root"] = str(scratch.relative_to(ROOT).as_posix())
    finalise(document)
    live.write_json(out_path, document)

    recorded = {(item.get("task"), item.get("session_index")) for item in document.get("sessions") or []}
    print("EVIDENCE " + json.dumps({"out": out_path.as_posix(), "tasks": tasks,
                                    "already_recorded": sorted(f"{t}#{i}" for t, i in recorded)},
                                   ensure_ascii=False), flush=True)
    exit_code = 0
    for task in tasks:
        if (task, 1) in recorded:
            print(f"SKIP {task} #1 (already recorded; first results are preserved -- use a new --out to re-run)",
                  flush=True)
            continue
        print(f"RUN  {task} #1 ...", flush=True)
        record = execute_session(task, 1, scratch, provider)
        document = append_session(out_path, record)
        goal = (record.get("goal") or {})
        print("DONE {task} #1 status={status} goal={goal} model={model} tools={tools} seconds={secs} "
              "failed={failed}".format(task=task, status=record.get("status"), goal=goal.get("achieved"),
                                       model=(record.get("limits") or {}).get("model_requests"),
                                       tools=(record.get("limits") or {}).get("tool_calls"),
                                       secs=(record.get("limits") or {}).get("seconds"),
                                       failed="、".join(goal.get("failed_criteria") or []) or "-"), flush=True)
        if record.get("status") != "completed" or not goal.get("achieved"):
            exit_code = 1

    document = live.load_json(out_path)
    finalise(document)
    document["finished_at"] = now_iso()
    document["duration_seconds"] = round(time.monotonic() - run_started, 3)
    live.write_json(out_path, document)
    summary = write_summary(document, out_path)
    aggregate = document.get("aggregate") or {}
    print("AGGREGATE " + json.dumps({"sessions": aggregate.get("sessions"),
                                     "achieved": aggregate.get("sessions_achieved"),
                                     "per_task": {task: info["goal_achieved"]
                                                  for task, info in (aggregate.get("per_task") or {}).items()},
                                     "summary": str(summary.relative_to(ROOT).as_posix())},
                                    ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())



