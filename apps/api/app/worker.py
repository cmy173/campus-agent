"""后台 worker 与 job 状态机。

单进程轮询 jobs 表（阶段 1 本地 SQLite：启动时假设无并发 worker）：

- 抢占：把最早的 queued 任务原子置为 running 并提交（独立短事务）；
- 执行：按 job.type 查处理器注册表，处理器分条目提交，进度/产物实时可见；
- 协作式取消：处理器在条目边界调用 check_cancel，cancel_requested 时抛 JobCancelled；
- 崩溃恢复：worker 启动时把遗留的 running 重置回 queued（requeue_stale），
  重放由处理器保证幂等（如导入按 file_id 去重），任务不会永久卡在 running。

独立进程运行：``python -m app.worker``；
或 ``STUDY_WORKER_INPROC=1`` 在 API 进程内启动守护线程（本地开发便捷模式）。
"""
from __future__ import annotations

import json
import copy
import hashlib
import base64
import logging
import os
import threading
import time
import zipfile
from pathlib import PurePosixPath
from dataclasses import dataclass, field
from typing import Any, Callable

from learning.parse import (
    ParseContext,
    ParseErrorCode,
    ParseLimits,
    ParseStatus,
    parse_document,
)
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from . import config, providers, review_deps, review_engine, secrets_store, storage
from .build_engine import BuildOutcome, enqueue_build_tasks_job, run_build_tasks
from .db import SessionLocal
from .errors import ApiError
from .ics import Event, build_ics
from .plan_engine import active_plan, resolve_user_tz
from .models import (
    OWNER_ASSIGNMENT,
    OWNER_COURSE,
    OWNER_RECORDING,
    OWNER_RESOURCE,
    OWNER_TASK,
    Assignment,
    Availability,
    Connection,
    ContentChunk,
    Course,
    FileRecord,
    Job,
    LearningTask,
    Plan,
    ProviderConfig,
    Recording,
    Resource,
    ResourceVersion,
    ReviewMaterialSnapshot,
    ReviewPaper,
    ReviewPaperRevision,
    SourceRef,
    TaskFeedback,
    iso_utc,
    utc_now,
)
from .repo import find_source_owner, new_id, refs_for
from .sync_engine import (
    AdapterError,
    AdapterMissing,
    SyncResultInvalid,
    adapter_error_entry,
    apply_sync_result,
    as_datetime,
    bound_courses,
    get_cursor,
    mark_auth_required,
    mark_connection_ok,
    mark_network_error,
    resolve_sync_adapter,
    _translate_protocol_error,
    store_cursor,
)

# Single source for the attachment size cap: the connector enforces the same value (PD-1),
# so the worker must classify an oversized attachment with it rather than a different one.
from connectors.adapters.xzzd import MAX_DOWNLOAD_BYTES  # noqa: E402

log = logging.getLogger("study.worker")


# ────────────────────────── 状态机契约 ──────────────────────────


class JobCancelled(Exception):
    """处理器在取消检查点主动中止。"""


class JobFailed(Exception):
    """预期的任务失败：可读错误 + 是否可重试。"""

    def __init__(self, code: str, message: str, *, retryable: bool = True, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details

    def to_error(self) -> dict[str, Any]:
        return job_error(self.code, self.message, retryable=self.retryable, details=self.details)


@dataclass
class JobResult:
    """处理器收尾结果；status 仅允许 succeeded / partial_failed（failed 用 JobFailed 抛出）。"""

    status: str = "succeeded"
    errors: list[dict] = field(default_factory=list)
    result_refs: list[dict] | None = None  # None=沿用处理器增量写入的 result_refs


def job_error(code: str, message: str, *, retryable: bool = True, details: dict | None = None) -> dict[str, Any]:
    """构造契约 error.schema.json#/$defs/ErrorInfo 条目（Job.errors 元素）。"""
    entry: dict[str, Any] = {"code": code, "message": message, "retryable": retryable}
    if details is not None:
        entry["details"] = details
    return entry


HANDLERS: dict[str, Callable[[Session, Job], JobResult]] = {}


def handler(job_type: str) -> Callable:
    def register(fn: Callable[[Session, Job], JobResult]) -> Callable[[Session, Job], JobResult]:
        HANDLERS[job_type] = fn
        return fn

    return register


def check_cancel(db: Session, job: Job) -> None:
    """在条目边界检查取消请求；调用前应先提交已完成条目的写入。"""
    db.refresh(job)
    if job.cancel_requested:
        raise JobCancelled(job.id)


# ────────────────────────── 队列与生命周期 ──────────────────────────


def requeue_stale(db: Session) -> int:
    """启动恢复：遗留 running 重置回 queued（单 worker 假设；params 保留在 payload）。"""
    result = db.execute(
        update(Job)
        .where(Job.status == "running")
        .values(status="queued", started_at=None, finished_at=None, progress=0.0, errors=[], warnings=[])
    )
    db.commit()
    recovered = result.rowcount or 0
    # A successful check may have committed before an interrupted worker could
    # reach its terminal state/hook. Resume its durable auth checkpoint on restart.
    for connection in db.execute(select(Connection).where(Connection.status == "ok")).scalars():
        recovered += resume_auth_sync_jobs(db, connection)
    return recovered


def _claim_next(db: Session) -> Job | None:
    job_id = db.execute(
        select(Job.id).where(Job.status == "queued").order_by(Job.created_at.asc(), Job.id.asc()).limit(1)
    ).scalar_one_or_none()
    if job_id is None:
        return None
    claimed = db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == "queued")
        .values(status="running", started_at=utc_now(), finished_at=None, progress=0.0, errors=[], warnings=[])
    )
    db.commit()
    if claimed.rowcount == 0:
        return None
    return db.get(Job, job_id)


def process_job(db: Session, job: Job) -> None:
    """执行单个已抢占（running）的任务并落终态。"""
    log.info("job %s(%s) 开始", job.id, job.type)
    if job.cancel_requested:
        # 抢占后立即检查：排队期/崩溃恢复前已请求取消的任务直接落终态，不执行处理器
        log.info("job %s 开跑前已处于取消请求状态", job.id)
        _finish(db, job, "cancelled", list(job.errors or []))
        return
    fn = HANDLERS.get(job.type)
    if fn is None:
        _finish(db, job, "failed", [job_error("NO_HANDLER", f"未注册的任务类型：{job.type}", retryable=False)])
        return
    try:
        result = fn(db, job)
    except JobCancelled:
        db.rollback()
        log.info("job %s 已取消", job.id)
        _finish(db, job, "cancelled", list(job.errors or []))
    except JobFailed as exc:
        db.rollback()
        log.warning("job %s 失败：%s", job.id, exc.message)
        _finish(db, job, "failed", [exc.to_error()])
    except Exception as exc:  # noqa: BLE001 —— 未预期异常：落可读错误与诊断信息，任务可重试
        db.rollback()
        log.exception("job %s 意外异常", job.id)
        _finish(
            db,
            job,
            "failed",
            [job_error("INTERNAL", f"处理异常：{type(exc).__name__}: {exc}", details={"job_id": job.id})],
        )
    else:
        log.info("job %s %s", job.id, result.status)
        _finish(db, job, result.status, result.errors, result.result_refs)


def _finish(db: Session, job: Job, status: str, errors: list[dict], result_refs: list[dict] | None = None) -> None:
    fresh = db.get(Job, job.id)
    if fresh is None:
        return
    fresh.status = status
    fresh.errors = list(errors or [])
    if result_refs is not None:
        fresh.result_refs = list(result_refs)
    fresh.finished_at = utc_now()
    if status in ("succeeded", "partial_failed"):
        fresh.progress = 1.0
    db.commit()
    if fresh.type == "sync" and status in ("failed", "partial_failed") and (fresh.payload or {}).get("auth_required"):
        # Check may have committed after the AUTH checkpoint but before _finish.
        connection = db.get(Connection, fresh.payload.get("connection_id"))
        if connection is not None:
            resume_auth_sync_jobs(db, connection)


def process_next_job() -> bool:
    """取一个排队任务执行；队列为空返回 False。"""
    with SessionLocal() as db:
        job = _claim_next(db)
        if job is None:
            return False
        process_job(db, job)
        return True


def run_forever(*, poll_interval: float | None = None, stop: threading.Event | None = None) -> None:
    interval = poll_interval if poll_interval is not None else config.WORKER_POLL_SECONDS
    with SessionLocal() as db:
        recovered = requeue_stale(db)
    if recovered:
        log.warning("启动恢复：%d 个中断任务重置为 queued", recovered)
    log.info("worker 启动，轮询间隔 %.1fs", interval)
    while not (stop is not None and stop.is_set()):
        try:
            busy = process_next_job()
        except Exception:  # noqa: BLE001 —— 轮询层兜底，异常不终止 worker
            log.exception("worker 轮询异常，继续运行")
            busy = False
        if busy:
            continue
        if stop is not None:
            if stop.wait(interval):
                break
        else:
            time.sleep(interval)
    log.info("worker 退出")


