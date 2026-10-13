"""X3 一次性工具：A22 真实 ZIP 课件包 → /imports 落库核验（收尾轮证据工具）。

做什么：
    - 读取 A22 收官实测证据（默认 %LOCALAPPDATA%\\zjulearn\\controlled-browser\\a20-results 下
      a22-fetch*.json）与下载件目录（默认同根下 downloads\\），运行期建立 ZIP ↔ 真实资源映射；
    - 独立复算下载件 sha256，与证据 handle.sha256/size 交叉验证（fetch1/fetch1b 双抓一致）；
    - 在 B17 回放库副本（默认 var/b17-replay.db → var/x3-zip-import.db）上经 TestClient 依次发
      3 次 POST /imports（各带 source_ref，走 A9 挂接通道），真 worker（process_next_job）消费；
    - 逐件核对（CR006 口径：可入库归档、解析终态 skipped、不伪造解析结果）：
      import succeeded 且 result_refs 指向【既有】资源（不重复建）；资源 v1→v2（v1 保留 file_id=null）；
      v2 落 file_id/content_hash/mime=application/zip/size，parse_status=skipped，access_level=full；
      parse job failed + UNSUPPORTED_MEDIA_TYPE + retryable=false；0 ContentChunk；
      GET /resources 派生 chunk_count=null 且 source_refs 保留；GET /files 字节与下载件一致；
      课程资源/作业总数不变。
    - 报告写 var/x3-zip-import/_x3-report.json（本地 gitignored，含真实 ID，不入提交物）。

边界：
    - 脚本源码与提交物不含任何真实课号/资源 ID（全部运行期从证据与文件名读取）；
    - 不触外网；本工具产生的文件落在 var/x3-zip-import-files/（STUDY_FILES_DIR 重定向）；
    - 退出码：全部核对通过 0，否则 1（报告仍会写出）。

运行：
    cd apps/api && .venv/Scripts/python tools/x3_zip_import.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LOCAL = Path(os.environ.get("LOCALAPPDATA") or "")
DEFAULT_EVIDENCE_DIR = LOCAL / "zjulearn" / "controlled-browser" / "a20-results"
DEFAULT_DOWNLOADS_DIR = LOCAL / "zjulearn" / "controlled-browser" / "downloads"
COURSE_ID = "crs_a21"  # B17 回放库内的 B 侧课程 ID（非平台真实 ID）
REF_NAME_RE = re.compile(r"^xzzd-(\d+)\.zip$")

CHECKS: list[dict] = []
FAILED: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> bool:
    CHECKS.append({"name": name, "ok": bool(ok), "detail": str(detail)})
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  | {detail}" if detail != "" else ""))
    if not ok:
        FAILED.append(name)
    return bool(ok)


def sha256_hex(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def load_a22_evidence(evidence_dir: Path) -> dict[str, dict]:
    """A22 fetch 证据 → {handle.ref: {resource_id, sha256(带前缀原文), size, files[]}}（多次抓取归并）。"""
    evidence: dict[str, dict] = {}
    for path in sorted(evidence_dir.glob("a22-fetch*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        handle = (raw.get("outcome") or {}).get("handle") or {}
        ref = handle.get("ref")
        if not ref or not handle.get("sha256"):
            continue
        entry = evidence.setdefault(
            ref,
            {"resource_id": raw.get("resource_id"), "sha256": set(), "size": set(), "files": []},
        )
        entry["sha256"].add(handle["sha256"])
        entry["size"].add(handle.get("size"))
        entry["files"].append(path.name)
    return evidence


def drain_until(client, job_id: str, attempts: int = 200) -> str:
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


def queue_drained() -> bool:
    """队列里是否还有 queued/running 任务（import/parse）。"""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Job

    with SessionLocal() as db:
        rows = db.execute(
            select(Job.id).where(Job.type.in_(("import", "parse")), Job.status.in_(("queued", "running")))
        ).scalars().all()
    return not rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="X3 A22 真实 ZIP 经 /imports 落库核验（CR006 口径）")
    ap.add_argument("--db", default=str(ROOT / "var" / "b17-replay.db"), help="B17 回放库（源，只读复制）")
    ap.add_argument("--work-db", default=str(ROOT / "var" / "x3-zip-import.db"), help="本工具副本库（每次重建）")
    ap.add_argument("--files-dir", default=str(ROOT / "var" / "x3-zip-import-files"), help="受控文件目录（重定向）")
    ap.add_argument("--evidence-dir", default=str(DEFAULT_EVIDENCE_DIR), help="A22 证据目录")
    ap.add_argument("--downloads-dir", default=str(DEFAULT_DOWNLOADS_DIR), help="A22 下载件目录")
    ap.add_argument("--report-dir", default=str(ROOT / "var" / "x3-zip-import"), help="报告输出目录")
    args = ap.parse_args(argv)

    src_db, work_db = Path(args.db).resolve(), Path(args.work_db).resolve()
    files_dir, report_dir = Path(args.files_dir).resolve(), Path(args.report_dir).resolve()
    if not src_db.is_file():
        raise SystemExit(f"B17 回放库不存在：{src_db}（先运行 tools/b17_replay.py）")

    # ── 证据与下载件（运行期真实 ID 不经源码）──
    evidence = load_a22_evidence(Path(args.evidence_dir))
    downloads = {p.name: p for p in sorted(Path(args.downloads_dir).glob("xzzd-*.zip"))}
    check("P0 A22 证据含 3 个 zip（fetch1/fetch1b/fetch2/fetch3 归并）", len(evidence) == 3, sorted(evidence))
    check("P0 下载件 3 个", len(downloads) == 3, sorted(downloads))

    items: list[dict] = []
    for ref, ev in sorted(evidence.items()):
        m = REF_NAME_RE.match(ref)
        path = downloads.get(ref)
        if not m or path is None:
            check(f"P0 证据 {ref} 有对应下载件", False, f"matches={bool(m)} file={path}")
            continue
        data = path.read_bytes()
        sha_hex = sha256_hex(data)
        check(
            f"P1 {ref} 独立复算 sha 与证据一致（含双抓一致）",
            ev["sha256"] == {f"sha256:{sha_hex}"},
            f"evidence={sorted(ev['sha256'])} recomputed=sha256:{sha_hex}",
        )
        check(f"P1 {ref} 字节数与证据一致", ev["size"] == {len(data)}, f"{sorted(ev['size'])} / {len(data)}")
        items.append({"ref": ref, "path": path, "data": data, "sha256": f"sha256:{sha_hex}", "ext": m.group(1)})

    # ── 副本库与环境（不碰回放库本体、不碰 var/study.db）──
    work_db.parent.mkdir(parents=True, exist_ok=True)
    if work_db.exists():
        work_db.unlink()
    shutil.copyfile(src_db, work_db)
    if files_dir.exists():
        shutil.rmtree(files_dir)
    files_dir.mkdir(parents=True, exist_ok=True)
    os.environ["STUDY_DATABASE_URL"] = f"sqlite:///{work_db.as_posix()}"
    os.environ["STUDY_FILES_DIR"] = str(files_dir)
    os.environ.pop("STUDY_WORKER_INPROC", None)
    os.environ.pop("STUDY_SYNC_ADAPTERS", None)

    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from app import models  # noqa: F401  注册模型到 Base.metadata
    from app.db import Base, SessionLocal, engine
    from app.main import app
    from app.models import (
        OWNER_RESOURCE,
        Assignment,
        ContentChunk,
        FileRecord,
        Job,
        Resource,
        ResourceVersion,
        SourceRef,
        utc_now,
    )
    from app.repo import find_source_owner

    Base.metadata.create_all(engine)

    # ── 前置快照 ──
    with SessionLocal() as db:
        resources = list(db.execute(select(Resource).where(Resource.course_id == COURSE_ID)).scalars())
        assignments = list(db.execute(select(Assignment).where(Assignment.course_id == COURSE_ID)).scalars())
        by_ext = {}
        for r in resources:
            ext = db.execute(
                select(SourceRef.external_id).where(
                    SourceRef.owner_type == OWNER_RESOURCE, SourceRef.owner_id == r.id
                )
            ).scalars().first()
            by_ext[ext] = r.id
        platform = db.execute(select(SourceRef.platform).where(SourceRef.owner_type == OWNER_RESOURCE)).scalars().first()
    check(
        "P2 回放库前置：3 资源 / 3 作业",
        len(resources) == 3 and len(assignments) == 3,
        {"resources": len(resources), "assignments": len(assignments)},
    )
    check(
        "P2 回放库前置：资源均 file_id=null / parse_status=pending / version=1",
        all(r.file_id is None and r.parse_status == "pending" and r.version == 1 for r in resources),
    )
    check(
        "P2 证据资源与回放库 source_ref 全部对上（3/3）",
        all(it["ext"] in by_ext for it in items),
        {it["ext"]: by_ext.get(it["ext"]) for it in items},
    )

    report_items: list[dict] = []
    with TestClient(app) as client:
        for it in items:
            rid = by_ext.get(it["ext"])
            if rid is None:
                check(f"I {it['ref']} 已有资源可挂接", False, it["ext"])
                continue
            before_count = None
            with SessionLocal() as db:
                before_count = db.execute(select(func.count()).select_from(Resource)).scalar()

            payload = {
                "course_id": COURSE_ID,
                "kind": "other",
                "title": None,
                "source_ref": {
                    "platform": platform,
                    "external_id": it["ext"],
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                },
            }
            resp = client.post(
                "/api/v1/imports",
                data={"payload": json.dumps(payload, ensure_ascii=False)},
                files=[("files", (it["ref"], it["data"], "application/zip"))],
            )
            check(f"I1 {it['ref']} 受理 202 queued", resp.status_code == 202 and resp.json()["job"]["status"] == "queued", resp.status_code)
            job_id = resp.json()["job"]["id"]
            check(f"I2 {it['ref']} import worker 终态 succeeded", drain_until(client, job_id) == "succeeded")
            job = job_state(client, job_id)
            check(
                f"I2 {it['ref']} result_refs 指向既有资源（不重复建）",
                job["result_refs"] == [{"type": "resource", "id": rid}],
                job["result_refs"],
            )
            check(f"I2 {it['ref']} 无错误", job["errors"] == [], job["errors"])

            # parse job（import 收尾自动派发）: drain 后核对
            with SessionLocal() as db:
                parse_job_ids = [
                    j.id
                    for j in db.execute(select(Job).where(Job.type == "parse")).scalars()
                    if (j.payload or {}).get("resource_id") == rid and (j.payload or {}).get("version") == 2
                ]
            check(f"I3 {it['ref']} import 自动派发 parse（资源,version=2）", len(parse_job_ids) == 1, parse_job_ids)
            if parse_job_ids:
                parse_job_id = parse_job_ids[0]
                check(f"I3 {it['ref']} parse worker 终态 failed", drain_until(client, parse_job_id) == "failed")
                pjob = job_state(client, parse_job_id)
                e0 = pjob["errors"][0] if pjob["errors"] else {}
                check(
                    f"I3 {it['ref']} parse 错误码 UNSUPPORTED_MEDIA_TYPE 且不可重试（CR006：不伪造解析）",
                    e0.get("code") == "UNSUPPORTED_MEDIA_TYPE" and e0.get("retryable") is False,
                    e0,
                )

            with SessionLocal() as db:
                r = db.get(Resource, rid)
                check(
                    f"I4 {it['ref']} 资源 v1→v2 + 归档字段落库",
                    r.version == 2
                    and r.file_id is not None
                    and r.content_hash == it["sha256"]
                    and r.mime_type == "application/zip"
                    and r.size_bytes == len(it["data"])
                    and r.access_level == "full"
                    and r.parse_status == "skipped",
                    {"version": r.version, "parse_status": r.parse_status, "mime": r.mime_type},
                )
                versions = {
                    v.version: v
                    for v in db.execute(
                        select(ResourceVersion).where(ResourceVersion.resource_id == rid)
                    ).scalars()
                }
                check(
                    f"I4 {it['ref']} 版本链 v1(保留 file_id=null) + v2(file_id 已设, skipped)",
                    set(versions) == {1, 2}
                    and versions[1].file_id is None
                    and versions[2].file_id == r.file_id
                    and versions[2].parse_status == "skipped"
                    and versions[2].content_hash == it["sha256"],
                    {v: (versions[v].file_id, versions[v].parse_status) for v in sorted(versions)},
                )
                record = db.get(FileRecord, r.file_id)
                check(
                    f"I4 {it['ref']} FileRecord 指纹与 A22 证据一致",
                    record is not None and record.content_hash == it["sha256"] and record.size_bytes == len(it["data"]),
                )
                chunks = db.execute(select(func.count()).select_from(ContentChunk).where(ContentChunk.resource_id == rid)).scalar()
                check(f"I5 {it['ref']} 0 ContentChunk（不伪造解析产物）", chunks == 0, chunks)
                after_count = db.execute(select(func.count()).select_from(Resource)).scalar()
                check(f"I5 {it['ref']} 课程资源总数不变（3）", after_count == before_count == 3, f"{before_count}->{after_count}")
                file_id = r.file_id

            resp = client.get(f"/api/v1/resources/{rid}")
            body = resp.json()
            check(
                f"I6 {it['ref']} GET /resources：skipped + chunk_count=null + source_refs 保留",
                resp.status_code == 200
                and body["parse_status"] == "skipped"
                and body["chunk_count"] is None
                and any(s.get("external_id") == it["ext"] for s in body.get("source_refs", [])),
                {k: body.get(k) for k in ("parse_status", "chunk_count", "version")},
            )
            resp = client.get(f"/api/v1/files/{file_id}")
            check(
                f"I7 {it['ref']} GET /files 字节与下载件一致",
                resp.status_code == 200 and resp.content == it["data"],
                f"{resp.status_code} {len(resp.content)}B",
            )
            report_items.append(
                {
                    "ref": it["ref"],
                    "external_id": it["ext"],
                    "resource_id": rid,
                    "file_id": file_id,
                    "bytes": len(it["data"]),
                    "sha256": it["sha256"],
                    "import_job": job_id,
                    "parse_job": parse_job_id if parse_job_ids else None,
                }
            )

        check("Z1 队列已排空（无 queued/running import/parse）", queue_drained())

    # ── 报告 ──
    report = {
        "tool": "x3_zip_import",
        "generated_at": utc_now().isoformat(),
        "purpose": "A22 真实 ZIP → /imports 落库核验（CR006：可入库归档、解析 skipped、不伪造）",
        "course_id": COURSE_ID,
        "work_db": str(work_db),
        "files_dir": str(files_dir),
        "items": report_items,
        "checks": CHECKS,
        "failed": FAILED,
        "ok": not FAILED,
    }
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "_x3-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print()
    print(f"共 {len(CHECKS)} 项核对，{'全部通过' if not FAILED else f'失败 {len(FAILED)} 项：' + '; '.join(FAILED)}")
    print(f"副本库：{work_db}")
    print(f"报告：{report_dir / '_x3-report.json'}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
