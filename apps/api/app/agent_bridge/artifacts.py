"""Trusted document export (`artifacts.export`): contract validation, fixed renderer, session delivery.

The model may only supply *data* (title/sections/sheets/anchor strings). It cannot name a
program, module, interpreter, output directory, font, or remote URL. Rendering runs the
repository's existing renderer
(`apps/agent-runtime/skills/document-artifacts/scripts/export_documents.py`) as a
server-owned child process: the interpreter and the script path are module constants, and
every model-supplied value travels as JSON data inside a file under the staging directory —
never in argv. This is not shell access and opens no `run_code`/PTC/workflow capability.

Using a child process (rather than a thread) is what makes the deadline enforceable: on
timeout the child is terminated *and* reaped before the caller is told anything, so no
renderer I/O and no late publish can happen after a reported abort.

Delivery uses the existing conversation file store so every artifact gets a real receipt,
a session-scoped download id and a content hash. A render that succeeds while the copy
into the session file area fails is reported as `ARTIFACT_PUBLISH_FAILED` (rendered but
not delivered) and never as a fake success.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..config import REPO_ROOT
from ..errors import ApiError
from .files import ConversationFiles
from .store import Store

# Fixed, server-owned render entry points. `RENDER_COMMAND` is the single seam: production
# always points at the repository renderer script, tests may repoint it while keeping the
# same "server-owned command + data-only file input" shape.
RENDERER_PATH = REPO_ROOT / "apps/agent-runtime/skills/document-artifacts/scripts/export_documents.py"
RENDER_COMMAND: Path = RENDERER_PATH
RENDER_INTERPRETER: str = sys.executable

LOG = logging.getLogger("app.agent_bridge.artifacts")

# Fixed, server-side ceilings. They are contract limits, not model suggestions.
MAX_TITLE = 200
MAX_TEXT = 20_000
MAX_ANCHORS = 50
MAX_ANCHOR = 500
MAX_SECTIONS = 40
MAX_SHEETS = 10
MAX_ROWS = 500
MAX_COLUMNS = 30
MAX_PARAGRAPHS = 50
MAX_BULLETS = 100
MAX_SPEC_BYTES = 512 * 1024

# `campus-plugin.mjs` fences every domain tool call at 20 s. A render that outlives that
# fence would be reported as a tool timeout while the backend kept working, so the render
# deadline must finish strictly inside it — including process teardown, reading the output
# and publishing into the session file area.
DOMAIN_TOOL_FENCE_SECONDS = 20.0
RENDER_TEARDOWN_SECONDS = 2.0
RENDER_TIMEOUT_SECONDS = 15.0
if RENDER_TIMEOUT_SECONDS + RENDER_TEARDOWN_SECONDS >= DOMAIN_TOOL_FENCE_SECONDS:
    raise RuntimeError("渲染超时必须明显短于域工具围栏，否则模型会先看到工具超时")


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DateCell(Input):
    """Calendar date only; the renderer never derives or advances dates."""

    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def real_date(self):
        try:
            date.fromisoformat(self.date)
        except ValueError:
            raise ValueError("date 必须是有效的 YYYY-MM-DD 日期") from None
        return self


Cell = str | int | float | bool | None | DateCell

# The renderer also accepts a {"formula": "=..."} object. That shape is deliberately absent
# from this contract: agents supply text/numbers/dates/booleans only, and a string that
# merely starts with "=" stays literal text rather than becoming a live formula.
_COLUMN = Annotated[str, Field(min_length=1, max_length=200)]
_ROW = Annotated[list[Cell], Field(max_length=MAX_COLUMNS)]


class Sheet(Input):
    name: str = Field(min_length=1, max_length=31, pattern=r"^[^\\/*?:\[\]]+$")
    columns: list[_COLUMN] = Field(min_length=1, max_length=MAX_COLUMNS)
    rows: list[_ROW] = Field(default_factory=list, max_length=MAX_ROWS)

    @model_validator(mode="after")
    def rows_match_columns(self):
        for row in self.rows:
            if len(row) != len(self.columns):
                raise ValueError("每行单元格数量必须与 columns 一致")
        return self


class Table(Input):
    columns: list[_COLUMN] = Field(min_length=1, max_length=MAX_COLUMNS)
    rows: list[_ROW] = Field(default_factory=list, max_length=MAX_ROWS)

    @model_validator(mode="after")
    def rows_match_columns(self):
        for row in self.rows:
            if len(row) != len(self.columns):
                raise ValueError("每行单元格数量必须与 columns 一致")
        return self


class Section(Input):
    heading: str | None = Field(default=None, max_length=MAX_TITLE)
    paragraphs: list[str] = Field(default_factory=list, max_length=MAX_PARAGRAPHS)
    bullets: list[str] = Field(default_factory=list, max_length=MAX_BULLETS)
    table: Table | None = None

    @model_validator(mode="after")
    def not_empty(self):
        if not any((self.paragraphs, self.bullets, self.table)):
            raise ValueError("section 至少要有 paragraphs、bullets 或 table；空章节不产生文档")
        return self

    @model_validator(mode="after")
    def bounded_text(self):
        for value in [*self.paragraphs, *self.bullets]:
            if len(value) > MAX_TEXT:
                raise ValueError(f"单段文字不得超过 {MAX_TEXT} 字符")
        return self


class Anchor(Input):
    """Where a fact came from. Bounded strings; never a program or a remote URL to fetch."""

    source: str | None = Field(default=None, max_length=MAX_ANCHOR)
    resource_id: str | None = Field(default=None, max_length=128)
    version: int | None = Field(default=None, ge=1)
    locator: str | None = Field(default=None, max_length=200)
    chunk_id: str | None = Field(default=None, max_length=128)
    note: str | None = Field(default=None, max_length=MAX_ANCHOR)

    @model_validator(mode="after")
    def not_empty(self):
        if not any((self.source, self.resource_id, self.chunk_id, self.note)):
            raise ValueError("anchor 至少要给出 source、resource_id、chunk_id 或 note 之一")
        return self


class ExportArgs(Input):
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    subtitle: str | None = Field(default=None, max_length=MAX_TITLE)
    file_stem: str | None = Field(default=None, min_length=1, max_length=80)
    formats: list[Literal["pdf", "xlsx", "md"]] = Field(min_length=1, max_length=3)
    sections: list[Section] = Field(default_factory=list, max_length=MAX_SECTIONS)
    sheets: list[Sheet] = Field(default_factory=list, max_length=MAX_SHEETS)
    anchors: list[Anchor] = Field(default_factory=list, max_length=MAX_ANCHORS)

    @model_validator(mode="after")
    def complete_request(self):
        if len(set(self.formats)) != len(self.formats):
            raise ValueError("formats 不能重复")
        if {"pdf", "md"} & set(self.formats) and not self.sections:
            raise ValueError("pdf/md 至少需要一个 section")
        if "xlsx" in self.formats and not self.sheets:
            raise ValueError("xlsx 至少需要一个 sheet")
        names = [sheet.name.casefold() for sheet in self.sheets]
        if len(set(names)) != len(names):
            raise ValueError("工作表名称必须唯一")
        if self.file_stem and (re.search(r'[<>:"/\\|?*\x00-\x1f]', self.file_stem)
                               or self.file_stem != self.file_stem.strip()
                               or self.file_stem in {".", ".."} or self.file_stem.endswith(".")):
            raise ValueError("file_stem 必须是有效文件名，不能包含目录或保留字符")
        return self


def _renderer_script() -> Path:
    """The fixed renderer script. Never a model value; not configurable over the bridge."""
    path = Path(RENDER_COMMAND)
    try:
        if not path.is_file():
            raise OSError("renderer missing")
    except OSError as exc:
        raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "文档渲染器不可用，本轮不能生成 PDF/XLSX",
                       retryable=False) from exc
    return path


def _renderer_env() -> dict[str, str]:
    """Child environment with an explicit UTF-8, unbuffered text layer.

    The renderer's spec and its JSON result both carry Chinese text. Inheriting the host ANSI
    code page would make the child's own stdout encoding differ from UTF-8, so a filename or
    title would come back mojibake and never match the file on disk. Forcing UTF-8 here is
    server-owned configuration; the model supplies no environment value. Unbuffered output
    means the child's result reaches the parent on exit rather than being lost with the pipe.
    """
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _renderer_argv(script: Path, work: Path, stem: str, formats: list[str]) -> list[str]:
    """The complete child command: interpreter, script, then server-built values only.

    Model content never reaches argv — it is written to `spec.json` inside `work` and read by
    the renderer from `--input`, so a title or heading cannot become a flag, a program or a
    path. `--formats` is a closed token list and `--stem` is already sanitized (any directory
    separator or reserved character was rejected by `ExportArgs`).
    """
    return [RENDER_INTERPRETER, str(script), "--formats", ",".join(formats),
            "--input", str(work / "spec.json"), "--output-dir", str(work), "--stem", stem]


def _terminate(process: subprocess.Popen, grace: float = RENDER_TEARDOWN_SECONDS) -> None:
    """Terminate the child and reap it. Returns only once it can no longer run."""
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:  # pragma: no cover - already gone
            pass
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:  # pragma: no cover - already gone
                pass
    try:
        # Bounded final reap: never return while the child could still write.
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - defensive
        raise ApiError(500, "ARTIFACT_RENDER_FAILED", "渲染子进程无法回收，已停止导出") from exc


def _render(spec: dict, staging: Path, stem: str, formats: list[str]) -> dict:
    """Run the fixed renderer in a child process under a strict, enforceable deadline.

    Returns the renderer's own result on success. Raises `ARTIFACT_TIMEOUT` only after the
    child has been terminated and reaped; raises `"render-error:"` prefixed `ValueError` for a
    renderer fault so the caller can classify it with the other renderer faults.
    """
    script = _renderer_script()
    # Absolute, because the child runs with cwd=work: a relative staging directory would
    # otherwise make every path this function hands to the child resolve against itself.
    work = Path(staging).resolve()
    try:
        (work / "spec.json").write_text(json.dumps(spec, ensure_ascii=False, default=str), encoding="utf-8")
    except OSError as exc:
        raise ApiError(500, "ARTIFACT_STAGING_FAILED", "无法写入渲染输入文件，未生成文件") from exc

    command = _renderer_argv(script, work, stem, formats)
    try:
        process = subprocess.Popen(command, cwd=str(work), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                                   env=_renderer_env(), start_new_session=os.name != "nt")
    except OSError as exc:
        raise ApiError(503, "DEPENDENCY_UNAVAILABLE", "无法启动文档渲染器，本轮不能生成文件",
                       retryable=False) from exc

    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=RENDER_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate(process)
        stdout, stderr = _drain(process)
    if timed_out:
        # Terminal state reported only now: the renderer can no longer write or compete with
        # the caller's staging cleanup, so "aborted and not delivered" is literally true.
        raise ApiError(504, "ARTIFACT_TIMEOUT",
                       f"渲染超过 {RENDER_TIMEOUT_SECONDS:g} 秒上限，渲染进程已终止且未交付", retryable=True)
    if process.returncode != 0:
        detail = (stderr or b"").decode("utf-8", errors="replace").strip()[-400:]
        raise ValueError(f"render-error: {detail or '渲染器退出码 %s' % process.returncode}")
    try:
        outcome = json.loads((stdout or b"").decode("utf-8", errors="replace") or "{}")
    except ValueError as exc:
        raise ValueError(f"render-error: 渲染器输出无法解析（{exc}）") from exc
    if not isinstance(outcome, dict) or not isinstance(outcome.get("files"), list):
        raise ValueError("render-error: 渲染器返回了无法识别的结果")
    return outcome


def _drain(process: subprocess.Popen) -> tuple[bytes, bytes]:
    """Collect whatever a terminated child had buffered, without ever blocking the caller.

    The child is already dead and reaped, so only a pipe that some other process still holds
    could stall a read. Reading happens in a daemon thread with a hard wait, so a stuck pipe
    can never push this call past the tool fence.
    """
    out, err = b"", b""
    for stream in (process.stdout, process.stderr):
        if stream is None:
            continue
        try:
            data = _bounded_read(stream)
        except (OSError, ValueError):
            data = b""
        finally:
            try:
                stream.close()
            except OSError:  # pragma: no cover - defensive
                pass
        if stream is process.stdout:
            out = data
        else:
            err = data
    return out, err


def _bounded_read(stream, seconds: float = RENDER_TEARDOWN_SECONDS) -> bytes:
    box: dict[str, bytes] = {}

    def read():
        try:
            box["data"] = stream.read() or b""
        except (OSError, ValueError):  # pragma: no cover - closed by the child
            box["data"] = b""

    reader = threading.Thread(target=read, name="artifact-drain", daemon=True)
    reader.start()
    reader.join(seconds)
    return box.get("data", b"")


def _discard_published(files: ConversationFiles, cid: str, items: list[dict]) -> dict:
    """Roll back the rows this batch just created, and report what each removal achieved.

    Only the rows returned by this batch's own `save` calls are touched, so a pre-existing or
    foreign file can never be deleted. Every member is then classified with the download entry's own
    check: an id is only ever reported back to the model when the normal conversation file entry can
    really serve it (row *and* valid, present content), so "retained" and "actually downloadable"
    cannot drift apart.
    """
    states: dict[str, str] = {}
    for item in reversed(items):
        file_id = item.get("id")
        if not isinstance(file_id, str):
            continue
        try:
            outcome = files.discard(cid, file_id)
        except Exception as exc:  # noqa: BLE001 - reported to the caller as a real limit
            LOG.warning("artifact rollback raised for %s: %s: %s", file_id, type(exc).__name__, exc)
            outcome = 'uncertain'
        if outcome in ('removed', 'retained', 'cleanup_pending'):
            states[file_id] = outcome
        else:
            # The removal never proved anything: only the index may decide.
            states[file_id] = 'retained' if files.discard_state(cid, file_id) == 'retained' else 'uncertain'
    return {state: [file_id for file_id, value in states.items() if value == state]
            for state in ('removed', 'retained', 'cleanup_pending', 'uncertain')}


def _rollback_details(files: ConversationFiles, cid: str, published: list[dict],
                      rollback: dict, failed_format: str | None) -> dict:
    """Error details for a batch that could not finish, mapped onto real file states.

    Every id is re-checked with the *download entry's own* check (`files.discard_state` →
    `servable`), so a change that happened between the rollback attempt and this report cannot make
    the message lie. `retained_file_ids` holds only files the session file entry can still serve —
    an index row whose content is missing is leftover state, not a receipt. Ids that cannot be
    served are reported as pending cleanup and are never advertised as usable. `delivered` is
    filtered by the same rule.
    """
    retained: list[str] = []
    cleanup_pending: list[str] = []
    uncertain: list[str] = []
    for file_id in rollback["retained"]:
        if not isinstance(file_id, str):
            continue
        state = files.discard_state(cid, file_id)
        if state == 'retained':
            retained.append(file_id)
        elif state == 'rolled_back':
            cleanup_pending.append(file_id)
        else:
            uncertain.append(file_id)
    # A clean removal left nothing to sweep, so it is not cleanup work. It is re-checked only in
    # case the state changed afterwards and the id became usable again.
    for file_id in rollback["removed"]:
        if isinstance(file_id, str) and files.discard_state(cid, file_id) == 'retained':
            retained.append(file_id)
    # A residual blob is exactly the case "gone from the index, content left behind": pending
    # cleanup, never a usable receipt.
    for file_id in rollback["cleanup_pending"]:
        if not isinstance(file_id, str):
            continue
        if files.discard_state(cid, file_id) == 'retained':
            retained.append(file_id)
        elif file_id not in cleanup_pending:
            cleanup_pending.append(file_id)
    for file_id in rollback["uncertain"]:
        if not isinstance(file_id, str):
            continue
        state = files.discard_state(cid, file_id)
        if state == 'retained':
            retained.append(file_id)
        elif state == 'rolled_back':
            if file_id not in cleanup_pending:
                cleanup_pending.append(file_id)
        elif file_id not in uncertain:
            uncertain.append(file_id)
    usable = set(retained)
    delivered = [item for item in published if item.get("id") in usable]
    details = {"delivered": delivered, "retained_file_ids": retained,
               "cleanup_pending_file_ids": cleanup_pending,
               # The consuming gateway must be able to re-verify each advertised id against the
               # conversation download entry; an empty `delivered` would otherwise leave it with no
               # conversation to check, and it would have to drop the cleanup state entirely.
               "conversation_id": cid,
               "rollback": {"removed": [file_id for file_id in rollback["removed"] if isinstance(file_id, str)],
                            "retained": retained,
                            "cleanup_pending": cleanup_pending, "uncertain": uncertain}}
    if failed_format is not None:
        details["failed_format"] = failed_format
    return details


def _batch_failure(files: ConversationFiles, cid: str, published: list[dict], exc: Exception,
                   failed_format: str | None) -> ApiError:
    """Turn a failed batch into the honest structured error, or nothing if state is unreadable."""
    rollback = _discard_published(files, cid, published)
    details = _rollback_details(files, cid, published, rollback, failed_format)
    if not details["retained_file_ids"] and not details["cleanup_pending_file_ids"] and not details["rollback"]["uncertain"]:
        return ApiError(500, "ARTIFACT_PUBLISH_FAILED",
                        "文件已渲染但未交付任何文件（批次已回滚），可以安全重试同一调用",
                        retryable=True, details=details)
    suffix = ""
    if details["cleanup_pending_file_ids"]:
        suffix += f"；另有 {len(details['cleanup_pending_file_ids'])} 个文件仅残留待清理内容，不作为可用交付"
    if details["rollback"]["uncertain"]:
        suffix += f"；还有 {len(details['rollback']['uncertain'])} 个文件状态无法确认，不得当作已交付"
    return ApiError(500, "ARTIFACT_PUBLISH_FAILED",
                    "文件已渲染但批次未完成，部分已发布文件无法撤回；本会话当前保留了这些文件，"
                    "请先用它们，不要重复同一导出调用" + suffix,
                    retryable=False, details=details)


def _render_failure(files: ConversationFiles, cid: str, published: list[dict], exc: Exception,
                    failed_format: str | None) -> ApiError:
    """`_batch_failure`, but a failure while reporting must not escape as a raw 500."""
    try:
        return _batch_failure(files, cid, published, exc, failed_format)
    except Exception as report_exc:  # noqa: BLE001 - the honest fallback is still a structured error
        LOG.warning("artifact failure reporting failed: %s: %s", type(report_exc).__name__, report_exc)
        return ApiError(500, "ARTIFACT_PUBLISH_FAILED",
                        "文件已渲染但批次未完成，且回滚状态无法确认；请先用本会话文件列表中真实可见的文件，"
                        "不要重复同一导出调用", retryable=False,
                        details={"delivered": [], "retained_file_ids": [],
                                 "cleanup_pending_file_ids": [],
                                 "rollback": {"removed": [], "retained": [],
                                              "cleanup_pending": [], "uncertain": []}})


def export(db: Session, ctx, args: dict, files: ConversationFiles) -> dict:
    """Render and publish. Every failure path raises a structured ApiError."""
    if not getattr(ctx, "request_key", None):
        raise ApiError(403, "RUNTIME_DENIED", "导出需要可信调用标识")
    if len(json.dumps(args, ensure_ascii=False, default=str).encode()) > MAX_SPEC_BYTES:
        raise ApiError(413, "BUDGET_EXCEEDED", f"导出输入超过 {MAX_SPEC_BYTES} 字节上限，请拆分内容")

    request_key, identity = ctx.request_key, _identity(args)
    existing = _receipt(db, ctx.conversation_id, request_key)
    if existing is not None:
        if existing.request_hash != identity:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "同一导出调用不能更换内容；请使用新的调用")
        return json.loads(existing.result)

    formats = _renderer_formats(args["formats"])
    spec, stem = _renderer_spec(args), _safe_name(args.get("file_stem") or args["title"])
    root = _artifact_root(files.store)
    root.mkdir(parents=True, exist_ok=True)
    _no_links(root)
    staging = root / uuid.uuid4().hex
    try:
        staging.mkdir(parents=False, exist_ok=False)
    except OSError:
        raise ApiError(500, "ARTIFACT_STAGING_FAILED", "无法准备产物临时目录，未生成文件") from None

    rendered: dict[str, dict] = {}
    try:
        outcome = _render(spec, staging, stem, formats)
        produced = set()
        for item in outcome["files"]:
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                produced.add(Path(item["path"]).name)
        for fmt in formats:
            path = staging / f"{stem}.{fmt}"
            if not path.is_file() or path.stat().st_size == 0:
                raise ApiError(500, "ARTIFACT_RENDER_FAILED", f"渲染器未产生可交付的 {fmt} 文件")
            # Result-to-disk cross-check: the renderer must report the file we are about to
            # deliver, so a partial or differently-named output cannot pass as complete.
            if path.name not in produced:
                raise ApiError(500, "ARTIFACT_RENDER_FAILED", f"渲染器报告的成果与磁盘上的 {fmt} 文件不一致")
            rendered[fmt] = {"file": path, "size": path.stat().st_size,
                             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    except ApiError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except ValueError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise ApiError(400, "INVALID_INPUT", f"导出内容被渲染器拒绝：{exc}") from None
    except Exception:  # noqa: BLE001 - renderer faults surface as structure, not raw 500 text
        shutil.rmtree(staging, ignore_errors=True)
        raise ApiError(500, "ARTIFACT_RENDER_FAILED", "渲染失败，未产生可下载文件") from None

    # All-or-nothing batch: every format is rendered before the first file is published, and a
    # batch that cannot finish removes exactly the rows it created. "Not delivered" then means
    # zero files, and a successful batch means all of them.
    published: list[dict] = []
    try:
        for fmt in formats:
            with rendered[fmt]["file"].open("rb") as stream:
                published.append(files.save(ctx.conversation_id, stream, f"{stem}.{fmt}", "output",
                                            f"{request_key}:{fmt}", str(rendered[fmt]["file"])))
    except ApiError as exc:
        raise _render_failure(files, ctx.conversation_id, published, exc, fmt) from exc
    except OSError as exc:
        raise _render_failure(files, ctx.conversation_id, published, exc, None) from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    data = {"files": published, "renderer": "document-artifacts/export_documents.py",
            "anchors": list(args.get("anchors") or []), "unknown": "未知事实保持 unknown，渲染器不补全缺失字段",
            "limits": {"max_rows": MAX_ROWS, "max_columns": MAX_COLUMNS, "max_spec_bytes": MAX_SPEC_BYTES,
                       "timeout_seconds": RENDER_TIMEOUT_SECONDS}}
    try:
        db.add(m.AgentArtifactReceipt(id="art_" + uuid.uuid4().hex, conversation_id=ctx.conversation_id,
                                      request_key=request_key, request_hash=identity,
                                      result=json.dumps(data, ensure_ascii=False, sort_keys=True),
                                      created_at=datetime.now(timezone.utc)))
        db.commit()
    except Exception as exc:
        db.rollback()
        LOG.warning("artifact receipt not saved: %s: %s", type(exc).__name__, exc)
        # The files really are delivered; only the replay receipt is missing. Say so
        # instead of reporting a clean success that a later replay would duplicate.
        raise ApiError(500, "ARTIFACT_RECEIPT_FAILED",
                       "文件已发布但回执未保存；请先确认已下载的文件，不要重复导出同一调用",
                       retryable=False, details={"delivered": published}) from None
    return data


def _renderer_formats(formats: list[str]) -> list[str]:
    # The shared renderer only understands these three names; map explicitly instead of
    # passing model input through to the renderer.
    allowed = {"pdf": "pdf", "xlsx": "xlsx", "md": "md"}
    unknown = set(formats) - set(allowed)
    if unknown:
        raise ApiError(400, "INVALID_INPUT", "只支持 pdf、xlsx、md 三种格式")
    return [allowed[name] for name in formats]


def _cell(value: Any) -> Any:
    if isinstance(value, dict):
        # Pydantic dumped the DateCell submodel; the renderer wants the same shape.
        return {"date": value["date"]}
    return value


def _renderer_spec(args: dict) -> dict:
    """Translate the validated contract into the renderer's spec (data only)."""
    spec: dict[str, Any] = {"title": args["title"]}
    if args.get("subtitle"):
        spec["subtitle"] = args["subtitle"]
    if args.get("anchors"):
        sources = []
        for anchor in args["anchors"]:
            parts = [anchor.get("source") or anchor.get("note") or anchor.get("resource_id") or anchor.get("chunk_id")]
            if anchor.get("resource_id"):
                parts.append(f"资料 {anchor['resource_id']}")
            if anchor.get("version"):
                parts.append(f"版本 v{anchor['version']}")
            if anchor.get("locator"):
                parts.append(str(anchor["locator"]))
            sources.append("·".join(str(part) for part in parts if part))
        spec["sources"] = sources
    if args.get("sections"):
        sections = []
        for section in args["sections"]:
            rendered = {"paragraphs": list(section["paragraphs"]), "bullets": list(section["bullets"])}
            if section.get("heading"):
                rendered["heading"] = section["heading"]
            if section.get("table"):
                rendered["table"] = {"columns": list(section["table"]["columns"]),
                                     "rows": [[_cell(value) for value in row] for row in section["table"]["rows"]]}
            sections.append(rendered)
        spec["sections"] = sections
    if args.get("sheets"):
        spec["sheets"] = [{"name": sheet["name"], "columns": list(sheet["columns"]),
                           "rows": [[_cell(value) for value in row] for row in sheet["rows"]]}
                          for sheet in args["sheets"]]
    return spec


def _safe_name(text: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(text or "")).strip(" ._")
    return cleaned[:60] or "导出文档"


def _artifact_root(store: Store) -> Path:
    """Server-owned staging root, next to (not inside) the shared session file area."""
    return store.path.parent / "artifact-work"


def _no_links(path: Path) -> None:
    for entry in (*reversed(path.parents), path):
        try:
            metadata = entry.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ApiError(403, "SCOPE_DENIED", "产物目录包含链接或重解析点，不能用于导出")


def _identity(args: dict) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _receipt(db: Session, conversation_id: str, request_key: str) -> m.AgentArtifactReceipt | None:
    return db.execute(select(m.AgentArtifactReceipt).where(
        m.AgentArtifactReceipt.conversation_id == conversation_id,
        m.AgentArtifactReceipt.request_key == request_key,
    )).scalar_one_or_none()