def start_inproc_if_enabled() -> threading.Event | None:
    """STUDY_WORKER_INPROC=1 时在 API 进程内启动守护线程 worker（本地开发便捷模式）。"""
    if os.environ.get("STUDY_WORKER_INPROC", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return None
    stop = threading.Event()
    threading.Thread(target=run_forever, kwargs={"stop": stop}, name="study-worker", daemon=True).start()
    return stop


# ────────────────────────── 处理器：手动导入 ──────────────────────────


class _ItemError(Exception):
    """单条目可预期失败（计入 errors[]，不终止整体任务）。"""

    def __init__(self, code: str, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@handler("import")
def handle_import(db: Session, job: Job) -> JobResult:
    """手动导入落库：文件记录 → 资源（+ 版本、来源引用）；按 file_id 幂等去重。

    带 source_ref 的导入是 A→B 上传通道（A9）：同 (platform, external_id) 已存在资源时
    挂接新版本（内容变化）或补齐文件句柄（同指纹），而不是重复建资源。
    收尾时为待解析（parse_status=pending）的资源自动派发 parse 任务（同资源同版本去抖）。
    """
    payload = job.payload or {}
    course_id = payload.get("course_id")
    kind = payload.get("kind")
    base_title = payload.get("title")
    source_ref = payload.get("source_ref")
    files = payload.get("files") or []

    if db.get(Course, course_id) is None:
        raise JobFailed("INVALID_PAYLOAD", f"课程不存在：{course_id}", retryable=False)
    if not files:
        raise JobFailed("INVALID_PAYLOAD", "导入任务缺少文件列表", retryable=False)

    errors: list[dict] = []
    refs: list[dict] = []
    total = len(files)
    for idx, spec in enumerate(files):
        check_cancel(db, job)
        file_id = spec.get("file_id")
        resource: Resource | None = None
        try:
            if not isinstance(file_id, str) or not file_id.startswith("fil_"):
                raise _ItemError("INVALID_PAYLOAD", f"文件标识不合法：{file_id!r}", retryable=False)
            record = db.get(FileRecord, file_id)
            if record is None:
                raise _ItemError("NOT_FOUND", f"文件记录不存在：{file_id}")
            candidates = select(Resource).where(Resource.file_id == file_id, Resource.course_id == course_id,
                                                 Resource.kind == kind)
            if base_title:
                candidates = candidates.where(Resource.title == _resource_title(base_title, record.original_name, total))
            existing = db.execute(candidates).scalars().first()
            if existing is not None:  # 崩溃重放/重试：同一 blob 不重复建资源
                resource = existing
            else:
                target_id = (
                    find_source_owner(db, OWNER_RESOURCE, source_ref["platform"], source_ref["external_id"])
                    if source_ref
                    else None
                )
                target = db.get(Resource, target_id) if target_id else None
                if target is not None:
                    resource = _attach_file_to_resource(db, target, record, source_ref)
                else:
                    resource = _create_resource_from_file(db, course_id, kind, base_title, total, record, source_ref)
        except _ItemError as exc:
            db.rollback()
            errors.append(
                job_error(
                    exc.code, exc.message, retryable=exc.retryable, details={"file_id": file_id, "name": spec.get("name")}
                )
            )
        else:
            refs.append({"type": "resource", "id": resource.id})
        job.progress = (idx + 1) / total
        job.errors = list(errors)
        job.result_refs = list(refs)
        db.commit()

    dispatched: set[str] = set()
    for ref in refs:
        ref_id = ref.get("id")
        if ref.get("type") != "resource" or not isinstance(ref_id, str) or ref_id in dispatched:
            continue
        dispatched.add(ref_id)
        resource = db.get(Resource, ref_id)
        if resource is None or resource.file_id is None or resource.parse_status != "pending":
            continue
        enqueue_parse_job(db, resource, reason="import")
    db.commit()

    if errors:
        return JobResult(status="partial_failed" if refs else "failed", errors=errors, result_refs=refs)
    return JobResult(status="succeeded", result_refs=refs)


def _create_resource_from_file(
    db: Session,
    course_id: str,
    kind: str,
    base_title: str | None,
    total_files: int,
    record: FileRecord,
    source_ref: dict | None,
) -> Resource:
    resource = Resource(
        id=new_id(db, Resource, "res"),
        course_id=course_id,
        kind=kind,
        title=_resource_title(base_title, record.original_name, total_files),
        version=1,
        content_hash=record.content_hash,
        parse_status="pending",
        mime_type=record.mime_type,
        size_bytes=record.size_bytes,
        access_level="full",
        file_id=record.id,
        fetched_at=utc_now(),
    )
    db.add(resource)
    db.add(
        ResourceVersion(
            resource_id=resource.id,
            version=1,
            content_hash=record.content_hash,
            mime_type=record.mime_type,
            size_bytes=record.size_bytes,
            file_id=record.id,
            fetched_at=resource.fetched_at,
            parse_status="pending",
        )
    )
    if source_ref:
        db.add(
            SourceRef(
                owner_type=OWNER_RESOURCE,
                owner_id=resource.id,
                platform=source_ref["platform"],
                external_id=source_ref["external_id"],
                canonical_url=source_ref.get("canonical_url"),
                observed_at=as_datetime(source_ref["observed_at"]),
                seq=0,
            )
        )
    return resource


def _attach_file_to_resource(db: Session, resource: Resource, record: FileRecord, source_ref: dict | None) -> Resource:
    """把受控文件挂到同步已入库的资源：同指纹仅补齐文件，内容变化则递增版本（A4 §2）。"""
    if record.content_hash == resource.content_hash or (resource.file_id is None and resource.parse_status == "pending" and resource.content_hash is None):
        resource.content_hash = record.content_hash
        resource.mime_type = record.mime_type
        resource.size_bytes = record.size_bytes
        if resource.file_id is None:
            resource.file_id = record.id
            resource.fetched_at = utc_now()
            latest = db.execute(
                select(ResourceVersion).where(
                    ResourceVersion.resource_id == resource.id, ResourceVersion.version == resource.version
                )
            ).scalar_one_or_none()
            if latest is not None and latest.file_id is None:
                latest.content_hash = record.content_hash
                latest.file_id = record.id
                latest.fetched_at = resource.fetched_at
                if latest.mime_type is None:
                    latest.mime_type = record.mime_type
                if latest.size_bytes is None:
                    latest.size_bytes = record.size_bytes
    else:
        resource.version += 1
        resource.content_hash = record.content_hash
        resource.mime_type = record.mime_type
        resource.size_bytes = record.size_bytes
        resource.access_level = "full"
        resource.file_id = record.id
        resource.fetched_at = utc_now()
        resource.parse_status = "pending"
        resource.extent_value = None
        resource.extent_unit = None
        db.add(
            ResourceVersion(
                resource_id=resource.id,
                version=resource.version,
                content_hash=record.content_hash,
                mime_type=record.mime_type,
                size_bytes=record.size_bytes,
                file_id=record.id,
                fetched_at=resource.fetched_at,
                parse_status="pending",
            )
        )
    resource.archived_at = None
    resource.updated_at = utc_now()
    if source_ref:
        ref_row = db.execute(
            select(SourceRef).where(
                SourceRef.owner_type == OWNER_RESOURCE,
                SourceRef.owner_id == resource.id,
                SourceRef.platform == source_ref["platform"],
                SourceRef.external_id == source_ref["external_id"],
            )
        ).scalars().first()
        if ref_row is not None:
            ref_row.observed_at = as_datetime(source_ref["observed_at"])
            if source_ref.get("canonical_url"):
                ref_row.canonical_url = source_ref["canonical_url"]
        else:
            db.add(
                SourceRef(
                    owner_type=OWNER_RESOURCE,
                    owner_id=resource.id,
                    platform=source_ref["platform"],
                    external_id=source_ref["external_id"],
                    canonical_url=source_ref.get("canonical_url"),
                    observed_at=as_datetime(source_ref["observed_at"]),
                    seq=0,
                )
            )
    return resource


# ────────────────────────── 处理器：解析流水线（D2 接入） ──────────────────────────


# 输入决定型失败：不随重试变化（DEPENDENCY_MISSING/PARSE_TIMEOUT/INTERNAL_ERROR 之外）
_PARSE_NON_RETRYABLE = {
    ParseErrorCode.CORRUPT_FILE,
    ParseErrorCode.UNSUPPORTED_MEDIA_TYPE,
    ParseErrorCode.EMPTY_DOCUMENT,
    ParseErrorCode.FILE_TOO_LARGE,
    ParseErrorCode.PAGE_LIMIT_EXCEEDED,
    ParseErrorCode.ZIP_LIMIT_EXCEEDED,
    ParseErrorCode.ENCRYPTED_FILE,
}


# Attachment problems decided purely by the input (unsupported extension, or bytes that do
# not match the extension). Retrying cannot change them and the resource metadata is kept,
# so a sync reports them as warnings instead of failing the whole job.
ATTACHMENT_SKIP_CODES = frozenset({"UNSUPPORTED_MEDIA_TYPE", "CORRUPT_FILE", "FILE_TOO_LARGE"})


def _note_skipped_attachment(payload: dict, code: str) -> None:
    """Accumulate skipped-attachment counts on the durable job payload."""
    skipped = payload.setdefault("skipped_attachments", {})
    if isinstance(skipped, dict):
        skipped[code] = int(skipped.get(code) or 0) + 1


def _skipped_attachment_warnings(skipped: Any) -> list[dict]:
    """Turn accumulated skip counts into contract-shaped job warnings."""
    if not isinstance(skipped, dict):
        return []
    messages = {
        "UNSUPPORTED_MEDIA_TYPE": "附件格式暂不支持，已保留元数据、未下载正文",
        "CORRUPT_FILE": "附件内容与扩展名不符，已保留元数据、未纳入解析",
        "FILE_TOO_LARGE": "附件超过解析护栏上限（64MB），已保留元数据、未下载正文",
    }
    warnings: list[dict] = []
    for code, count in sorted(skipped.items()):
        try:
            total = int(count)
        except (TypeError, ValueError):
            continue
        if total <= 0:
            continue
        warnings.append({"code": code, "message": f"{messages.get(code, code)}（{total} 个）"})
    return warnings


def enqueue_parse_job(db: Session, resource: Resource, *, reason: str) -> Job:
    """为资源当前版本创建解析任务；同 (资源, 版本) 已有排队/执行中任务则复用（去抖）。

    reason 记录派发来源（import / reparse），仅存 payload，供诊断。不提交事务：由调用方提交。
    """
    version = resource.version
    active = db.execute(select(Job).where(Job.type == "parse", Job.status.in_(("queued", "running")))).scalars()
    for existing in active:
        existing_payload = existing.payload or {}
        if existing_payload.get("resource_id") == resource.id and existing_payload.get("version") == version:
            return existing
    job = Job(
        id=new_id(db, Job, "job"),
        type="parse",
        status="queued",
        progress=0.0,
        errors=[],
        result_refs=[],
        payload={"resource_id": resource.id, "version": version, "reason": reason},
    )
    db.add(job)
    return job


def _parse_limits() -> ParseLimits:
    # 单文件解析上限：config.MAX_PARSE_BYTES 单一常量（PD-1，64MB 十进制）；
    # 其余（页数/片段数/时限/解压总量）走解析模块默认值
    return ParseLimits(max_file_bytes=config.MAX_PARSE_BYTES)


def _parse_extent(stats: dict[str, Any]) -> tuple[int | None, str | None]:
    """篇幅派生：仅 pdf 页数 / pptx 幻灯片数是契约 extent_unit 枚举值；其余（如 docx 段落）未知。"""
    units = stats.get("units")
    label = stats.get("units_label")
    if isinstance(units, int) and label in ("page", "slide"):
        return units, label
    return None, None


def _unpack_course_archive(db: Session, job: Job, resource: Resource,
                           version_row: ResourceVersion, record: FileRecord, blob) -> JobResult:
    """有限解包为独立文档资源；不提取到命名路径，不递归解包。"""
    if version_row.version != resource.version:
        raise JobFailed("CONFLICT", "旧版本课件包不能覆盖当前成员，请解析最新版本", retryable=False)
    if record.size_bytes > config.MAX_PARSE_BYTES:
        raise JobFailed("FILE_TOO_LARGE", "课件包超过解析大小上限", retryable=False)
    try:
        archive = zipfile.ZipFile(blob)
    except zipfile.BadZipFile:
        raise JobFailed("CORRUPT_FILE", "课件包不是有效的 ZIP 文件", retryable=False) from None
    with archive:
        members = [m for m in archive.infolist() if not m.is_dir()]
        if len(members) > config.MAX_ARCHIVE_MEMBERS or sum(m.file_size for m in members) > config.MAX_ARCHIVE_BYTES:
            raise JobFailed("ZIP_LIMIT_EXCEEDED", "课件包成员数或解压总量超过上限", retryable=False)
        # 在任何落库前验证整个目录；禁止路径穿越、软链接、重复名称和加密成员。
        seen: set[str] = set()
        for member in members:
            name = member.filename.replace("\\", "/")
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or ":" in name or name in seen or ((member.external_attr >> 16) & 0o170000) == 0o120000:
                raise JobFailed("CORRUPT_FILE", "课件包包含不安全或重复的成员路径", retryable=False)
            if member.flag_bits & 1:
                raise JobFailed("ENCRYPTED_FILE", "暂不支持加密课件包", retryable=False)
            seen.add(name)
        docs = [m for m in members if PurePosixPath(m.filename).suffix.lower() in (".pdf", ".pptx", ".docx")
                and not m.filename.startswith("__MACOSX/")]
        if not docs:
            resource.parse_status = version_row.parse_status = "skipped"
            db.commit()
            raise JobFailed("UNSUPPORTED_MEDIA_TYPE", "课件包内没有 PDF、PPTX 或 DOCX；原包已保留", retryable=False)
        # 先有界读取所有支持成员，再保存；避免损坏的后半部分导致不完整导入。
        documents = []
        for member in docs:
            check_cancel(db, job)
            if member.file_size > config.MAX_PARSE_BYTES:
                raise JobFailed("FILE_TOO_LARGE", "课件包内文档超过解析大小上限", retryable=False)
            try:
                with archive.open(member) as stream:
                    data = stream.read(config.MAX_PARSE_BYTES + 1)
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError):
                raise JobFailed("CORRUPT_FILE", "课件包成员无法读取", retryable=False) from None
            if len(data) > config.MAX_PARSE_BYTES:
                raise JobFailed("FILE_TOO_LARGE", "课件包内文档超过解析大小上限", retryable=False)
            documents.append((member.filename.replace("\\", "/"), data))
    refs = [{"type": "resource", "id": resource.id}]
    for name, data in documents:
        check_cancel(db, job)
        key = f"archive-{resource.id}-{hashlib.sha256(name.encode()).hexdigest()[:24]}"
        source = {"platform": "manual", "external_id": key, "canonical_url": None,
                  "observed_at": iso_utc(utc_now())}
        owner = find_source_owner(db, OWNER_RESOURCE, "manual", key)
        child = db.get(Resource, owner) if owner else None
        digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
        if child is None or child.content_hash != digest:
            file_id = new_id(db, FileRecord, "fil")
            stored = storage.store_bytes(file_id, data, display_name=name,
                                        mime_type=storage.EXTENSION_MIME[PurePosixPath(name).suffix.lower()])
            child_file = FileRecord(id=file_id, original_name=stored.original_name, mime_type=stored.mime_type,
                                    size_bytes=stored.size_bytes, content_hash=stored.content_hash)
            db.add(child_file)
            if child is None:
                child = _create_resource_from_file(db, resource.course_id, "document",
                                                  f"{resource.title} / {name}", 1, child_file, source)
            else:
                _attach_file_to_resource(db, child, child_file, source)
            db.flush()
        if child.parse_status != "ready":
            enqueue_parse_job(db, child, reason="archive")
        refs.append({"type": "resource", "id": child.id})
    resource.parse_status = version_row.parse_status = "skipped"
    resource.updated_at = utc_now()
    db.commit()
    return JobResult(status="succeeded", result_refs=refs)


@handler("parse")
def handle_parse(db: Session, job: Job) -> JobResult:
    """解析任务：受控文件 → D2 parse_document → ContentChunk 落库（检索与计划引擎的文本来源）。

    契约 parse_status 枚举（pending/processing/ready/failed/skipped）无 needs_ocr：
    needs_ocr 按 failed + 错误码 NEEDS_OCR 如实落库（可重试），不把“未提取到文本”写成成功。
    版本已有旧产物（parse_status=ready）时不置 processing，终态失败也不改动旧产物：
    “旧解析产物在新任务成功前保持可用”（reparse 契约）；失败原因保留在任务 errors（含诊断 ID）。
    可取消：状态提交点/解析前后均检查 cancel_requested，中止时回退到此前状态。
    """
    payload = job.payload or {}
    resource_id = payload.get("resource_id")
    version = payload.get("version")
    if not isinstance(resource_id, str) or not isinstance(version, int) or version < 1:
        raise JobFailed(
            "INVALID_PAYLOAD",
            f"解析任务参数不合法：resource_id={resource_id!r}, version={version!r}",
            retryable=False,
        )
    resource = db.get(Resource, resource_id)
    if resource is None:
        raise JobFailed("NOT_FOUND", f"资源不存在：{resource_id}", retryable=False)
    version_row = db.execute(
        select(ResourceVersion).where(
            ResourceVersion.resource_id == resource_id, ResourceVersion.version == version
        )
    ).scalar_one_or_none()
    if version_row is None:
        raise JobFailed("NOT_FOUND", f"资源版本不存在：{resource_id} v{version}", retryable=False)

    is_current = version == resource.version
    file_id = version_row.file_id or (resource.file_id if is_current else None)
    if not isinstance(file_id, str) or not file_id:
        raise JobFailed(
            "NOT_FOUND",
            "该版本没有已入库的受控文件，无法解析",
            retryable=False,
            details={"resource_id": resource_id, "version": version},
        )
    record = db.get(FileRecord, file_id)
    if record is None:
        raise JobFailed(
            "NOT_FOUND",
            f"文件记录不存在：{file_id}",
            retryable=False,
            details={"resource_id": resource_id, "version": version},
        )
    try:
        blob = storage.blob_path(file_id)
    except ApiError:
        raise JobFailed("NOT_FOUND", f"文件标识不合法：{file_id}", retryable=False) from None
    if not blob.exists():
        raise JobFailed(
            "FILE_MISSING",
            f"受控文件缺失：{file_id}（元数据在库但存储中没有该文件）",
            retryable=False,
            details={"resource_id": resource_id, "version": version, "file_id": file_id},
        )

    check_cancel(db, job)
    if record.mime_type == "application/zip":
        try:
            return _unpack_course_archive(db, job, resource, version_row, record, blob)
        except JobFailed as exc:
            if version_row.parse_status != "skipped":
                version_row.parse_status = "failed"
                if is_current:
                    resource.parse_status = "failed"
                db.commit()
            raise exc
    old_ready = version_row.parse_status == "ready"
    previous_status = version_row.parse_status
    if not old_ready:
        version_row.parse_status = "processing"
        if is_current:
            resource.parse_status = "processing"
            resource.updated_at = utc_now()
        db.commit()

    try:
        data = blob.read_bytes()
        check_cancel(db, job)
        context = ParseContext(
            resource_id=resource.id,
            resource_version=version,
            content_hash=record.content_hash or "",
            job_id=job.id,
            observed_at=iso_utc(utc_now()),
        )
        result = parse_document(
            data,
            filename=record.original_name,
            mime_type=record.mime_type,
            context=context,
            limits=_parse_limits(),
            cache_dir=config.FILES_DIR / "parse-cache",
        )
        check_cancel(db, job)
    except JobCancelled:
        if not old_ready:
            version_row.parse_status = previous_status
            if is_current:
                resource.parse_status = previous_status
            db.commit()
        raise

    if result.status != ParseStatus.READY:
        needs_ocr = result.status == ParseStatus.NEEDS_OCR
        code = "NEEDS_OCR" if needs_ocr else (result.error_code or ParseErrorCode.INTERNAL_ERROR)
        # 可入库但无解析器的类型（如 .zip 课件包）：终态标 skipped（“该类型不解析”），不误标 failed；
        # 原因保留在任务 errors（UNSUPPORTED_MEDIA_TYPE，不可重试）。
        terminal = "skipped" if code == ParseErrorCode.UNSUPPORTED_MEDIA_TYPE else "failed"
        if not old_ready:  # 无旧产物可保留：如实标注终态（chunk_count 派生为 null）
            version_row.parse_status = terminal
            if is_current:
                resource.parse_status = terminal
                resource.updated_at = utc_now()
            db.commit()
        raise JobFailed(
            code,
            result.message or "解析失败",
            retryable=needs_ocr or code not in _PARSE_NON_RETRYABLE,
            details={
                "resource_id": resource.id,
                "version": version,
                "job_id": job.id,
                "extractor": result.extractor,
                "extractor_version": result.extractor_version,
                "limitations": list(result.limitations),
            },
        )

    db.execute(
        delete(ContentChunk).where(
            ContentChunk.resource_id == resource.id, ContentChunk.resource_version == version
        )
    )
    for chunk in result.chunks:
        chunk_payload = chunk.to_dict(context, result.extractor or "", result.extractor_version)
        db.add(
            ContentChunk(
                id=new_id(db, ContentChunk, "chk"),
                resource_id=resource.id,
                resource_version=version,
                ordinal=chunk.ordinal,
                text=chunk.text,
                locator=chunk_payload["locator"],
                provenance=chunk_payload["provenance"],
            )
        )
    extent_value, extent_unit = _parse_extent(result.stats)
    version_row.parse_status = "ready"
    version_row.extent_value = extent_value
    version_row.extent_unit = extent_unit
    if is_current:
        resource.parse_status = "ready"
        resource.extent_value = extent_value
        resource.extent_unit = extent_unit
        resource.updated_at = utc_now()
    db.commit()
    # 解析完成 → 按课程自动派发任务生成（B13 ④；同范围去抖由 enqueue 保证）
    enqueue_build_tasks_job(db, course_id=resource.course_id, reason="parse")
    db.commit()
    return JobResult(status="succeeded", result_refs=[{"type": "resource", "id": resource.id}])


# ────────────────────────── 处理器：任务生成（D3 接入） ──────────────────────────


@handler("build_tasks")
def handle_build_tasks(db: Session, job: Job) -> JobResult:
    """候选任务生成：资源/作业/回放（+既有任务）→ D12 build_tasks_json → 落库（match_key 对齐，幂等）。

    payload.course_id 限定课程范围（缺省/null 为全部课程）。
    重复触发不制造重复任务；既有任务的人工修正（耗时、完成状态等）不被覆盖（D3 §保留用户修正）。
    TaskGenResult.warnings 落 job.warnings 供前端展示；result_refs 记录本次新建/更新的任务。
    """
    payload = job.payload or {}
    course_id = payload.get("course_id")
    if course_id is not None and not isinstance(course_id, str):
        raise JobFailed("INVALID_PAYLOAD", f"课程范围不合法：{course_id!r}", retryable=False)
    if course_id is not None and db.get(Course, course_id) is None:
        raise JobFailed("NOT_FOUND", f"课程不存在：{course_id}", retryable=False)

    check_cancel(db, job)
    job.progress = 0.35
    db.commit()
    outcome: BuildOutcome = run_build_tasks(db, course_id=course_id)
    check_cancel(db, job)
    refs = [{"type": "task", "id": task_id} for task_id in outcome.created + outcome.updated]
    job.warnings = outcome.warnings
    job.result_refs = refs
    db.commit()
    log.info("job %s 任务生成：新建 %d，更新 %d，告警 %d", job.id, len(outcome.created), len(outcome.updated), len(outcome.warnings))
    return JobResult(status="succeeded", result_refs=refs)


# ────────────────────────── 处理器：复习卷生成（CR008 接 A/D/模型） ──────────────────────────

_REVIEW_MODES = ("ai", "bank", "hybrid")
_PROVIDER_RETRY_LIMIT = 2  # 附加尝试上限（冻结契约：429/超时/5xx 有界重试 ≤2 次，401 永不）
# hybrid 模式：配额/总分/章节覆盖缺口由题库合并补齐（合并后统一校验），不触发模型修复轮
_HYBRID_BANK_DEFERRED_CODES = frozenset({"QUOTA_MISMATCH", "TOTAL_POINTS_MISMATCH", "COVERAGE_MISSING"})


def _review_phase(db: Session, job: Job, phase: str, progress: float) -> None:
    """阶段推进（phase + progress 短提交；取消在阶段边界可见）。"""
    job.phase = phase
    job.progress = progress
    db.commit()


def _review_cancel_probe(db: Session, job: Job) -> Callable[[], bool]:
    """A 题库的协作式取消探针（刷新 job 读取 cancel_requested）。"""
    def cancelled() -> bool:
        db.refresh(job)
        return bool(job.cancel_requested)
    return cancelled


def _interruptible_sleep(db: Session, job: Job, seconds: float) -> None:
    """可取消等待（重试退避期间保持取消响应）。"""
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        db.refresh(job)
        if job.cancel_requested:
            raise JobCancelled(job.id)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.25, remaining))


