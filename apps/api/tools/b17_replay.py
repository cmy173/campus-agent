"""B17 一次性回放工具：A21 冻结的真实 sync-result → B 消费链落库核验（收官证据工具）。

做什么：
    - 读取 A21 收官实测证据 JSON（默认 %LOCALAPPDATA%\\zjulearn\\controlled-browser\\a20-results）：
      a21-sync1 / a21-sync2（同一真实课程两次同步）+ a21-sync-bad（真实失败回放）；
    - 独立复算交叉验证 A21 文档的归一化指纹（递归剔除 cursor/observed_at → json sort_keys → sha256 前 16 位）；
    - 在独立回放库（默认 var/b17-replay.db，每次重建；不触碰 var/study.db）建最小场地：
      xzzd 连接 + 一门回放课程（Course.id=crs_a21，平台课号经 course source_ref.external_id 承载）；
    - 进程内注册回放适配器模拟 serve 语义：信封数据逐字节原样，仅改写 connection_id/course_id 为 B 侧值
      （serve 的适配器对输入 course_id 原样回传；冻结信封里的 connection_id=a20-cli 为 CLI 直驱占位）；
    - 经 TestClient 走 POST /connections/{id}/sync → 真 worker（process_next_job）消费，逐轮核对：
      R1 首轮落库（行/字段/cursor/observed_at/作业提交态/连接状态/last_sync_at）、
      R2 二次同步幂等（不重复落行、不升版、仅刷新 observed_at 与 cursor、since_cursor 透传）、
      R3 去抖与端点幂等（同 Idempotency-Key 响应重放；未完成任务的合并去抖，只产生一个 job）、
      R4 派生损坏单项隔离（kind 清空 → partial_failed + INVALID_PAYLOAD，其余条目照常落库）、
      R5 真实失败回放（RESOURCE_NOT_FOUND retryable=false，job failed，连接不被误标）；
    - 导出 D15 输入：GET /courses/{id}/resources|assignments|recordings 与 course 到
      var/b17-a21-export/（本地 gitignored，含真实 ID，不入提交物），附 manifest 与 replay-report。

边界：
    - 脚本源码与提交物不含任何真实课号/资源 ID（全部运行期从证据文件读取）；
    - 不触外网、不碰用户浏览器会话；回放适配器仅本工具进程可见；
    - 退出码：全部核对通过 0，否则 1（报告仍会写出）。

运行：
    cd apps/api && .venv/Scripts/python tools/b17_replay.py
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_INPUT_DIR = Path(os.environ.get("LOCALAPPDATA") or "") / "zjulearn" / "controlled-browser" / "a20-results"
FINGERPRINT_EXPECTED = "323afc775e093580"  # A21 文档 §3 的双次同步归一化指纹（本工具独立复算交叉验证）
CONNECTION_ID = "conn_xzzd_a21"
COURSE_ID = "crs_a21"
IDEM_KEY = "b17-r3-idem-0001"

CHECKS: list[dict] = []
FAILED: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> bool:
    CHECKS.append({"name": name, "ok": bool(ok), "detail": str(detail)})
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  | {detail}" if detail != "" else ""))
    if not ok:
        FAILED.append(name)
    return bool(ok)


def strip_volatile(value: object) -> object:
    """A21 §3 归一化口径：递归剔除 cursor 与 observed_at（含条目嵌套）。"""
    if isinstance(value, dict):
        return {k: strip_volatile(v) for k, v in value.items() if k not in ("cursor", "observed_at")}
    if isinstance(value, list):
        return [strip_volatile(v) for v in value]
    return value


def fingerprint(envelope: dict) -> str:
    data = json.dumps(strip_volatile(envelope), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pick_one(input_dir: Path, pattern: str, explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise SystemExit(f"指定的证据文件不存在：{path}")
        return path
    matches = sorted(input_dir.glob(pattern))
    if len(matches) != 1:
        raise SystemExit(f"在 {input_dir} 下期望恰一个 {pattern}，实际 {len(matches)} 个")
    return matches[0]


class ReplayAdapter:
    """回放适配器：按 mode 返回冻结信封或抛出冻结失败。

    mode=ok1/ok2：返回对应真实信封（仅改写 connection_id/course_id 为 B 侧值，模拟 serve 回传语义）；
    mode=mutated：在 ok1 基础上把首个资源的 kind 清空（派生损坏，验证单项隔离）；
    mode=bad：抛出记录中的真实失败（RESOURCE_NOT_FOUND，retryable=false）。
    """

    def __init__(self, envelopes: dict[str, dict], bad_error: dict):
        self.envelopes = envelopes
        self.bad_error = bad_error
        self.mode = "ok1"
        self.calls: list[dict] = []

    def __call__(self, connection: dict, request: dict) -> dict:
        self.calls.append(
            {
                "mode": self.mode,
                "connection": copy.deepcopy(connection),
                "request": copy.deepcopy(request),
            }
        )
        if self.mode == "bad":
            from app.sync_engine import AdapterError  # 惰性导入：调用时 app 模块已按回放库环境就绪

            err = self.bad_error
            raise AdapterError(
                str(err["code"]),
                str(err.get("message") or err["code"]),
                retryable=bool(err.get("retryable", True)),
                details=err.get("details") if isinstance(err.get("details"), dict) else None,
                action=err.get("action") if isinstance(err.get("action"), dict) else None,
            )
        env = copy.deepcopy(self.envelopes["ok1" if self.mode == "mutated" else self.mode])
        env["connection_id"] = connection["connection_id"]
        env["course_id"] = request["course_id"]
        if self.mode == "mutated":
            env["upserts"]["resources"][0]["kind"] = ""
        return env

    @property
    def last_call(self) -> dict:
        return self.calls[-1]


def norm_dt(value):
    from app.repo import normalize_dt

    return normalize_dt(value) if value is not None else None


def parse_dt(value):
    from app.sync_engine import as_datetime

    return as_datetime(value)


def post_sync(client, *, key: str | None = None, full: bool = False):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(f"/api/v1/connections/{CONNECTION_ID}/sync", json={"full": full}, headers=headers)


def drain_until(client, job_id: str, attempts: int = 100) -> str:
    """执行真 worker 直到目标任务离开 queued/running；返回终态。"""
    from app import worker
    from app.db import SessionLocal
    from app.models import Job

    for _ in range(attempts):
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            if job is None or job.status not in ("queued", "running"):
                return job.status if job is not None else "missing"
        assert worker.process_next_job(), "队列为空但目标任务未完成"
    return "timeout"


def job_state(client, job_id: str) -> dict:
    resp = client.get(f"/api/v1/jobs/{job_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def snapshot_rows() -> dict:
    """回放库内该课程的落库概览（行数、版本、资源版本数）。"""
    from sqlalchemy import func, select

    from app.db import SessionLocal
    from app.models import Assignment, Recording, Resource, ResourceVersion

    with SessionLocal() as db:
        resources = list(db.execute(select(Resource).where(Resource.course_id == COURSE_ID)).scalars())
        assignments = list(db.execute(select(Assignment).where(Assignment.course_id == COURSE_ID)).scalars())
        recordings = list(db.execute(select(Recording).where(Recording.course_id == COURSE_ID)).scalars())
        rv_count = db.execute(
            select(func.count()).select_from(ResourceVersion).where(
                ResourceVersion.resource_id.in_([r.id for r in resources] or [""])
            )
        ).scalar()
        return {
            "resources": resources,
            "assignments": assignments,
            "recordings": recordings,
            "resource_versions": int(rv_count or 0),
        }


def export_outputs(client, export_dir: Path, env1: dict, evidence: dict) -> dict:
    """GET 三条列表 + course 写入导出目录；返回 manifest。"""
    export_dir.mkdir(parents=True, exist_ok=True)
    targets = {
        "course.json": f"/api/v1/courses/{COURSE_ID}",
        "resources.json": f"/api/v1/courses/{COURSE_ID}/resources",
        "assignments.json": f"/api/v1/courses/{COURSE_ID}/assignments",
        "recordings.json": f"/api/v1/courses/{COURSE_ID}/recordings",
    }
    files: dict[str, dict] = {}
    for name, url in targets.items():
        resp = client.get(url)
        assert resp.status_code == 200, resp.text
        path = export_dir / name
        path.write_text(json.dumps(resp.json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        files[name] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}

    resources = json.loads((export_dir / "resources.json").read_text(encoding="utf-8"))["items"]
    assignments = json.loads((export_dir / "assignments.json").read_text(encoding="utf-8"))["items"]
    recordings = json.loads((export_dir / "recordings.json").read_text(encoding="utf-8"))["items"]

    from app.models import utc_now

    manifest = {
        "generated_at": utc_now().isoformat(),
        "purpose": "D15 排程输入（本地导出，含真实 ID，不入提交物）",
        "course": {"id": COURSE_ID, "platform": env1["platform"], "external_course_id": env1["course_id"]},
        "counts": {"resources": len(resources), "assignments": len(assignments), "recordings": len(recordings)},
        "evidence": evidence,
        "files": files,
    }
    (export_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="B17 A21 真实同步回放核验")
    ap.add_argument("--input-dir", default=str(DEFAULT_INPUT_DIR), help="A21 证据目录")
    ap.add_argument("--sync1-file", default=None, help="覆盖 a21-sync1-*.json 自动挑选")
    ap.add_argument("--sync2-file", default=None, help="覆盖 a21-sync2-*.json 自动挑选")
    ap.add_argument("--bad-file", default=None, help="覆盖 a21-sync-bad-*.json 自动挑选")
    ap.add_argument("--db", default=str(ROOT / "var" / "b17-replay.db"), help="回放库路径（每次重建）")
    ap.add_argument("--export-dir", default=str(ROOT / "var" / "b17-a21-export"), help="D15 输入导出目录")
    ap.add_argument("--expect-fingerprint", default=FINGERPRINT_EXPECTED, help="期望的归一化指纹")
    args = ap.parse_args(argv)

    input_dir = Path(args.input_dir)
    f1 = pick_one(input_dir, "a21-sync1-*.json", args.sync1_file)
    f2 = pick_one(input_dir, "a21-sync2-*.json", args.sync2_file)
    fb = pick_one(input_dir, "a21-sync-bad-*.json", args.bad_file)
    raw1 = json.loads(f1.read_text(encoding="utf-8"))
    raw2 = json.loads(f2.read_text(encoding="utf-8"))
    rawb = json.loads(fb.read_text(encoding="utf-8"))
    env1: dict = raw1["envelope"]
    env2: dict = raw2["envelope"]
    bad_error: dict = rawb["error"]

    # ── R0：证据完整性（指纹交叉验证）──
    fp1, fp2 = fingerprint(env1), fingerprint(env2)
    check("R0 指纹与 A21 文档一致", fp1 == args.expect_fingerprint, f"recomputed={fp1} expected={args.expect_fingerprint}")
    check("R0 sync1/sync2 归一化一致", fp1 == fp2, f"{fp1} / {fp2}")
    for tag, env in (("sync1", env1), ("sync2", env2)):
        upserted = sum(len(env["upserts"][k]) for k in ("resources", "assignments", "recordings"))
        check(f"R0 {tag} stats.upserted == 条目数", env["stats"]["upserted"] == upserted, f"{env['stats']['upserted']} == {upserted}")

    # ── 独立回放库与最小场地（不碰 var/study.db）──
    db_path = Path(args.db).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    os.environ["STUDY_DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ.pop("STUDY_WORKER_INPROC", None)
    os.environ.pop("STUDY_SYNC_ADAPTERS", None)

    from fastapi.testclient import TestClient
    from app import models  # noqa: F401  注册模型到 Base.metadata
    from app.db import Base, SessionLocal, engine
    from app.main import app
    from app.models import (
        OWNER_ASSIGNMENT,
        OWNER_RESOURCE,
        Assignment,
        Connection,
        Course,
        Job,
        Resource,
        SourceRef,
        SyncCursor,
        utc_now,
    )
    from app.repo import find_source_owner
    from app.sync_engine import AdapterError, register_sync_adapter

    platform = env1["platform"]
    orig_course_id = env1["course_id"]  # 平台课号：写入 course source_ref.external_id（serve 映射载体）
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        db.add(Connection(id=CONNECTION_ID, platform=platform, display_name="xzzd A21 replay", status="ok"))
        db.add(Course(id=COURSE_ID, title="A21 replay course (real sync-result)", term=None, teacher=None))
        db.add(
            SourceRef(
                owner_type="course",
                owner_id=COURSE_ID,
                platform=platform,
                external_id=orig_course_id,
                canonical_url=None,
                observed_at=utc_now(),
                seq=0,
            )
        )
        db.commit()

    adapter = ReplayAdapter({"ok1": env1, "ok2": env2}, bad_error)
    register_sync_adapter(platform, adapter)

    evidence = {
        "files": {f1.name: sha256_file(f1), f2.name: sha256_file(f2), fb.name: sha256_file(fb)},
        "fingerprint": fp1,
        "observed_at": env1["observed_at"],
        "stats": env1["stats"],
    }

    with TestClient(app) as client:
        # ── R1：首轮真实同步落库 ──
        adapter.mode = "ok1"
        resp = post_sync(client)
        check("R1 受理 202 queued", resp.status_code == 202 and resp.json()["job"]["status"] == "queued", resp.status_code)
        jid = resp.json()["job"]["id"]
        check("R1 worker 终态 succeeded", drain_until(client, jid) == "succeeded")
        job = job_state(client, jid)
        check("R1 result_refs=6（3 资源 + 3 作业）", len(job["result_refs"]) == 6, job["result_refs"])
        check("R1 任务无错误", job["errors"] == [], job["errors"])
        call = adapter.last_call
        check(
            "R1 适配器仅收连接上下文（无凭据）",
            call["connection"] == {"platform": platform, "connection_id": CONNECTION_ID},
            call["connection"],
        )
        check("R1 首轮 since_cursor 为空（全量 resync）", call["request"]["since_cursor"] is None)
        check("R1 请求含 B 侧 course_id", call["request"]["course_id"] == COURSE_ID)

        with SessionLocal() as db:
            conn = db.get(Connection, CONNECTION_ID)
            check("R1 连接状态 ok + last_sync_at 已写", conn.status == "ok" and conn.last_sync_at is not None)
            check("R1 连接无错误与恢复动作", conn.last_error is None and conn.recovery_action is None, conn.last_error)
            cursor = db.get(SyncCursor, (CONNECTION_ID, COURSE_ID))
            check(
                "R1 cursor 落库为信封 next",
                cursor is not None and cursor.cursor == env1["cursor"]["next"],
                cursor.cursor if cursor else None,
            )
            for item in env1["upserts"]["resources"]:
                ext = item["source_ref"]["external_id"]
                rid = find_source_owner(db, OWNER_RESOURCE, platform, ext)
                row = db.get(Resource, rid) if rid else None
                ok = (
                    row is not None
                    and row.course_id == COURSE_ID
                    and row.title == item["title"]
                    and row.kind == item["kind"]
                    and row.size_bytes == item["size_bytes"]
                    and row.access_level == item["access_level"]
                    and row.parse_status == "pending"
                    and row.mime_type == item.get("mime_type")
                    and row.content_hash == item.get("content_hash")
                    and norm_dt(row.source_updated_at) == parse_dt(item["source_updated_at"])
                )
                check(f"R1 资源落库字段一致（ext={ext}）", ok, f"title={row.title if row else None}")
                obs = _ref_observed(db, OWNER_RESOURCE, rid, platform, ext) if rid else None
                check(
                    f"R1 资源 source_ref.observed_at（ext={ext}）",
                    norm_dt(obs) == parse_dt(item["source_ref"]["observed_at"]),
                    obs,
                )
            for item in env1["upserts"]["assignments"]:
                ext = item["source_ref"]["external_id"]
                aid = find_source_owner(db, OWNER_ASSIGNMENT, platform, ext)
                row = db.get(Assignment, aid) if aid else None
                ok = (
                    row is not None
                    and row.course_id == COURSE_ID
                    and row.title == item["title"]
                    and row.submission_status == item["submission_status"]
                    and row.due_precision == item["due_precision"]
                    and norm_dt(row.due_at) == parse_dt(item["due_at"])
                    and norm_dt(row.observed_at) == parse_dt(item["source_ref"]["observed_at"])
                )
                check(f"R1 作业落库字段一致（ext={ext}）", ok, f"status={row.submission_status if row else None}")
            snap = snapshot_rows()
            check(
                "R1 行数 3 资源 / 3 作业 / 0 回放",
                len(snap["resources"]) == 3 and len(snap["assignments"]) == 3 and len(snap["recordings"]) == 0,
                {k: len(v) for k, v in snap.items() if isinstance(v, list)},
            )
            check("R1 首轮资源版本数=3（每资源 1 版）", snap["resource_versions"] == 3, snap["resource_versions"])
            statuses = sorted(a.submission_status for a in snap["assignments"])
            check("R1 作业提交态透传（1 not_submitted + 2 submitted）", statuses == ["not_submitted", "submitted", "submitted"], statuses)
            last_sync_1 = conn.last_sync_at

        # ── R2：二次真实同步（sync2）幂等 ──
        adapter.mode = "ok2"
        resp = post_sync(client)
        check("R2 受理 202", resp.status_code == 202, resp.status_code)
        jid2 = resp.json()["job"]["id"]
        check("R2 worker 终态 succeeded", drain_until(client, jid2) == "succeeded")
        job2 = job_state(client, jid2)
        check("R2 result_refs=6（重复观测全部命中已有行）", len(job2["result_refs"]) == 6, job2["result_refs"])
        check("R2 since_cursor 透传首轮游标", adapter.last_call["request"]["since_cursor"] == env1["cursor"]["next"], adapter.last_call["request"]["since_cursor"])
        with SessionLocal() as db:
            snap2 = snapshot_rows()
            check("R2 行数不变（幂等）", len(snap2["resources"]) == 3 and len(snap2["assignments"]) == 3 and len(snap2["recordings"]) == 0)
            check("R2 资源版本不升（指纹未变）", all(r.version == 1 for r in snap2["resources"]), [r.version for r in snap2["resources"]])
            check("R2 资源版本行数仍=3", snap2["resource_versions"] == 3, snap2["resource_versions"])
            cursor = db.get(SyncCursor, (CONNECTION_ID, COURSE_ID))
            check("R2 cursor 刷新为 sync2 next", cursor is not None and cursor.cursor == env2["cursor"]["next"])
            sample = snap2["resources"][0]
            ref_obs = _ref_observed(db, OWNER_RESOURCE, sample.id, platform, _ext_of(sample.id, db, platform, OWNER_RESOURCE))
            check(
                "R2 已有资源仅刷新 observed_at（sync2 值）",
                norm_dt(ref_obs) == parse_dt(env2["observed_at"]),
                ref_obs,
            )
            conn = db.get(Connection, CONNECTION_ID)
            check("R2 连接仍 ok 且 last_sync_at 前移", conn.status == "ok" and conn.last_sync_at > last_sync_1)

        # ── R3：去抖与端点幂等（不排空）──
        adapter.mode = "ok2"
        from sqlalchemy import func, select as sa_select

        with SessionLocal() as db:
            jobs_before = db.execute(sa_select(func.count()).select_from(Job).where(Job.type == "sync")).scalar()
        resp_a = post_sync(client, key=IDEM_KEY)
        jid3 = resp_a.json()["job"]["id"]
        check("R3 同 Idempotency-Key 响应重放（逐字节）", post_sync(client, key=IDEM_KEY).json() == resp_a.json())
        resp_c = post_sync(client)  # 无键：命中未完成任务的合并去抖
        check("R3 未完成任务合并去抖（同 job id）", resp_c.json()["job"]["id"] == jid3, resp_c.json()["job"]["id"])
        with SessionLocal() as db:
            jobs_after = db.execute(sa_select(func.count()).select_from(Job).where(Job.type == "sync")).scalar()
        check("R3 只产生一个 job", jobs_after == jobs_before + 1, f"{jobs_before} -> {jobs_after}")
        check("R3 worker 终态 succeeded", drain_until(client, jid3) == "succeeded")
        check("R3 完成后同键命中存储响应", post_sync(client, key=IDEM_KEY).json() == resp_a.json())
        with SessionLocal() as db:
            snap3 = snapshot_rows()
            check("R3 第三次消费行数与版本不变", len(snap3["resources"]) == 3 and all(r.version == 1 for r in snap3["resources"]))

        # ── R4：派生损坏 → partial_failed + 单项隔离 ──
        adapter.mode = "mutated"
        resp = post_sync(client)
        jid4 = resp.json()["job"]["id"]
        check("R4 worker 终态 partial_failed", drain_until(client, jid4) == "partial_failed")
        job4 = job_state(client, jid4)
        errs = job4["errors"]
        first = errs[0] if errs else {}
        check(
            "R4 单项错误 INVALID_PAYLOAD 且定位条目",
            len(errs) == 1 and first.get("code") == "INVALID_PAYLOAD" and first.get("details", {}).get("kind") == "resource",
            errs,
        )
        check(
            "R4 错误 external_id 指向被损坏条目",
            first.get("details", {}).get("external_id") == env1["upserts"]["resources"][0]["source_ref"]["external_id"],
            first.get("details"),
        )
        check("R4 其余条目照常落库（2 资源 + 3 作业 = 5 refs）", len(job4["result_refs"]) == 5, job4["result_refs"])
        with SessionLocal() as db:
            snap4 = snapshot_rows()
            check("R4 行数不变（损坏条目未落新行）", len(snap4["resources"]) == 3 and len(snap4["assignments"]) == 3)

        # ── R5：真实失败回放（RESOURCE_NOT_FOUND）──
        with SessionLocal() as db:
            conn = db.get(Connection, CONNECTION_ID)
            last_sync_before_r5 = conn.last_sync_at
        adapter.mode = "bad"
        resp = post_sync(client)
        jid5 = resp.json()["job"]["id"]
        check("R5 worker 终态 failed", drain_until(client, jid5) == "failed")
        job5 = job_state(client, jid5)
        e5 = job5["errors"][0] if job5["errors"] else {}
        check(
            "R5 错误码/可重试/HTTP 状态透传",
            e5.get("code") == bad_error["code"]
            and e5.get("retryable") == bad_error["retryable"]
            and e5.get("details", {}).get("http_status") == bad_error["details"]["http_status"],
            e5,
        )
        check("R5 错误详情带 course_id", e5.get("details", {}).get("course_id") == COURSE_ID)
        check("R5 无结果引用", job5["result_refs"] == [])
        with SessionLocal() as db:
            conn = db.get(Connection, CONNECTION_ID)
            check(
                "R5 课程级失败不误标连接",
                conn.status == "ok" and conn.last_error is None and conn.last_sync_at == last_sync_before_r5,
                f"status={conn.status}",
            )

        # ── 导出 D15 输入（GET 面）──
        export_dir = Path(args.export_dir).resolve()
        manifest = export_outputs(client, export_dir, env1, evidence)
        check(
            "EXPORT 计数与信封一致",
            manifest["counts"] == {"resources": 3, "assignments": 3, "recordings": 0},
            manifest["counts"],
        )
        exported = json.loads((export_dir / "resources.json").read_text(encoding="utf-8"))["items"]
        check(
            "EXPORT 资源含 source_refs 且平台一致",
            all(i["source_refs"] and i["source_refs"][0]["platform"] == platform for i in exported),
        )
        exported_asg = json.loads((export_dir / "assignments.json").read_text(encoding="utf-8"))["items"]
        check(
            "EXPORT 作业提交态完整",
            sorted(a["submission_status"] for a in exported_asg) == ["not_submitted", "submitted", "submitted"],
        )

    # ── 报告 ──
    export_dir = Path(args.export_dir).resolve()
    report = {
        "tool": "b17_replay",
        "generated_at": utc_now().isoformat(),
        "evidence_files": evidence["files"],
        "fingerprint": fp1,
        "db": str(db_path),
        "rounds": ["R0 证据完整性", "R1 首轮落库", "R2 幂等", "R3 去抖/端点幂等", "R4 单项隔离", "R5 真实失败"],
        "adapter_calls": adapter.calls,
        "checks": CHECKS,
        "failed": FAILED,
        "ok": not FAILED,
    }
    export_dir.mkdir(parents=True, exist_ok=True)
    (export_dir / "_b17-replay-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print()
    print(f"共 {len(CHECKS)} 项核对，{'全部通过' if not FAILED else f'失败 {len(FAILED)} 项：' + '; '.join(FAILED)}")
    print(f"回放库：{db_path}")
    print(f"导出目录（含报告）：{export_dir}")
    return 0 if not FAILED else 1


def _ref_observed(db, owner_type: str, owner_id: str, platform: str, external_id: str):
    from sqlalchemy import select

    from app.models import SourceRef

    return db.execute(
        select(SourceRef.observed_at).where(
            SourceRef.owner_type == owner_type,
            SourceRef.owner_id == owner_id,
            SourceRef.platform == platform,
            SourceRef.external_id == external_id,
        )
    ).scalars().first()


def _ext_of(owner_id: str, db, platform: str, owner_type: str) -> str:
    from sqlalchemy import select

    from app.models import SourceRef

    return db.execute(
        select(SourceRef.external_id).where(
            SourceRef.owner_type == owner_type, SourceRef.owner_id == owner_id, SourceRef.platform == platform
        )
    ).scalars().first()


if __name__ == "__main__":
    raise SystemExit(main())