def _review_model_call(
    db: Session,
    job: Job,
    cfg: ProviderConfig,
    api_key: str,
    messages: list[dict],
    *,
    max_tokens: int,
    phase: str,
) -> str:
    """模型调用（B 统一发起，密钥只在内存中传递、不落日志）：仅可重试错误有界退避重试。"""
    attempts = 0
    messages = [dict(message) for message in messages]
    if messages and messages[0].get("role") == "system":
        messages[0]["content"] += (
            "\n命题依据优先使用本课程课件/PPT与作业正文。标题以‘历年卷参考：’开头的材料仅辅助参考题型、"
            "难度、分值与范围，不默认照搬原题，也不能用历年卷内容扩展超出用户备注限制的考点。"
            "课堂转写、板书识别若出现仅是补充，识别不确定处不能当作确定事实。"
        )
    while True:
        attempts += 1
        check_cancel(db, job)
        try:
            content, _status = providers.call_chat_completions(
                cfg.base_url, cfg.model, api_key, cfg.timeout_seconds, messages, max_tokens=max_tokens
            )
            return content
        except providers.ProviderCallError as exc:
            if not exc.retryable or attempts > _PROVIDER_RETRY_LIMIT:
                raise JobFailed(
                    exc.code,
                    exc.message,
                    retryable=exc.retryable,
                    details={"phase": phase, "attempts": attempts},
                ) from None
            log.info("job %s 模型调用第 %d 次失败（%s），有界重试", job.id, attempts, exc.code)
            _interruptible_sleep(db, job, min(4.0, 1.5 * attempts))


def _review_job_warnings(warnings: list[dict]) -> list[dict]:
    """契约 Warning（question_ids）→ job.schema Warning（task_ids；题目 ID 并入 details）。"""
    out: list[dict] = []
    for warning in warnings or []:
        details = dict(warning.get("details") or {})
        question_ids = list(warning.get("question_ids") or [])
        if question_ids:
            details["question_ids"] = question_ids
        out.append({
            "code": str(warning.get("code") or "REVIEW_WARNING"),
            "message": str(warning.get("message") or ""),
            "task_ids": [],
            "details": details or None,
        })
    return out


def _review_max_tokens(blueprint_dict: dict[str, Any]) -> int:
    total = sum(int(q.get("count") or 0) for q in blueprint_dict.get("quotas") or [])
    # 光学 8 道计算题实测输出 16991（含思考 10950）；保留充分思考及答案余量。
    return max(32768, min(65536, total * 1600 + 16384))


def _review_remaining_deficits(deficits: list[dict], candidate: dict[str, Any] | None) -> list[dict]:
    """合并后仍缺的题型缺口（按候选实际题数扣减；不为已补足的题报 DEFICIT_UNFILLED）。"""
    counts: dict[str, int] = {}
    for question in (candidate or {}).get("questions") or []:
        qtype = str(question.get("qtype") or "")
        counts[qtype] = counts.get(qtype, 0) + 1
    remaining: list[dict] = []
    for deficit in deficits:
        still = max(0, int(deficit.get("needed") or 0) - counts.get(str(deficit.get("qtype") or ""), 0))
        if still > 0:
            remaining.append({**deficit, "needed": still})
    return remaining


@handler("review_generate")
def handle_review_generate(db: Session, job: Job) -> JobResult:
    """复习卷生成流水线：快照 → D 蓝图/分析 → A 检索 → 模型 → 校验/修复 → 持久化为修订。

    - 材料按受理时快照逐片段复读并核对漂移；漂移即失败（不静默发送与预览不一致的正文）。
    - 模型请求由 B 统一发起（secrets_store 取密钥，仅内存持有）；修复轮 ≤1（冻结契约）。
    - 报告 errors 但 ≥1 题：落库 + partial_failed（失败项进 errors）；failed/0 题：JobFailed。
    - 取消发生在落库提交前则不留任何修订（修订行 + latest_revision + job 产物为单提交）。
    """
    payload = job.payload or {}
    paper_id = payload.get("paper_id")
    snapshot_id = payload.get("snapshot_id")
    revision = int(payload.get("revision") or 0)
    revision_reason = str(payload.get("revision_reason") or "initial")
    based_on_revision = payload.get("based_on_revision")
    mode = str(payload.get("mode") or "")
    provider_config_id = payload.get("provider_config_id")

    paper = db.get(ReviewPaper, paper_id) if isinstance(paper_id, str) else None
    snapshot = db.get(ReviewMaterialSnapshot, snapshot_id) if isinstance(snapshot_id, str) else None
    if paper is None or snapshot is None or revision < 1 or mode not in _REVIEW_MODES:
        raise JobFailed("INVALID_PAYLOAD", "出卷任务参数不完整（paper/snapshot/revision/mode）", retryable=False)
    if snapshot.course_id != paper.course_id:
        raise JobFailed("INVALID_PAYLOAD", "材料快照与试卷不属于同一课程", retryable=False)
    course = db.get(Course, paper.course_id)
    if course is None:
        raise JobFailed("NOT_FOUND", f"课程不存在：{paper.course_id}", retryable=False)

    # 崩溃恢复重放：本修订已落库则不重复生成（仅补记 job 终态与产物引用）
    existing = db.get(ReviewPaperRevision, (paper.id, revision))
    if existing is not None:
        if existing.snapshot_id != snapshot.id:
            raise JobFailed(
                "CONFLICT", "该修订号已被其他快照落库，为避免覆盖历史版本任务终止",
                retryable=False, details={"paper_id": paper.id, "revision": revision},
            )
        refs = [{"type": "review_paper", "id": paper.id}]
        job.result_refs = refs
        job.warnings = _review_job_warnings(existing.warnings)
        job.progress = 1.0
        db.commit()
        return JobResult(status="succeeded", result_refs=refs)

    engine = review_deps.review_engine_module()
    if engine is None:
        raise JobFailed("DEPENDENCY_UNAVAILABLE", "出卷引擎模块未接入（learning.review），无法出卷", retryable=False)

    provider_config = db.get(ProviderConfig, provider_config_id) if isinstance(provider_config_id, str) else None
    if mode != "bank":
        if provider_config is None:
            raise JobFailed("NOT_FOUND", f"AI 服务配置不存在：{provider_config_id}", retryable=False)
        frozen_provider = payload.get("provider_snapshot")
        if isinstance(frozen_provider, dict) and any(
            frozen_provider.get(key) != getattr(provider_config, key) for key in ("base_url", "model", "protocol")
        ):
            raise JobFailed("PROVIDER_CHANGED", "AI 服务地址或模型已变化，请重新预览并生成", retryable=False)
        if not secrets_store.has_key(provider_config.id):
            raise JobFailed("PROVIDER_KEY_MISSING", "该 AI 服务配置尚未保存密钥，无法调用模型", retryable=False)

    notes_text = str((snapshot.notes or {}).get("text") or "")
    constraints = dict(snapshot.constraints or {})
    title = str(payload.get("title") or paper.title)

    # ── materials：按冻结快照逐片段复读正文，核对漂移 ──
    _review_phase(db, job, "materials", 0.05)
    check_cancel(db, job)
    chunk_ids = [cid for item in snapshot.items or [] for cid in (item.get("chunk_ids") or [])]
    chunk_rows: dict[str, ContentChunk] = {}
    if chunk_ids:
        chunk_rows = {
            row.id: row
            for row in db.execute(select(ContentChunk).where(ContentChunk.id.in_(chunk_ids))).scalars()
        }
    materials, drift = review_engine.material_inputs(snapshot.items or [], chunk_rows, {})
    if drift:
        raise JobFailed(
            "MATERIAL_CHANGED",
            f"受理后材料内容发生变化（{'、'.join(drift)}），为保证与预览一致已中止；请重新发起出卷",
            retryable=False,
            details={"items": drift},
        )
    try:
        d_snapshot = engine.build_snapshot(
            course_id=snapshot.course_id,
            course_title=course.title,
            materials=materials,
            notes=notes_text,
            provider=review_engine.provider_for_engine(
                {
                    "config_id": provider_config.id,
                    "protocol": provider_config.protocol,
                    "model": provider_config.model,
                    "base_url": provider_config.base_url,
                }
                if provider_config is not None
                else None
            ),
        )
        analysis = engine.analyze_materials(d_snapshot)
    except engine.ReviewError as exc:
        raise JobFailed(exc.code, exc.message, retryable=False, details=exc.details) from None

    # ── blueprint：约束 → 蓝图（bank/hybrid 在此产出检索请求） ──
    _review_phase(db, job, "blueprint", 0.15)
    check_cancel(db, job)
    engine_limits = engine.ReviewLimits()
    try:
        blueprint = engine.build_blueprint(
            d_snapshot,
            constraints=review_engine.engine_constraints(constraints, mode, title),
            analysis=analysis,
            limits=engine_limits,
        )
    except engine.ReviewError as exc:
        raise JobFailed(exc.code, exc.message, retryable=False, details=exc.details) from None
    blueprint_dict = blueprint.to_dict()

    selection = None
    missing = list(snapshot.missing or [])
    selection_warnings: list[dict] = []
    deficits: list[dict] = []

    # ── bank_search（bank/hybrid）：A 题库检索 → 按蓝图选题 ──
    if mode in ("bank", "hybrid"):
        _review_phase(db, job, "bank_search", 0.35)
        check_cancel(db, job)
        qb = review_deps.questionbank_module()
        if qb is None:
            raise JobFailed(
                "DEPENDENCY_UNAVAILABLE", "题库模块未接入（connectors.questionbanks），无法使用 bank/hybrid 模式",
                retryable=False,
            )
        sources = list(review_deps.questionbank_sources_config().get("sources") or [])
        if not sources:
            raise JobFailed("BANK_NOT_CONFIGURED", "题库来源未配置，无法使用 bank/hybrid 模式", retryable=False)
        cancel_probe = _review_cancel_probe(db, job)
        candidates: list[dict] = []
        searches = review_engine.build_bank_searches(
            blueprint_dict, course.title, sources, engine_limits.max_search_requests
        )
        for index, search in enumerate(searches):
            check_cancel(db, job)
            try:
                search_outcome = qb.search_candidates(search, cancellation=cancel_probe)
            except qb.QuestionBankCancelled:
                raise JobCancelled(job.id) from None
            except qb.QuestionBankError as exc:
                raise JobFailed(
                    exc.code, exc.message, retryable=exc.retryable, details=dict(exc.details or {})
                ) from None
            candidates.extend(search_outcome.candidates)
            for warning in search_outcome.warnings:
                selection_warnings.append({
                    "code": str(warning.get("code") or "BANK_SOURCE_WARNING"),
                    "message": str(warning.get("message") or ""),
                    "details": warning.get("details"),
                })
            job.progress = 0.35 + 0.15 * (index + 1) / max(1, len(searches))
            db.commit()
        try:
            selection = engine.select_bank_questions(blueprint, candidates)
        except engine.ReviewError as exc:
            raise JobFailed(exc.code, exc.message, retryable=False, details=exc.details) from None
        for text in selection.warnings:
            selection_warnings.append({"code": "BANK_WARNING", "message": str(text), "details": None})
        deficits = [d.to_dict() for d in selection.deficits]

    # ── generation + validation（ai/hybrid）：模型调用 + ≤1 轮有限修复 ──
    outcome = None
    repair_rounds = 0
    pre_repair_candidate: dict | None = None
    if mode in ("ai", "hybrid"):
        _review_phase(db, job, "generation", 0.55)
        check_cancel(db, job)
        api_key = secrets_store.get_key(provider_config.id)
        if not api_key:
            raise JobFailed("PROVIDER_KEY_MISSING", "该 AI 服务配置尚未保存密钥，无法调用模型", retryable=False)
        max_tokens = _review_max_tokens(blueprint_dict)
        try:
            gen_request = engine.build_generation_request(
                d_snapshot,
                blueprint,
                selected=selection,
                deficits=list(selection.deficits) if selection is not None else None,
                analysis=analysis,
                limits=engine_limits,
            )
        except engine.ReviewError as exc:
            raise JobFailed(exc.code, exc.message, retryable=False, details=exc.details) from None
        raw = _review_model_call(
            db, job, provider_config, api_key, gen_request.messages, max_tokens=max_tokens, phase="generation"
        )
        _review_phase(db, job, "validation", 0.8)
        check_cancel(db, job)
        try:
            outcome = engine.parse_and_validate_output(raw, d_snapshot, blueprint, selected=selection)
        except engine.ReviewError as exc:
            raise JobFailed(exc.code, exc.message, retryable=False, details=exc.details) from None
        repair_cap = min(1, int(outcome.report.repair_rounds_max or 0), engine_limits.max_repair_rounds)

        def _repair_worthwhile() -> bool:
            if outcome.candidate is None or not outcome.report.repair_needed:
                return False
            if mode != "hybrid":
                return True
            return any(
                issue.severity == "error" and str(getattr(issue, "code", "")) not in _HYBRID_BANK_DEFERRED_CODES
                for issue in outcome.report.issues
            )

        while repair_rounds < repair_cap and _repair_worthwhile():
            check_cancel(db, job)
            try:
                repair_request = engine.build_repair_request(
                    outcome.candidate, outcome.report, d_snapshot, blueprint,
                    selected=selection, limits=engine_limits,
                )
            except engine.ReviewError as exc:
                raise JobFailed(exc.code, exc.message, retryable=False, details=exc.details) from None
            raw_repaired = _review_model_call(
                db, job, provider_config, api_key, repair_request.messages,
                max_tokens=max_tokens, phase="validation",
            )
            try:
                repaired = engine.parse_and_validate_output(raw_repaired, d_snapshot, blueprint, selected=selection)
            except engine.ReviewError as exc:
                raise JobFailed(exc.code, exc.message, retryable=False, details=exc.details) from None
            repair_rounds += 1
            if repaired.candidate is not None:
                pre_repair_candidate = outcome.candidate.to_dict()
                outcome = repaired
            else:
                selection_warnings.append({
                    "code": "REPAIR_ATTEMPT_FAILED",
                    "message": "修复轮输出无法解析，已保留首轮结果并如实标注其校验状态。",
                    "details": {"repair_rounds": repair_rounds},
                })
                break
        if mode == "hybrid":
            try:
                outcome = engine.merge_ai_with_bank(selection, outcome, d_snapshot, blueprint)
            except engine.ReviewError as exc:
                raise JobFailed(exc.code, exc.message, retryable=False, details=exc.details) from None

    if mode == "bank":
        _review_phase(db, job, "validation", 0.8)
        check_cancel(db, job)
        try:
            outcome = engine.validate_bank_questions(selection, d_snapshot, blueprint)
        except engine.ReviewError as exc:
            raise JobFailed(exc.code, exc.message, retryable=False, details=exc.details) from None

    # ── saving：适配契约形状 → 持久化修订（单提交；取消发生在提交前则不留空版本） ──
    _review_phase(db, job, "saving", 0.95)
    check_cancel(db, job)
    report_dict = outcome.report.to_dict()
    candidate_dict = outcome.candidate.to_dict() if outcome.candidate is not None else None
    report_status = str(report_dict.get("status") or "failed")

    if report_status == "failed" or not ((candidate_dict or {}).get("questions") or []):
        first_error = next(
            (issue for issue in report_dict.get("issues") or [] if issue.get("severity") == "error"), None
        )
        raise JobFailed(
            "GENERATION_FAILED" if report_status == "failed" else "NO_QUESTIONS",
            (first_error or {}).get("message") or "模型输出不合格且无可交付题目",
            retryable=report_status == "failed",
            details={
                "report_status": report_status,
                "error_count": report_dict.get("error_count"),
                "errors": review_engine.report_error_entries(report_dict)[:10],
            },
        )

    prefix = review_engine.question_id_prefix(paper.id)
    locators = review_engine.chunk_locator_map(snapshot.items or [])
    adapted = review_engine.adapt_questions(
        candidate_dict, report_dict, prefix, locators, review_engine.effective_points(constraints)
    )
    repaired_ids = review_engine.mark_repaired(pre_repair_candidate if mode == "ai" else None, adapted)
    if not adapted:
        raise JobFailed(
            "NO_QUESTIONS", "适配后无可交付题目",
            retryable=False, details={"report_status": report_status},
        )
    remaining_deficits = _review_remaining_deficits(deficits, candidate_dict)
    warnings = review_engine.adapt_warnings(
        report_dict,
        candidate_dict,
        adapted,
        missing=missing,
        deficits=remaining_deficits,
        selection_warnings=selection_warnings,
        repair_rounds=repair_rounds,
        repaired_ids=repaired_ids,
    )
    final_questions = review_engine.finalize_questions(adapted)
    blueprint_contract = review_engine.adapt_blueprint(
        blueprint_dict, analysis.to_dict(), final_questions, mode, missing, notes_text
    )
    summary = review_engine.adapt_summary(final_questions)
    provider_block = review_engine.provider_snapshot(
        {
            "config_id": provider_config.id,
            "name": provider_config.name,
            "protocol": provider_config.protocol,
            "base_url": provider_config.base_url,
            "model": provider_config.model,
        }
        if provider_config is not None
        else None
    )

    check_cancel(db, job)
    db.add(
        ReviewPaperRevision(
            paper_id=paper.id,
            revision=revision,
            revision_reason=revision_reason,
            based_on_revision=based_on_revision if isinstance(based_on_revision, int) else None,
            snapshot_id=snapshot.id,
            title=title,
            mode=mode,
            engine_version=snapshot.engine_version,
            fingerprint=snapshot.fingerprint,
            constraints=constraints,
            provider=provider_block,
            blueprint=blueprint_contract,
            questions=final_questions,
            warnings=warnings,
            summary=summary,
        )
    )
    paper.latest_revision = revision
    paper.title = title
    paper.updated_at = utc_now()
    refs = [{"type": "review_paper", "id": paper.id}]
    job.result_refs = refs
    job.warnings = _review_job_warnings(warnings)
    db.commit()
    log.info(
        "job %s 出卷完成：paper=%s revision=%d 题目 %d 告警 %d 报告 %s",
        job.id, paper.id, revision, len(final_questions), len(warnings), report_status,
    )

    if report_status in ("ok", "ok_with_warnings"):
        return JobResult(status="succeeded", result_refs=refs)
    return JobResult(status="partial_failed", errors=review_engine.report_error_entries(report_dict), result_refs=refs)


# ────────────────────────── 处理器：平台同步 ──────────────────────────


def sync_pending_course_ids(payload: dict) -> list[str] | None:
    """Durable continuation scope; None denotes a legacy job without a checkpoint."""
    for key in ("pending_course_ids", "remaining_courses"):
        value = payload.get(key)
        if isinstance(value, list) and all(isinstance(c, str) and c for c in value):
            scope = payload.get("course_ids")
            return list(dict.fromkeys(c for c in value if not isinstance(scope, list) or c in scope))
    return None


def resume_auth_sync_jobs(db: Session, connection: Connection) -> int:
    """Requeue auth-paused leaf jobs after a committed, successful connection check.

    Reuse the original job/receipt instead of spawning tasks. Retry ancestry prevents
    resuming both a parent and its retry; all ancestors must share connection/scope.
    The conditional status update makes repeated/concurrent checks idempotent.
    """
    db.refresh(connection)
    if connection.status != "ok":
        return 0
    jobs = list(db.execute(select(Job).where(Job.type == "sync")).scalars())
    by_id = {job.id: job for job in jobs}

    def valid_ancestry(job: Job) -> bool:
        seen = {job.id}
        while (job.payload or {}).get("parent_job_id"):
            parent_id = job.payload["parent_job_id"]
            if not isinstance(parent_id, str) or parent_id in seen:
                return False
            parent = by_id.get(parent_id)
            if parent is None or (parent.payload or {}).get("connection_id") != connection.id:
                return False
            child_scope = (job.payload or {}).get("course_ids")
            parent_scope = (parent.payload or {}).get("course_ids")
            if not isinstance(child_scope, list) or not isinstance(parent_scope, list) or not set(child_scope) <= set(parent_scope):
                return False
            seen.add(parent_id)
            job = parent
        return True

    superseded = {
        (job.payload or {}).get("parent_job_id") for job in jobs
        if (job.payload or {}).get("connection_id") == connection.id and valid_ancestry(job)
    }
    resumed = 0
    for job in jobs:
        payload = dict(job.payload or {})
        if (payload.get("connection_id") != connection.id or job.id in superseded
                or job.status not in ("failed", "partial_failed") or job.cancel_requested
                or not payload.get("auth_required") or not sync_pending_course_ids(payload)
                or not valid_ancestry(job)):
            continue
        payload["auth_required"] = False
        payload["sync_errors"] = [e for e in payload.get("sync_errors", []) if e.get("code") != "AUTH_EXPIRED"]
        claimed = db.execute(
            update(Job).where(
                Job.id == job.id, Job.status.in_(("failed", "partial_failed")), Job.cancel_requested.is_(False),
                select(Connection.status).where(Connection.id == connection.id).scalar_subquery() == "ok",
            ).values(status="queued", started_at=None, finished_at=None, errors=[], payload=payload)
            .execution_options(synchronize_session=False)
        ).rowcount
        resumed += claimed or 0
    db.commit()
    db.expire_all()
    return resumed


def _sync_checkpoint(db: Session, job: Job, payload: dict, pending: list[str],
                     errors: list[dict], refs: list[dict], *, phase: str) -> None:
    payload.update(pending_course_ids=list(pending), remaining_courses=list(pending), sync_errors=list(errors))
    job.payload = copy.deepcopy(payload)  # Never share nested mutable JSON with ORM's committed value.
    job.phase = phase
    job.errors = list(errors)
    job.result_refs = list(refs)
    job.progress = 1 - len(pending) / max(1, len(payload["course_ids"]))
    db.commit()


def _pause_auth_sync(db: Session, job: Job, connection: Connection, payload: dict,
                     pending: list[str], errors: list[dict], refs: list[dict], exc: AdapterError) -> JobResult:
    exc.retryable = False  # Never schedule automatic AUTH retries.
    errors.append(adapter_error_entry(exc, pending[0]))
    mark_auth_required(connection, action=exc.action, message=exc.message)
    payload["auth_required"] = True
    _sync_checkpoint(db, job, payload, pending, errors, refs, phase="auth_required")
    return JobResult(status="partial_failed" if (payload.get("sync_succeeded_course_ids") or refs) else "failed",
                     errors=errors, result_refs=refs)


@handler("sync")
def handle_sync(db: Session, job: Job) -> JobResult:
    """逐课程 checkpoint：元数据/游标和待下载附件同事务保留，AUTH 等待成功 check。"""
    payload = dict(job.payload or {})
    connection_id = payload.get("connection_id")
    full = bool(payload.get("full", False))
    kinds = payload.get("kinds")
    if kinds is not None and (not isinstance(kinds, list)
                              or any(kind not in {"assignments", "resources"} for kind in kinds)):
        raise JobFailed("INVALID_PAYLOAD", "同步范围 kinds 无效", retryable=False)
    connection = db.get(Connection, connection_id) if isinstance(connection_id, str) else None
    if connection is None:
        raise JobFailed("INVALID_PAYLOAD", f"连接不存在：{connection_id}", retryable=False)
    if connection.status == "disabled":
        raise JobFailed("CONFLICT", "连接已停用，任务不执行", retryable=False, details={"connection_id": connection.id})
    course_ids = payload.get("course_ids")
    if not isinstance(course_ids, list) or not course_ids or any(not isinstance(c, str) or not c for c in course_ids):
        course_ids = bound_courses(db, connection.platform)
    if not course_ids:
        raise JobFailed("INVALID_PAYLOAD", "连接下没有已绑定来源的课程", retryable=False)
    payload["course_ids"] = list(dict.fromkeys(course_ids))
    pending = sync_pending_course_ids(payload)
    if pending is None:
        pending = list(payload["course_ids"])
    errors = list(payload.get("sync_errors") or [])
    refs = list(job.result_refs or [])
    succeeded = list(payload.get("sync_succeeded_course_ids") or [])
    checkpoints = dict(payload.get("course_checkpoints") or {})
    payload.update(sync_succeeded_course_ids=succeeded, course_checkpoints=checkpoints)
    _sync_checkpoint(db, job, payload, pending, errors, refs, phase="sync")
    # A stale/retried queued job must not probe credentials or loop AUTH failures.
    if pending and (connection.status == "auth_required" or (payload.get("auth_required") and connection.status != "ok")):
        errors = [e for e in errors if e.get("code") != "AUTH_EXPIRED"]
        return _pause_auth_sync(db, job, connection, payload, pending, errors, refs,
                                AdapterError("AUTH_EXPIRED", "登录已失效，请先检查连接", retryable=False))
    if payload.get("auth_required") and connection.status == "ok":
        # Check can commit while a crashed/finishing job is still running and thus
        # not requeueable by the hook. The committed connection state authorizes
        # its later stale-worker continuation; no login/check is attempted here.
        payload["auth_required"] = False
        errors = [e for e in errors if e.get("code") != "AUTH_EXPIRED"]
        _sync_checkpoint(db, job, payload, pending, errors, refs, phase="sync")
    try:
        adapter = resolve_sync_adapter(connection.platform)
    except AdapterMissing as exc:
        raise JobFailed("DEPENDENCY_UNAVAILABLE", str(exc), retryable=False, details={"platform": connection.platform}) from None

    conn_info = {"platform": connection.platform, "connection_id": connection.id}
    for course_id in list(pending):
        check_cancel(db, job)
        db.refresh(connection)
        if connection.status == "disabled":
            raise JobFailed("CONFLICT", "连接已停用，任务不执行", retryable=False)
        if connection.status == "auth_required":
            return _pause_auth_sync(db, job, connection, payload, pending, errors, refs,
                                    AdapterError("AUTH_EXPIRED", "登录已失效，请先检查连接", retryable=False))
        checkpoint = checkpoints.get(course_id)
        if checkpoint is None:
            since = None if full else get_cursor(db, connection.id, course_id)
            try:
                envelope = adapter(conn_info, {"course_id": course_id, "since_cursor": since,
                                               "full": full, "kinds": kinds})
                stats = apply_sync_result(db, connection=connection, course_id=course_id, envelope=envelope)
                store_cursor(db, connection_id=connection.id, course_id=course_id,
                             cursor=(envelope.get("cursor") or {}).get("next"),
                             mode=envelope.get("mode") or "incremental")
            except AdapterError as exc:
                if exc.code == "AUTH_EXPIRED":
                    return _pause_auth_sync(db, job, connection, payload, pending, errors, refs, exc)
                errors.append(adapter_error_entry(exc, course_id))
                if exc.code == "NETWORK_ERROR":
                    mark_network_error(connection, exc.message)
            except SyncResultInvalid as exc:
                errors.append(job_error("INVALID_PAYLOAD", str(exc), retryable=False, details={"course_id": course_id}))
            else:
                if course_id not in succeeded:
                    succeeded.append(course_id)
                errors.extend(stats.errors)
                for ref in stats.refs:
                    if ref not in refs:
                        refs.append(ref)
                # Persist only source identities, never signed URLs/session material.
                sources = []
                for item in envelope["upserts"]["resources"]:
                    source = item.get("source_ref") or {}
                    resource_id = find_source_owner(db, OWNER_RESOURCE, connection.platform, source.get("external_id"))
                    resource = db.get(Resource, resource_id) if resource_id else None
                    if (resource is not None and resource.course_id == course_id
                            and resource.access_level == "full" and resource.file_id is None):
                        sources.append({"resource_id": resource.id, "external_id": source["external_id"]})
                checkpoint = {"phase": "attachments", "remaining_resources": sources}
                checkpoints[course_id] = checkpoint
                _sync_checkpoint(db, job, payload, pending, errors, refs, phase="attachments")
        if checkpoint is not None:
            factory = getattr(adapter, "protocol_factory", None)
            fetch = None
            if factory is not None:
                try:
                    protocol = factory(platform=connection.platform, connection_id=connection.id)
                    fetch = getattr(protocol, "fetch_resource_bytes", None)
                except Exception as exc:
                    translated = exc if isinstance(exc, AdapterError) else _translate_protocol_error(exc)
                    if translated is not None and translated.code == "AUTH_EXPIRED":
                        return _pause_auth_sync(db, job, connection, payload, pending, errors, refs, translated)
                    raise
            remaining = list(checkpoint.get("remaining_resources") or [])
            for item in list(remaining) if fetch is not None else []:
                check_cancel(db, job)
                db.refresh(connection)
                if connection.status == "disabled":
                    raise JobFailed("CONFLICT", "连接已停用，任务不执行", retryable=False)
                if connection.status == "auth_required":
                    return _pause_auth_sync(db, job, connection, payload, pending, errors, refs,
                                            AdapterError("AUTH_EXPIRED", "登录已失效，请先检查连接", retryable=False))
                resource = db.get(Resource, item["resource_id"])
                owner = find_source_owner(db, OWNER_RESOURCE, connection.platform, item["external_id"])
                if (resource is not None and resource.id == owner and resource.course_id == course_id
                        and resource.access_level == "full" and resource.file_id is None):
                    source = {"platform": connection.platform, "external_id": item["external_id"],
                              "observed_at": iso_utc(utc_now())}
                    try:
                        _fetch_synced_document(db, resource, source, fetch)
                    except JobFailed as exc:
                        if exc.code == "AUTH_EXPIRED":
                            return _pause_auth_sync(db, job, connection, payload, pending, errors, refs,
                                                    AdapterError(exc.code, exc.message, retryable=False))
                        if exc.code in ATTACHMENT_SKIP_CODES:
                            # Input-determined, non-retryable attachment problems (unsupported
                            # extension, content not matching its extension). The sync itself
                            # succeeded and the metadata is kept, so this must not turn the
                            # whole job into partial_failed. It is reported as a warning.
                            _note_skipped_attachment(payload, exc.code)
                        else:
                            errors.append(job_error(exc.code, exc.message, retryable=exc.retryable,
                                                    details={"course_id": course_id, "resource_id": resource.id}))
                    except Exception as exc:
                        translated = exc if isinstance(exc, AdapterError) else _translate_protocol_error(exc)
                        if translated is None:
                            raise
                        if translated.code == "AUTH_EXPIRED":
                            return _pause_auth_sync(db, job, connection, payload, pending, errors, refs, translated)
                        errors.append(adapter_error_entry(translated, course_id))
                remaining.remove(item)
                checkpoint["remaining_resources"] = list(remaining)
                _sync_checkpoint(db, job, payload, pending, errors, refs, phase="attachments")
            enqueue_build_tasks_job(db, course_id=course_id, reason="sync")
            checkpoints.pop(course_id, None)
        pending.remove(course_id)
        _sync_checkpoint(db, job, payload, pending, errors, refs, phase="sync")

    if succeeded:
        mark_connection_ok(connection)
    _sync_checkpoint(db, job, payload, pending, errors, refs, phase="done")
    skipped_warnings = _skipped_attachment_warnings(payload.get("skipped_attachments"))
    if skipped_warnings:
        job.warnings = list(job.warnings or []) + skipped_warnings
        db.commit()
    if errors:
        return JobResult(status="partial_failed" if (succeeded or refs) else "failed", errors=errors, result_refs=refs)
    return JobResult(status="succeeded", result_refs=refs)


def _fetch_synced_document(db: Session, resource: Resource, source: dict, fetch: Callable) -> None:
    payload = fetch(source["external_id"])
    if payload.get("status") != "ok":
        denial = payload.get("denial") or {}
        if (payload.get("status") == "denied" and isinstance(resource.size_bytes, int)
                and resource.size_bytes > MAX_DOWNLOAD_BYTES):
            # PD-1 size guard: the connector refuses a file above the policy cap. That is
            # an input-determined skip (metadata kept), not a lost permission — decide it
            # from the resource's own size and the adapter's own cap, not from wording.
            raise JobFailed("FILE_TOO_LARGE",
                            f"附件 {resource.size_bytes} 字节超过解析护栏上限 {MAX_DOWNLOAD_BYTES} 字节，"
                            "元数据已保留", retryable=False)
        raise JobFailed("RESOURCE_NOT_FOUND" if payload.get("status") == "gone" else "PERMISSION_DENIED",
                        denial.get("message") or "平台文件不可获取，元数据已保留", retryable=False)
    name = storage.safe_display_name(payload.get("filename"))
    mime = storage.EXTENSION_MIME.get(PurePosixPath(name).suffix.lower())
    if mime is None:
        raise JobFailed("UNSUPPORTED_MEDIA_TYPE", "附件格式暂不支持，元数据已保留", retryable=False)
    encoded = payload.get("data_base64")
    if not isinstance(encoded, str) or len(encoded) > (config.MAX_UPLOAD_BYTES + 2) // 3 * 4:
        raise JobFailed("FILE_TOO_LARGE", "连接器返回文件超限或载荷无效", retryable=False)
    try:
        data = base64.b64decode(encoded, validate=True)
    except ValueError:
        raise JobFailed("INVALID_PAYLOAD", "连接器文件编码无效", retryable=False) from None
    digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
    if len(data) != payload.get("size") or digest != payload.get("sha256"):
        raise JobFailed("INVALID_PAYLOAD", "连接器文件大小或哈希不匹配", retryable=False)
    if not data.startswith(b"%PDF-" if mime == "application/pdf" else b"PK\x03\x04"):
        raise JobFailed("CORRUPT_FILE", "附件内容与格式不符", retryable=False)
    file_id = new_id(db, FileRecord, "fil")
    stored = storage.store_bytes(file_id, data, display_name=name, mime_type=mime)
    record = FileRecord(id=file_id, original_name=stored.original_name, mime_type=mime,
                        size_bytes=len(data), content_hash=digest)
    db.add(record)
    _attach_file_to_resource(db, resource, record, source)
    db.flush()
    enqueue_parse_job(db, resource, reason="sync")
    db.commit()


def _resource_title(base_title: str | None, file_name: str, total_files: int) -> str:
    if not base_title:
        return file_name
    return base_title if total_files == 1 else f"{base_title} · {file_name}"


# ────────────────────────── 处理器：JSON 导出 ──────────────────────────


_EXPORT_SECTIONS = ("courses", "resources", "assignments", "recordings", "tasks", "plans", "feedback")


def _export_section_rows(db: Session, section: str) -> list[dict[str, Any]]:
    """按类别汇集契约形状记录（含 source_refs；资源 chunk_count 与读端点同口径派生）。"""
    if section == "courses":
        return [
            c.to_contract(refs_for(db, OWNER_COURSE, c.id))
            for c in db.execute(select(Course).order_by(Course.id)).scalars()
        ]
    if section == "resources":
        rows: list[dict[str, Any]] = []
        for resource in db.execute(select(Resource).order_by(Resource.id)).scalars():
            chunk_count: int | None = None
            if resource.parse_status == "ready":
                chunk_count = db.execute(
                    select(func.count())
                    .select_from(ContentChunk)
                    .where(
                        ContentChunk.resource_id == resource.id,
                        ContentChunk.resource_version == resource.version,
                    )
                ).scalar_one()
            rows.append(resource.to_contract(refs_for(db, OWNER_RESOURCE, resource.id), chunk_count=chunk_count))
        return rows
    if section == "assignments":
        return [
            a.to_contract(refs_for(db, OWNER_ASSIGNMENT, a.id))
            for a in db.execute(select(Assignment).order_by(Assignment.id)).scalars()
        ]
    if section == "recordings":
        return [
            r.to_contract(refs_for(db, OWNER_RECORDING, r.id))
            for r in db.execute(select(Recording).order_by(Recording.id)).scalars()
        ]
    if section == "tasks":
        return [
            t.to_contract(refs_for(db, OWNER_TASK, t.id))
            for t in db.execute(select(LearningTask).order_by(LearningTask.id)).scalars()
        ]
    if section == "plans":
        return [p.to_contract() for p in db.execute(select(Plan).order_by(Plan.created_at, Plan.id)).scalars()]
    if section == "feedback":
        return [f.to_contract() for f in db.execute(select(TaskFeedback).order_by(TaskFeedback.id)).scalars()]
    raise JobFailed("INVALID_PAYLOAD", f"未知导出类别：{section}", retryable=False)


@handler("export")
def handle_export(db: Session, job: Job) -> JobResult:
    """导出：按 payload.format 分派——json（按类别）或 ics（计划 + 截止）。

    产物为受控文件；result_refs 含 export_file 供 GET /files/{file_id} 下载。
    payload.include 仅对 json 生效（缺省导出全部类别）；落盘与元数据登记在取消检查点之间，
    取消时不留孤儿文件。
    """
    payload = job.payload or {}
    fmt = payload.get("format") or "json"
    if fmt == "ics":
        return _export_ics(db, job)
    if fmt != "json":
        raise JobFailed("INVALID_PAYLOAD", f"导出格式不合法：{fmt!r}", retryable=False)
    include = payload.get("include")
    if include is None:
        sections = list(_EXPORT_SECTIONS)
    elif isinstance(include, list) and all(isinstance(s, str) and s in _EXPORT_SECTIONS for s in include):
        wanted = set(include)
        sections = [s for s in _EXPORT_SECTIONS if s in wanted]
    else:
        raise JobFailed("INVALID_PAYLOAD", f"导出类别不合法：{include!r}", retryable=False)

    check_cancel(db, job)
    document = {
        "exported_at": iso_utc(utc_now()),
        "sections": {section: _export_section_rows(db, section) for section in sections},
    }
    data = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    file_id = new_id(db, FileRecord, "fil")
    stored = storage.store_bytes(
        file_id, data, display_name=f"study-export-{stamp}.json", mime_type="application/json"
    )
    try:
        check_cancel(db, job)
    except JobCancelled:
        storage.remove_blob(file_id)
        raise
    db.add(
        FileRecord(
            id=stored.file_id,
            original_name=stored.original_name,
            mime_type=stored.mime_type,
            size_bytes=stored.size_bytes,
            content_hash=stored.content_hash,
        )
    )
    refs = [{"type": "export_file", "id": file_id}]
    job.result_refs = refs
    db.commit()
    return JobResult(status="succeeded", result_refs=refs)


def _export_ics(db: Session, job: Job) -> JobResult:
    """ICS 导出：生效计划时间块 + 任务截止（B19）。

    范围与时区口径见 app/ics.py 模块注释；缺可用时间（无时区依据）时如实失败，
    不臆造时区。candidate 计划不导出（仅 status=active）；unknown 精度截止不导出。
    """
    check_cancel(db, job)
    availability = db.get(Availability, 1)
    if availability is None:
        raise JobFailed(
            "CONFLICT",
            "尚未设置可用时间（时区依据），无法导出 ICS；请先配置 /api/v1/preferences/availability",
            retryable=False,
        )
    tz = resolve_user_tz(availability.timezone)
    active = active_plan(db)

    tasks = {t.id: t for t in db.execute(select(LearningTask).order_by(LearningTask.id)).scalars()}
    events: list[Event] = []
    if active is not None:
        for block in sorted(active.blocks, key=lambda b: (b.starts_at, b.id)):
            task = tasks.get(block.task_id)
            if task is None:  # 引用缺失：不导出无标题依据的事件
                continue
            events.append(
                Event(
                    uid=f"{block.id}@study-workbench",
                    summary=task.title,
                    starts_at=block.starts_at,
                    ends_at=block.ends_at,
                )
            )
    for task in tasks.values():
        if task.due_at is None or task.archived_at is not None or task.status == "cancelled":
            continue
        if task.due_precision == "datetime":
            events.append(Event(uid=f"{task.id}@study-workbench", summary=task.title, starts_at=task.due_at))
        elif task.due_precision == "date":
            events.append(
                Event(uid=f"{task.id}@study-workbench", summary=task.title, starts_at=task.due_at, all_day=True)
            )
        # due_precision=unknown：不导出（不做精度臆断）

    data = build_ics(events=events, tz_name=availability.timezone, tz=tz, exported_at=utc_now()).encode("utf-8")
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    file_id = new_id(db, FileRecord, "fil")
    stored = storage.store_bytes(
        file_id, data, display_name=f"study-plan-{stamp}.ics", mime_type="text/calendar"
    )
    try:
        check_cancel(db, job)
    except JobCancelled:
        storage.remove_blob(file_id)
        raise
    db.add(
        FileRecord(
            id=stored.file_id,
            original_name=stored.original_name,
            mime_type=stored.mime_type,
            size_bytes=stored.size_bytes,
            content_hash=stored.content_hash,
        )
    )
    refs = [{"type": "export_file", "id": file_id}]
    job.result_refs = refs
    db.commit()
    return JobResult(status="succeeded", result_refs=refs)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from .connectors_registry import register_default_adapters
    from .log_redact import install_logging_redaction
    install_logging_redaction()
    register_default_adapters()
    log.info("数据库：%s", config.DATABASE_URL)
    try:
        run_forever()
    except KeyboardInterrupt:
        log.info("收到中断，退出")


if __name__ == "__main__":
    main()
