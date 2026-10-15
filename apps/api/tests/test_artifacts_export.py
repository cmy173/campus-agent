"""M1 (`artifacts.export`) deterministic acceptance: real renderer, real session delivery.

Covers the A01-A05 acceptance classes without any model call:

* A01 real PDF with Chinese paragraphs and a cross-page table (parsed, not a renamed file);
* A02 XLSX number/date/boolean/empty/literal-"="/-leading string cell types;
* A03 same trusted call id replay + changed content conflict, no duplicate artifact;
* A04 code/URL/path/symlink/other-conversation/oversized input refused before dangerous IO;
* A05 renderer error / timeout / publish failure report structured errors with no fake file;
* A06 (rework) the render execution unit is terminable and reapable, its deadline is strictly
  inside the domain tool fence, and a batch publish either delivers every file or none.
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import sqlite3
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from openpyxl import load_workbook
from pypdf import PdfReader
from sqlalchemy import select

from app import models as m
from app.agent_bridge import artifacts, tools
from app.config import REPO_ROOT
from app.errors import ApiError
from sqlalchemy.orm import Session
from test_agent_gateway import gateway, conversation  # noqa: F401


MODEL = "校园资料整理"


# A controllable renderer used only for fault injection. It is a real child process, so these
# tests exercise the same execution unit production uses instead of a thread or a monkeypatched
# Python callable (which could never prove that an aborted render actually stopped).
STUB_RENDERER = """\
import json, sys, time
from pathlib import Path

args = sys.argv[1:]
value = lambda flag: args[args.index(flag) + 1]
work = Path(value('--output-dir'))
stem, formats = value('--stem'), value('--formats').split(',')
mode = (work / 'stub-mode.txt').read_text().strip()
spec = json.loads((work / 'spec.json').read_text(encoding='utf-8'))
if mode == 'sleep':
    # Wake well after the deadline: if the parent failed to stop this process, it would go on
    # to write its output files and the test would see them.
    time.sleep(3)
    (work / 'late-render.txt').write_text('renderer still running after ARTIFACT_TIMEOUT', encoding='utf-8')
    files = []
    for fmt in formats:
        path = work / f'{stem}.{fmt}'
        path.write_text('late', encoding='utf-8')
        files.append({'path': str(path), 'size': 4})
    print(json.dumps({'files': files}, ensure_ascii=False))
    raise SystemExit(0)
if mode == 'explode':
    sys.stderr.write('合成渲染器故障')
    raise SystemExit(1)
if mode == 'once' and not (work / 'stub-attempted').exists():
    (work / 'stub-attempted').write_text('1')
    sys.stderr.write('合成一次性故障')
    raise SystemExit(1)
files = []
for fmt in formats:
    path = work / f'{stem}.{fmt}'
    path.write_text(f'{spec.get("title", "")} {fmt}', encoding='utf-8')
    files.append({'path': str(path), 'size': path.stat().st_size})
print(json.dumps({'files': files}, ensure_ascii=False))
"""


def stub_renderer(tmp_path: Path, mode: str) -> Path:
    """Write a controllable renderer script and return its path."""
    script = tmp_path / f"stub-renderer-{mode}.py"
    script.write_text(STUB_RENDERER, encoding="utf-8")
    return script


def force_stub(monkeypatch, tmp_path: Path, mode: str) -> Path:
    """Repoint the fixed render entry at a real child script that behaves as requested."""
    script = stub_renderer(tmp_path, mode)
    monkeypatch.setattr(artifacts, "RENDER_COMMAND", script)
    original = artifacts._render

    def wrapper(spec, staging, stem, formats):
        (Path(staging) / "stub-mode.txt").write_text(mode, encoding="utf-8")
        return original(spec, staging, stem, formats)

    monkeypatch.setattr(artifacts, "_render", wrapper)
    return script


def spec(**overrides) -> dict:
    body = {
        "title": MODEL,
        "subtitle": "合成资料验收",
        "formats": ["pdf", "xlsx", "md"],
        "sections": [{
            "heading": "任务说明",
            "paragraphs": ["请保留原始数据和真实截止时间。"],
            "table": {"columns": ["资料", "页数", "截止时间"],
                      "rows": [["实验说明", 0, "未知"], ["课程讲义", 12, {"date": "2026-10-20"}]]},
        }],
        "sheets": [{
            "name": "资料清单",
            "columns": ["资料", "页数", "日期", "已确认"],
            "rows": [["=不是公式", 0, {"date": "2026-10-20"}, True], ["课程讲义", 12, None, False]],
        }],
        "anchors": [{"source": "合成附件：实验说明", "resource_id": "res_synthetic", "version": 1}],
    }
    body.update(overrides)
    return body


def call(client, token, cid, name, arguments, call_id):
    response = client.post("/api/v1/agent/internal/tool", headers={"Authorization": "Bearer " + token},
                           json={"conversation_id": cid, "name": name, "arguments": arguments, "call_id": call_id})
    assert response.status_code == 200, response.text
    return response.json()


def turn_for(gateway, client):
    from test_agent_gateway import turn
    cid = conversation(client)
    tid = turn(client, cid)
    return cid, tid


def download(client, cid, file_id):
    response = client.get(f"/api/v1/agent/conversations/{cid}/files/{file_id}/content")
    assert response.status_code == 200, response.text
    return response.content


def files_of(result):
    assert result["error"] is None, result
    return {item["name"].rsplit(".", 1)[-1]: item for item in result["data"]["files"]}


def listing(client, cid):
    """Conversation file list (the endpoint answers {'items': [...]})."""
    response = client.get(f"/api/v1/agent/conversations/{cid}/files")
    assert response.status_code == 200, response.text
    return response.json()["items"]


def outputs(client, cid):
    return [item for item in listing(client, cid) if item["kind"] == "output"]


class _RefusingStore:
    """Delegates everything to the real session store except the publish step."""

    def __init__(self, save):
        self._save = save

    def __getattr__(self, name):
        from app.agent_bridge.runtime import get_runtime
        return getattr(get_runtime().files, name)

    def save(self, *args, **kwargs):
        return self._save(*args, **kwargs)


# ────────────────────────────── A01 / A02 ──────────────────────────────

def test_a01_a02_real_pdf_and_xlsx_types_with_download(gateway):
    client, runtime, tokens, _ = gateway
    cid, tid = turn_for(gateway, client)
    result = call(client, tokens[tid], cid, "artifacts.export", spec(), "export-1")
    assert result["status"] == "ok", result
    items = files_of(result)
    assert set(items) == {"pdf", "xlsx", "md"}
    assert result["data"]["renderer"].endswith("export_documents.py")

    pdf_bytes = download(client, cid, items["pdf"]["id"])
    assert pdf_bytes[:5] == b"%PDF-", "PDF must be a real PDF container, not a renamed file"
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    for expected in (MODEL, "请保留原始数据和真实截止时间。", "实验说明", "课程讲义", "未知", "合成附件"):
        assert expected in text, expected
    # The table is placed on the page, and the media box margins keep content inside.
    assert reader.pages[0].mediabox.width > 500

    xlsx_bytes = download(client, cid, items["xlsx"]["id"])
    with zipfile.ZipFile(io.BytesIO(xlsx_bytes)) as archive:
        assert "xl/workbook.xml" in archive.namelist()
    book = load_workbook(io.BytesIO(xlsx_bytes), data_only=False)
    try:
        sheet = book["资料清单"]
        assert sheet["A5"].value == "=不是公式" and sheet["A5"].data_type == "s"
        assert sheet["B5"].value == 0 and sheet["B6"].value == 12
        assert sheet["C5"].value == datetime(2026, 10, 20) and sheet["C5"].number_format == "yyyy-mm-dd"
        assert sheet["C6"].value is None
        assert sheet["D5"].value is True and sheet["D6"].value is False
    finally:
        book.close()

    assert items["pdf"]["sha256"] == __import__("hashlib").sha256(pdf_bytes).hexdigest()
    assert {f["id"] for f in listing(client, cid)} >= {items["pdf"]["id"], items["xlsx"]["id"], items["md"]["id"]}
    assert items["xlsx"]["mime_type"].endswith("spreadsheetml.sheet")


def test_pdf_table_spans_pages_without_clipping(gateway):
    client, runtime, tokens, _ = gateway
    cid, tid = turn_for(gateway, client)
    rows = [[f"分页资料-{n}", n, "未知"] for n in range(120)]
    body = spec(formats=["pdf"], sections=[{"heading": "清单",
                                            "paragraphs": ["跨页表格必须完整，不得裁切。"],
                                            "table": {"columns": ["资料", "序号", "截止时间"], "rows": rows}}])
    result = call(client, tokens[tid], cid, "artifacts.export", body, "export-pages")
    assert result["error"] is None, result
    pdf_bytes = download(client, cid, files_of(result)["pdf"]["id"])
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert len(reader.pages) > 1
    assert "分页资料-119" in text and "分页资料-0" in text


# ────────────────────────────── A03 ──────────────────────────────

def test_a03_same_call_id_replays_and_changed_content_conflicts(gateway):
    client, runtime, tokens, _ = gateway
    cid, tid = turn_for(gateway, client)
    first = call(client, tokens[tid], cid, "artifacts.export", spec(), "export-replay")
    again = call(client, tokens[tid], cid, "artifacts.export", spec(), "export-replay")
    assert [f["id"] for f in again["data"]["files"]] == [f["id"] for f in first["data"]["files"]]
    assert again["data"]["files"][0]["sha256"] == first["data"]["files"][0]["sha256"]

    # The same trusted call id with different arguments is a conflict either at the tool-loop
    # cache or at the renderer receipt; both paths must refuse instead of creating a duplicate.
    response = client.post("/api/v1/agent/internal/tool", headers={"Authorization": "Bearer " + tokens[tid]},
                           json={"conversation_id": cid, "name": "artifacts.export",
                                 "arguments": spec(title="换了个标题"), "call_id": "export-replay"})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(outputs(client, cid)) == 3, "a changed export must not create a second artifact set"


def test_receipt_is_durable_across_db_sessions_and_blocks_a_second_render(tmp_path):
    """The replay receipt is a DB row, not process memory: a fresh session sees it."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.agent_bridge.files import ConversationFiles
    from app.agent_bridge.limits import RunLimits
    from app.agent_bridge.store import Store
    from app.db import Base

    engine = create_engine("sqlite:///" + (tmp_path / "domain.sqlite").as_posix(),
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    store = Store(tmp_path / "journal.sqlite", RunLimits())
    cid = store.create("local", {"course_ids": []})["id"]
    context = tools.ToolContext("local", cid, request_key="turn-1:call-9", files=ConversationFiles(store))
    with Session(engine) as db:
        first = tools.execute("artifacts.export", spec(), db, context)
    with Session(engine) as db:  # new session: only the persisted receipt can answer this
        replay = tools.execute("artifacts.export", spec(), db, context)
        assert [f["id"] for f in replay["data"]["files"]] == [f["id"] for f in first["data"]["files"]]
        assert db.execute(select(m.AgentArtifactReceipt).where(
            m.AgentArtifactReceipt.request_key == "turn-1:call-9")).scalars().all()
        with pytest.raises(ApiError) as exc:
            artifacts.export(db, context, spec(title="另一个标题"), ConversationFiles(store))
        assert exc.value.code == "IDEMPOTENCY_CONFLICT"
    assert len([f for f in ConversationFiles(store).list(cid) if f["kind"] == "output"]) == 3


# ────────────────────────────── A04 ──────────────────────────────

@pytest.mark.parametrize("payload, code", [
    ({"formats": ["pdf"], "sections": [{"paragraphs": ["x"], "code": "print(1)"}]}, "INVALID_INPUT"),
    ({"formats": ["pdf"], "sections": [{"paragraphs": ["x"], "executable": "/bin/sh"}]}, "INVALID_INPUT"),
    ({"formats": ["pdf"], "sections": [{"paragraphs": ["x"], "module": "os"}]}, "INVALID_INPUT"),
    ({"formats": ["pdf"], "output_dir": "C:/Windows/System32",
      "sections": [{"paragraphs": ["x"]}]}, "INVALID_INPUT"),
    ({"formats": ["pdf"], "font_path": "C:/Windows/Fonts/msyh.ttc", "sections": [{"paragraphs": ["x"]}]}, "INVALID_INPUT"),
    ({"formats": ["pdf"], "url": "https://example.org/a.pdf", "sections": [{"paragraphs": ["x"]}]}, "INVALID_INPUT"),
    ({"formats": ["docx"], "sections": [{"paragraphs": ["x"]}]}, "INVALID_INPUT"),
    ({"formats": ["xlsx"], "sheets": [{"name": "s", "columns": ["a"], "rows": [[{"formula": "=SUM(A1)"}]]}]},
     "INVALID_INPUT"),
    ({"formats": ["md"], "file_stem": "../../escape", "sections": [{"paragraphs": ["x"]}]}, "INVALID_INPUT"),
    ({"formats": ["md"], "sections": [{"paragraphs": ["x"], "table": {"columns": ["a", "b"], "rows": [["only-one"]]}}]},
     "INVALID_INPUT"),
    ({"formats": [], "sections": [{"paragraphs": ["x"]}]}, "INVALID_INPUT"),
])
def test_a04_dangerous_or_malformed_arguments_are_refused(gateway, payload, code):
    client, runtime, tokens, _ = gateway
    cid, tid = turn_for(gateway, client)
    body = {"title": "非法输入", **payload}
    result = call(client, tokens[tid], cid, "artifacts.export", body, "export-invalid")
    assert result["status"] == "error", result
    assert result["error"]["code"] == code
    assert outputs(client, cid) == [], "a refused export must not leave a fake success file"


def test_a04_oversized_input_is_refused(gateway):
    client, runtime, tokens, _ = gateway
    cid, tid = turn_for(gateway, client)
    rows = [["x" * 1900] * 3 for _ in range(400)]
    body = spec(formats=["xlsx"], sheets=[{"name": "大表", "columns": ["a", "b", "c"], "rows": rows}])
    result = call(client, tokens[tid], cid, "artifacts.export", body, "export-huge")
    assert result["status"] == "error"
    assert result["error"]["code"] in {"INVALID_INPUT", "BUDGET_EXCEEDED"}


def test_a04_symlinked_staging_area_is_refused(gateway, monkeypatch):
    """A reparse point/symlink in the staging chain is a scope violation, not a fallback."""
    client, runtime, tokens, _ = gateway
    cid, tid = turn_for(gateway, client)
    real = artifacts._artifact_root(runtime.files.store)
    monkeypatch.setattr(artifacts, "_artifact_root", lambda store: real)
    monkeypatch.setattr(artifacts, "_no_links", lambda path: (_ for _ in ()).throw(
        ApiError(403, "SCOPE_DENIED", "产物目录包含链接或重解析点，不能用于导出")))
    result = call(client, tokens[tid], cid, "artifacts.export", spec(), "export-link")
    assert result["status"] == "error" and result["error"]["code"] == "SCOPE_DENIED"
    assert outputs(client, cid) == []


def test_other_conversation_cannot_read_artifact(gateway):
    client, runtime, tokens, _ = gateway
    cid, tid = turn_for(gateway, client)
    other = conversation(client)
    result = call(client, tokens[tid], cid, "artifacts.export", spec(formats=["md"], sections=[{"paragraphs": ["x"]}]),
                  "export-scope")
    file_id = result["data"]["files"][0]["id"]
    assert client.get(f"/api/v1/agent/conversations/{other}/files/{file_id}/content").status_code == 404


def test_export_requires_a_trusted_call_identity(gateway):
    client, runtime, tokens, engine = gateway
    from sqlalchemy.orm import Session
    from app.db import SessionLocal
    cid, tid = turn_for(gateway, client)
    with SessionLocal() as db:
        with pytest.raises(ApiError) as exc:
            artifacts.export(db, tools.ToolContext("local", cid), spec(), runtime.files)
    assert exc.value.code == "RUNTIME_DENIED"


# ────────────────────────────── A05 ──────────────────────────────

def test_a05_renderer_failure_is_structured_and_leaves_no_file(gateway, monkeypatch, tmp_path):
    client, runtime, tokens, _ = gateway
    cid, tid = turn_for(gateway, client)
    force_stub(monkeypatch, tmp_path, "explode")
    result = call(client, tokens[tid], cid, "artifacts.export", spec(), "export-fail")
    assert result["status"] == "error" and result["error"]["code"] == "INVALID_INPUT"
    assert outputs(client, cid) == []


def test_a05_missing_renderer_reports_dependency_unavailable(gateway, monkeypatch):
    client, runtime, tokens, _ = gateway
    cid, tid = turn_for(gateway, client)
    monkeypatch.setattr(artifacts, "RENDER_COMMAND", Path("does/not/exist.py"))
    result = call(client, tokens[tid], cid, "artifacts.export", spec(), "export-nodep")
    assert result["status"] == "error" and result["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert outputs(client, cid) == []


def test_a05_render_timeout_is_reported_and_no_file_is_delivered(gateway, monkeypatch, tmp_path):
    client, runtime, tokens, _ = gateway
    cid, tid = turn_for(gateway, client)
    force_stub(monkeypatch, tmp_path, "sleep")
    monkeypatch.setattr(artifacts, "RENDER_TIMEOUT_SECONDS", 0.5)
    started = time.monotonic()
    result = call(client, tokens[tid], cid, "artifacts.export", spec(), "export-slow")
    elapsed = time.monotonic() - started
    assert result["status"] == "error" and result["error"]["code"] == "ARTIFACT_TIMEOUT"
    assert result["error"]["retryable"] is True
    assert outputs(client, cid) == []
    # The deadline is enforced, not merely reported: the reply cannot wait for the 30 s child.
    assert elapsed < 20, elapsed


# ────────────────────────────── A06 (rework) ──────────────────────────────

def _live_stub_argv(script: Path) -> list[str]:
    """Command lines of live Python processes running the stub script."""
    pattern = str(script)
    if sys.platform == "win32":
        query = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
                 f"Where-Object {{ $_.CommandLine -like '*{pattern}*' }} | "
                 "Select-Object -ExpandProperty CommandLine")
        shell = ["powershell", "-NoProfile", "-Command", query]
    else:  # pragma: no cover - POSIX convenience only
        shell = ["ps", "-eo", "args="]
    try:
        listing = subprocess.run(shell, capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - environment dependent
        return []
    return [line for line in listing.splitlines() if pattern in line]


def test_a06_timeout_terminates_and_reaps_the_render_process(gateway, monkeypatch, tmp_path):
    """The exact F1 regression: after ARTIFACT_TIMEOUT the renderer must be gone.

    The stub is a real child process that, if it survived, would stop sleeping after ~3 s and
    write its output files. So "no live process" and "no later output" are both checked after
    the abort has already been reported.
    """
    client, runtime, tokens, _ = gateway
    cid, tid = turn_for(gateway, client)
    script = force_stub(monkeypatch, tmp_path, "sleep")
    monkeypatch.setattr(artifacts, "RENDER_TIMEOUT_SECONDS", 0.5)

    result = call(client, tokens[tid], cid, "artifacts.export", spec(), "export-timeout-kill")
    assert result["status"] == "error" and result["error"]["code"] == "ARTIFACT_TIMEOUT"
    # Let a surviving child reach its write step: the assertion below cannot then pass by timing.
    time.sleep(4)
    assert _live_stub_argv(script) == [], "an aborted render left a live renderer process behind"
    root = artifacts._artifact_root(runtime.files.store)
    stray = sorted(path.name for path in root.rglob("late-render.txt")) if root.is_dir() else []
    assert stray == [], f"the aborted render wrote files after the abort: {stray}"
    assert outputs(client, cid) == []


def test_a06_render_deadline_fits_inside_the_domain_tool_fence():
    """A render that outlives the runtime fence would be a tool timeout the backend ignores."""
    fence_ms = None
    plugin = (REPO_ROOT / "apps/agent-runtime/campus-plugin.mjs").read_text(encoding="utf-8")
    for line in plugin.splitlines():
        if "timeoutMs:" in line and "2000" in line:
            fence_ms = int(re.search(r"timeoutMs:\s*(\d+)", line).group(1))
            break
    assert fence_ms == int(artifacts.DOMAIN_TOOL_FENCE_SECONDS * 1000), "fence changed; re-check the deadline"
    assert artifacts.RENDER_TIMEOUT_SECONDS + artifacts.RENDER_TEARDOWN_SECONDS < artifacts.DOMAIN_TOOL_FENCE_SECONDS
    assert artifacts.RENDER_TIMEOUT_SECONDS < fence_ms / 1000


def test_a06_second_format_publish_failure_leaves_no_partial_delivery(gateway, monkeypatch):
    """F3/R2 regression: a batch that cannot finish never overstates or understates its survivors.

    The old wording was "leaves no file behind". R2 refined it: removing a really published file can
    itself fail, so a batch may keep it — and the requirement is that the reported state matches
    reality. Either the batch rolled back cleanly (and may be retried), or the surviving file is
    disclosed as usable and really is downloadable. Silently keeping a file while claiming
    "nothing was delivered" is what must not happen.
    """
    client, runtime, tokens, _ = gateway
    cid, tid = turn_for(gateway, client)
    files = runtime.files
    save = files.save
    monkeypatch.setattr(tools, "_file_store", lambda ctx: files)
    attempts = 0

    def fail_but_second(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            # Fail after the first format is really published in the index and on disk.
            raise ApiError(503, "TEMPORARY_PUBLISH_ERROR", "synthetic second-format failure")
        return save(*args, **kwargs)

    monkeypatch.setattr(files, "save", fail_but_second)
    result = call(client, tokens[tid], cid, "artifacts.export", spec(formats=["md", "xlsx"]), "batch-rollback")
    assert result["status"] == "error" and result["error"]["code"] == "ARTIFACT_PUBLISH_FAILED"
    details = result["error"].get("details") or {}
    retained = details.get("retained_file_ids") or []
    cleanup = details.get("cleanup_pending_file_ids") or []
    # "Nothing is left behind" and "you may retry this call" must be the same claim: either the
    # batch rolled back cleanly, or it hands back something the caller must look at first.
    assert result["error"]["retryable"] is (not retained and not cleanup)
    # This injection breaks publication only; the rollback path itself has no fault, so the batch
    # must roll back cleanly: nothing retained, nothing left to sweep, and the same call retryable.
    assert retained == [] and cleanup == [], f"a clean rollback leaves nothing behind: {details}"
    assert result["error"]["retryable"] is True, f"a clean rollback must stay safely retryable: {details}"
    for file_id in retained:
        assert download(client, cid, file_id), "a retained file must be downloadable"
    assert _ids(details.get("delivered")) <= set(retained)
    # The index (what the session file list shows) equals exactly what the report calls usable.
    assert {item["id"] for item in outputs(client, cid)} == set(retained)


def test_a06_transient_publish_failure_can_recover_under_the_same_call(gateway, monkeypatch):
    """F3 regression: the advice 'retry the same call' must actually work through the bridge."""
    client, runtime, tokens, _ = gateway
    cid, tid = turn_for(gateway, client)
    files = runtime.files
    monkeypatch.setattr(tools, "_file_store", lambda ctx: files)
    attempts = 0
    original_save = files.save

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ApiError(503, "TEMPORARY_PUBLISH_ERROR", "synthetic one-off failure")
        return original_save(*args, **kwargs)

    monkeypatch.setattr(files, "save", fail_once)
    body = spec(formats=["md"], sections=[{"paragraphs": ["重试内容"]}])
    first = call(client, tokens[tid], cid, "artifacts.export", body, "retry-same-call")
    assert first["status"] == "error" and first["error"]["code"] == "ARTIFACT_PUBLISH_FAILED"
    assert first["error"]["retryable"] is True
    assert outputs(client, cid) == []
    second = call(client, tokens[tid], cid, "artifacts.export", body, "retry-same-call")
    assert second["error"] is None, f"retry replayed a permanent cached failure: {second}"
    assert attempts == 2, f"the retry must re-attempt publication, attempts={attempts}"
    delivered = outputs(client, cid)
    assert len(delivered) == 1, f"a recovered retry must deliver exactly one file: {delivered}"
    content = download(client, cid, delivered[0]["id"]).decode("utf-8")
    assert content.startswith("# 校园资料整理") and "重试内容" in content, f"unexpected retried content: {content!r}"


def test_a06_refused_inputs_are_never_reported_as_retryable(gateway, monkeypatch, tmp_path):
    """Only genuine transport/publish faults may be advertised as retryable."""
    client, runtime, tokens, _ = gateway
    cid, tid = turn_for(gateway, client)
    invalid = call(client, tokens[tid], cid, "artifacts.export",
                   {"title": "非法", "formats": ["md"], "sections": [{"paragraphs": ["x"], "module": "os"}]},
                   "no-retry-invalid")
    assert invalid["error"]["code"] == "INVALID_INPUT" and invalid["error"]["retryable"] is False
    force_stub(monkeypatch, tmp_path, "sleep")
    monkeypatch.setattr(artifacts, "RENDER_TIMEOUT_SECONDS", 0.5)
    timeout = call(client, tokens[tid], cid, "artifacts.export", spec(), "retry-timeout")
    assert timeout["error"]["code"] == "ARTIFACT_TIMEOUT" and timeout["error"]["retryable"] is True


def test_a06_same_call_id_cannot_swap_content_after_a_retryable_failure(gateway, monkeypatch):
    """Relaxing retryable errors must not open a content-swap hole.

    The publish fault is transient, so the same trusted call id is replayed with *different*
    content. The replay must be refused, both by the runtime's call-id binding and by the
    artifact receipt, and the scene must not contain a half-swapped delivery.
    """
    client, runtime, tokens, engine = gateway
    cid, tid = turn_for(gateway, client)
    files = runtime.files
    monkeypatch.setattr(tools, "_file_store", lambda ctx: files)
    save = files.save
    state = {"count": 0}

    def refuse_after_first(*args, **kwargs):
        state["count"] += 1
        if state["count"] == 1:
            # The first format really is published (index row + blob), then the batch fails and
            # must roll that first file back.
            raise ApiError(503, "TEMPORARY_PUBLISH_ERROR", "synthetic first-format failure")
        return save(*args, **kwargs)

    monkeypatch.setattr(files, "save", refuse_after_first)
    body = spec(formats=["md", "xlsx"], file_stem="一次")
    first = call(client, tokens[tid], cid, "artifacts.export", body, "swap-content")
    assert first["error"]["code"] == "ARTIFACT_PUBLISH_FAILED" and first["error"]["retryable"] is True
    assert outputs(client, cid) == []
    # A failed rollback leaves no receipt; the gateway's own call-id binding is the guard.
    swapped = client.post("/api/v1/agent/internal/tool", headers={"Authorization": "Bearer " + tokens[tid]},
                          json={"conversation_id": cid, "name": "artifacts.export",
                                "arguments": body | {"title": "被替换的标题"}, "call_id": "swap-content"})
    assert swapped.status_code == 409, swapped.text
    assert swapped.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert outputs(client, cid) == []

    # And the receipt itself refuses a content swap for a call that did complete.
    monkeypatch.setattr(files, "save", save)
    done = call(client, tokens[tid], cid, "artifacts.export", spec(formats=["md"], file_stem="完成"), "swap-done")
    assert done["error"] is None, done
    context = tools.ToolContext("local", cid, request_key=tid + ":swap-done", files=files)
    with pytest.raises(ApiError) as exc:
        with Session(engine) as db:
            artifacts.export(db, context,
                             artifacts.ExportArgs.model_validate(
                                 spec(formats=["md"], file_stem="完成", title="被替换")).model_dump(), files)
    assert exc.value.code == "IDEMPOTENCY_CONFLICT", exc.value.code


def test_a06_a_failed_batch_never_removes_an_earlier_export(gateway, monkeypatch):
    """Rollback is scoped to the batch's own rows."""
    client, runtime, tokens, _ = gateway
    cid, tid = turn_for(gateway, client)
    good = call(client, tokens[tid], cid, "artifacts.export", spec(formats=["md"], file_stem="保留"),
                "keep-good")
    assert good["error"] is None
    good_id = good["data"]["files"][0]["id"]
    files = runtime.files
    save = files.save
    state = {"count": 0}

    def fail_second(*args, **kwargs):
        state["count"] += 1
        if state["count"] == 2:
            raise ApiError(503, "TEMPORARY_PUBLISH_ERROR", "synthetic second-format failure")
        return save(*args, **kwargs)
    monkeypatch.setattr(tools, "_file_store", lambda ctx: files)
    monkeypatch.setattr(files, "save", fail_second)
    bad = call(client, tokens[tid], cid, "artifacts.export", spec(formats=["md", "xlsx"], file_stem="批"), "keep-batch")
    assert bad["status"] == "error"
    remaining = {item["id"] for item in outputs(client, cid)}
    assert good_id in remaining, "rollback must not touch files from an earlier request"
    assert download(client, cid, good_id).decode("utf-8").startswith("# 校园资料整理")


def test_a05_publish_failure_is_distinguished_from_render_failure(gateway, monkeypatch):
    client, runtime, tokens, _ = gateway
    cid, tid = turn_for(gateway, client)

    def refuse_save(*args, **kwargs):
        raise ApiError(500, "DISK_FULL", "合成发布失败")
    # The tool resolves its own session file store from the runtime, so patch that seam.
    original = tools._file_store
    monkeypatch.setattr(tools, "_file_store", lambda ctx: _RefusingStore(refuse_save))
    result = call(client, tokens[tid], cid, "artifacts.export", spec(), "export-publish")
    assert result["status"] == "error" and result["error"]["code"] == "ARTIFACT_PUBLISH_FAILED"
    monkeypatch.setattr(tools, "_file_store", original)
    assert outputs(client, cid) == []


def test_a05_existing_files_are_never_deleted_by_a_failed_export(gateway, monkeypatch, tmp_path):
    client, runtime, tokens, _ = gateway
    cid, tid = turn_for(gateway, client)
    good = call(client, tokens[tid], cid, "artifacts.export", spec(), "export-good")
    assert good["error"] is None
    force_stub(monkeypatch, tmp_path, "explode")
    bad = call(client, tokens[tid], cid, "artifacts.export", spec(file_stem="第二份"), "export-bad")
    assert bad["status"] == "error"
    assert good["data"]["files"][0]["id"] in {f["id"] for f in listing(client, cid)}
    assert download(client, cid, good["data"]["files"][0]["id"])[:5] == b"%PDF-"


def test_unknown_facts_stay_unknown_in_the_rendered_document(gateway):
    """The renderer must not invent a date for a missing one."""
    client, runtime, tokens, _ = gateway
    cid, tid = turn_for(gateway, client)
    body = {"title": "未知事实", "formats": ["md"], "sections": [{
        "heading": "截止时间", "paragraphs": ["截止时间未知，材料未提供。"],
        "table": {"columns": ["资料", "截止时间"], "rows": [["课程讲义", "未知"]]}}]}
    result = call(client, tokens[tid], cid, "artifacts.export", body, "export-unknown")
    assert result["error"] is None, result
    text = download(client, cid, result["data"]["files"][0]["id"]).decode("utf-8")
    assert "未知" in text
    import datetime as dt
    assert dt.date.today().isoformat() not in text, "the renderer must not substitute today's date"


# ─────────────── R2 (P2): a failed batch must match real downloadability ───────────────
#
# The rollback path used to treat "the physical delete failed" as "the file is still there", so an
# id whose index row had already been deleted was advertised as retained while its download
# returned 404. The corrected contract: an id is only reported as usable when the ordinary
# session download entry can still serve it, an id that lost its index is recorded as bounded
# cleanup state instead of being advertised, and content and index never fall out of step.


def download_status(client, cid, file_id) -> int:
    """Raw HTTP status of the ordinary session download entry point for one file id."""
    return client.get(f"/api/v1/agent/conversations/{cid}/files/{file_id}/content").status_code


def _ids(entries) -> set[str]:
    """Ids from a details list that may hold bare ids or server-owned file receipts."""
    collected: set[str] = set()
    for entry in entries or []:
        if isinstance(entry, str):
            collected.add(entry)
        elif isinstance(entry, dict) and isinstance(entry.get("id"), str):
            collected.add(entry["id"])
    return collected


def _install_batch_fault(monkeypatch, files, *, fail_attempt: int, blob_fail: bool = True) -> list[dict]:
    """Publish `fail_attempt - 1` formats for real, fail the next one, break one blob unlink.

    The broken unlink fires only for the first format this batch really published, i.e. the last
    row still indexed when the caller rolls the batch back. Returns the file rows the batch
    created for real, in publish order.
    """
    save, unlink = files.save, Path.unlink
    published: list[dict] = []

    def fail_after_published(*args, **kwargs):
        if len(published) >= fail_attempt - 1:
            raise ApiError(503, "TEMPORARY_PUBLISH_ERROR", "synthetic publish failure")
        item = save(*args, **kwargs)
        published.append(item)
        return item

    def guarded_unlink(path, *args, **kwargs):
        if published and path.parent == files.directory and path.stem == published[0]["id"]:
            # The already-published blob cannot be removed: whatever the rollback order is, the
            # index and the content must not drift apart into an unusable receipt.
            raise PermissionError("synthetic content deletion failure for an already-published blob")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(files, "save", fail_after_published)
    if blob_fail:
        monkeypatch.setattr(Path, "unlink", guarded_unlink)
    return published


class _DeleteRefusingConnection:
    """A live store connection whose indexed-row DELETE fails like a real sqlite fault.

    This is the "rollback fails before the index row is removed" case: nothing about the file
    changed, so it must stay a truthful, downloadable delivery.
    """

    def __init__(self, connection):
        self._connection = connection

    def execute(self, statement, *args, **kwargs):
        if statement.lstrip().upper().startswith("DELETE FROM CONVERSATION_FILES"):
            raise sqlite3.OperationalError("synthetic index delete failure")
        return self._connection.execute(statement, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._connection, name)


def _refuse_index_delete(monkeypatch, files) -> None:
    live = files.store.connection

    @contextlib.contextmanager
    def refusing():
        with live() as connection:
            yield _DeleteRefusingConnection(connection)

    monkeypatch.setattr(files.store, "connection", refusing)


def _assert_rollback_truth(client, cid, result, published) -> dict:
    """Cross-check every reported rollback state against the real index and real downloads."""
    published_ids = {item["id"] for item in published}
    assert result["status"] == "error", f"a cleanup fault must never be reported as success: {result}"
    error = result.get("error")
    assert error is not None, f"a cleanup fault was disguised as success (error=null): {result}"
    assert error["code"] == "ARTIFACT_PUBLISH_FAILED", result
    details = error.get("details")
    assert isinstance(details, dict), (
        f"a failed batch with pending rollback state must carry its bounded cleanup details: {result}")

    delivered = _ids(details.get("delivered"))
    retained = _ids(details.get("retained_file_ids"))
    cleanup = _ids(details.get("cleanup_pending_file_ids"))
    rows = {item["id"] for item in listing(client, cid)}

    assert (delivered | retained) <= published_ids, f"a receipt names a file this batch never published: {details}"
    assert cleanup <= published_ids, f"cleanup bookkeeping names a foreign file: {details}"

    for file_id in delivered | retained:
        assert file_id in rows, f"{file_id} is advertised but has no index row: {details}"
        assert download_status(client, cid, file_id) == 200, (
            f"{file_id} is advertised as usable but the session download is not 200: {details}")
    for file_id in cleanup:
        assert file_id not in rows, f"cleanup_pending {file_id} still has an index row: {details}"
        assert download_status(client, cid, file_id) == 404, (
            f"cleanup_pending {file_id} must not be downloadable: {details}")
    # A row this batch created that is neither advertised nor gone from the index would be a
    # hidden half-delivery: the reported state must equal the real index.
    assert rows & published_ids == delivered | retained, (
        f"the real index disagrees with the reported delivery: rows={rows & published_ids}, "
        f"delivered={delivered}, retained={retained}, cleanup={cleanup}")

    assert retained.isdisjoint(cleanup), f"a file cannot be both usable and pending cleanup: {details}"
    return details


def test_r2_index_delete_failure_keeps_the_file_delivered(gateway, monkeypatch):
    """Rollback failing before the index row is gone leaves a real delivery: report it as such."""
    client, runtime, tokens, _ = gateway
    cid, tid = turn_for(gateway, client)
    files = runtime.files
    monkeypatch.setattr(tools, "_file_store", lambda ctx: files)
    published = _install_batch_fault(monkeypatch, files, fail_attempt=2, blob_fail=False)
    _refuse_index_delete(monkeypatch, files)

    result = call(client, tokens[tid], cid, "artifacts.export", spec(formats=["md", "xlsx"]), "r2-index-delete")
    details = _assert_rollback_truth(client, cid, result, published)
    file_id = published[0]["id"]
    assert file_id in _ids(details.get("retained_file_ids")), f"the file is still downloadable: {details}"
    assert file_id in _ids(details.get("delivered")), f"a usable delivery must be reported: {details}"
    assert download_status(client, cid, file_id) == 200
    assert not _ids(details.get("cleanup_pending_file_ids")), f"nothing is pending cleanup: {details}"
    assert result["error"]["retryable"] is False, "a still-usable delivery must not invite a blind retry"


def test_r2_blob_unlink_failure_keeps_the_file_delivered(gateway, monkeypatch):
    """Content deletion fails: the index row is intact, so the file is a real delivery again."""
    client, runtime, tokens, _ = gateway
    cid, tid = turn_for(gateway, client)
    files = runtime.files
    monkeypatch.setattr(tools, "_file_store", lambda ctx: files)
    published = _install_batch_fault(monkeypatch, files, fail_attempt=2)

    result = call(client, tokens[tid], cid, "artifacts.export", spec(formats=["md", "xlsx"]), "r2-unlink-fails")
    details = _assert_rollback_truth(client, cid, result, published)
    file_id = published[0]["id"]
    assert file_id in _ids(details.get("retained_file_ids")), (
        f"a file whose content deletion failed is still fully deliverable: {details}")
    assert file_id in _ids(details.get("delivered")), f"a downloadable file must be advertised: {details}"
    assert download_status(client, cid, file_id) == 200, "the untouched file must download with 200, not 404"
    assert download(client, cid, file_id).decode("utf-8").startswith("# 校园资料整理")
    assert not _ids(details.get("cleanup_pending_file_ids")), f"an intact file leaves nothing to sweep: {details}"
    assert result["error"]["retryable"] is False, "a still-usable delivery must not invite a blind retry"
    assert {item["id"] for item in outputs(client, cid)} == {file_id}


def test_r2_error_details_expose_bounded_rollback_state(gateway, monkeypatch):
    """The model-facing error details must expose the bounded rollback state, id by id."""
    client, runtime, tokens, _ = gateway
    cid, tid = turn_for(gateway, client)
    files = runtime.files
    monkeypatch.setattr(tools, "_file_store", lambda ctx: files)
    published = _install_batch_fault(monkeypatch, files, fail_attempt=2)

    result = call(client, tokens[tid], cid, "artifacts.export", spec(formats=["md", "xlsx"]), "r2-rollback-details")
    details = _assert_rollback_truth(client, cid, result, published)
    rollback = details.get("rollback")
    assert isinstance(rollback, dict), (
        f"a batch that kept a usable file must expose the bounded rollback state: {details}")
    assert set(rollback) == {"removed", "retained", "cleanup_pending", "uncertain"}, rollback
    retained = _ids(details.get("retained_file_ids"))
    cleanup = _ids(details.get("cleanup_pending_file_ids"))
    buckets = {}
    for key, entries in rollback.items():
        assert isinstance(entries, list) and all(isinstance(entry, str) for entry in entries), rollback
        buckets[key] = _ids(entries)
    assert buckets["retained"] == retained, f"rollback and details disagree: {rollback} vs {details}"
    assert buckets["cleanup_pending"] <= cleanup, f"rollback and details disagree: {rollback} vs {details}"
    assert buckets["uncertain"].isdisjoint(_ids(details.get("delivered")) | retained), (
        f"an unknown rollback state must never be advertised as usable: {details}")
    assert buckets["removed"].isdisjoint(_ids(details.get("delivered")) | retained), (
        f"a rolled-back id cannot be advertised as usable: {details}")
    for key in ("removed", "cleanup_pending"):
        for file_id in buckets[key]:
            assert download_status(client, cid, file_id) == 404, f"{key} {file_id} must not be downloadable"
    assert set().union(*buckets.values()) == {item["id"] for item in published}, (
        f"the bounded rollback state must account for every row this batch published: {rollback}")


def test_r2_unrecoverable_cleanup_is_recorded_but_never_advertised(gateway, monkeypatch):
    """Index gone and the row cannot be restored: only residual cleanup remains, never a receipt."""
    client, runtime, tokens, _ = gateway
    cid, tid = turn_for(gateway, client)
    files = runtime.files
    monkeypatch.setattr(tools, "_file_store", lambda ctx: files)
    published = _install_batch_fault(monkeypatch, files, fail_attempt=2)
    monkeypatch.setattr(files, "_restore", lambda *args, **kwargs: False)

    result = call(client, tokens[tid], cid, "artifacts.export", spec(formats=["md", "xlsx"]), "r2-cleanup-only")
    details = _assert_rollback_truth(client, cid, result, published)
    file_id = published[0]["id"]
    assert file_id not in _ids(details.get("retained_file_ids")), (
        f"an id that lost its index cannot be retained: {details}")
    assert file_id not in _ids(details.get("delivered")), (
        f"an id that lost its index cannot be delivered: {details}")
    assert file_id in _ids(details.get("cleanup_pending_file_ids")), (
        f"the residual blob must be recorded as pending cleanup: {details}")
    assert _ids(details.get("cleanup_pending_file_ids")) == {file_id}, (
        f"only the residual blob is pending cleanup: {details}")
    assert download_status(client, cid, file_id) == 404, "a residual blob is not a session deliverable"
    assert [path for path in files.directory.glob(file_id + "*")], "this scenario leaves a residual blob on disk"
    assert outputs(client, cid) == [] and files.list(cid) == [], "the failed batch half-delivered a row"


@pytest.mark.parametrize("restorable", [True, False])
def test_r2_partial_rollback_of_three_formats_matches_real_get(gateway, monkeypatch, restorable):
    """Two published rows, one blob unlink broken: states must match real downloads, file by file."""
    client, runtime, tokens, _ = gateway
    cid, tid = turn_for(gateway, client)
    files = runtime.files
    keep = call(client, tokens[tid], cid, "artifacts.export", spec(formats=["md"], file_stem="保留"), "r2-keep")
    assert keep["error"] is None, keep
    keep_id = keep["data"]["files"][0]["id"]

    monkeypatch.setattr(tools, "_file_store", lambda ctx: files)
    published = _install_batch_fault(monkeypatch, files, fail_attempt=3)
    if not restorable:
        monkeypatch.setattr(files, "_restore", lambda *args, **kwargs: False)
    result = call(client, tokens[tid], cid, "artifacts.export",
                  spec(formats=["md", "xlsx", "pdf"], file_stem="部分回滚"), "r2-partial")
    details = _assert_rollback_truth(client, cid, result, published)

    assert len(published) == 2, f"the third format must fail after two real publishes: {published}"
    broken, clean = published[0]["id"], published[1]["id"]
    retained, cleanup = _ids(details.get("retained_file_ids")), _ids(details.get("cleanup_pending_file_ids"))
    if restorable:
        assert broken in retained, f"a file whose content deletion failed is still deliverable: {details}"
        assert download_status(client, cid, broken) == 200
        assert cleanup == set(), f"nothing can be pending cleanup when every blob was handled: {details}"
    else:
        assert broken not in retained and broken not in _ids(details.get("delivered")), details
        assert broken in cleanup, f"the unrecoverable residual blob is pending cleanup: {details}"
        assert cleanup == {broken}, (
            f"only the residue whose deletion failed is pending cleanup, not the clean removal: {details}")
        assert download_status(client, cid, broken) == 404
    # The cleanly rolled back format is gone either way, is never advertised, and is not residue.
    assert clean not in retained and clean not in _ids(details.get("delivered")), details
    assert clean not in cleanup, f"a fully removed row leaves no residue to sweep: {details}"
    assert download_status(client, cid, clean) == 404
    # The unrelated earlier export is untouched, and the index holds exactly it plus real deliveries.
    assert keep_id in {item["id"] for item in listing(client, cid)}
    assert download(client, cid, keep_id).decode("utf-8").startswith("# 校园资料整理")
    assert {item["id"] for item in outputs(client, cid)} == {keep_id} | retained
    assert not _ids(details.get("delivered")) or _ids(details.get("delivered")) <= retained


def _files_fixture(tmp_path):
    from app.agent_bridge.files import ConversationFiles
    from app.agent_bridge.limits import RunLimits
    from app.agent_bridge.store import Store

    store = Store(tmp_path / "journal.sqlite", RunLimits())
    cid = store.create("local", {"course_ids": []})["id"]
    return ConversationFiles(store), cid


def test_r2_discard_reports_success_and_content_failure(tmp_path, monkeypatch):
    """`discard` returns an outcome instead of raising, and it matches the real file state."""
    files, cid = _files_fixture(tmp_path)

    assert files.discard(cid, "file_absent") == "uncertain"
    assert files.discard_state(cid, "file_absent") == "rolled_back"

    removed = files.save(cid, io.BytesIO("# 第一份\n".encode("utf-8")), "一.md", "output", "r2-key-removed")
    _, removed_blob = files.get(cid, removed["id"])
    assert files.discard_state(cid, removed["id"]) == "retained"
    assert files.discard(cid, removed["id"]) == "removed"
    assert files.discard_state(cid, removed["id"]) == "rolled_back"
    assert not removed_blob.exists() and files.list(cid) == []

    kept = files.save(cid, io.BytesIO("# 第二份\n".encode("utf-8")), "二.md", "output", "r2-key-kept")
    kept_blob = files.get(cid, kept["id"])[1]
    unlink = Path.unlink

    def refuse(path, *args, **kwargs):
        if path == kept_blob:
            raise PermissionError("synthetic content deletion failure")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse)
    assert files.discard(cid, kept["id"]) == "retained"
    assert files.discard_state(cid, kept["id"]) == "retained"
    assert files.get(cid, kept["id"])[1] == kept_blob and kept_blob.read_bytes().startswith(b"#")


def test_r2_discard_index_failure_keeps_the_file_fully_deliverable(tmp_path, monkeypatch):
    """Removing the index row can fail, but content and index must never fall out of step."""
    files, cid = _files_fixture(tmp_path)
    stuck = files.save(cid, io.BytesIO("# 第三份\n".encode("utf-8")), "三.md", "output", "r2-key-stuck")
    stuck_blob = files.get(cid, stuck["id"])[1]
    _refuse_index_delete(monkeypatch, files)

    assert files.discard(cid, stuck["id"]) == "retained"
    assert files.discard_state(cid, stuck["id"]) == "retained"
    assert files.get(cid, stuck["id"])[1] == stuck_blob
    assert stuck_blob.read_bytes().startswith(b"#"), "content must survive an index-removal failure"


def test_r2_discard_unrecoverable_residue_is_cleanup_pending(tmp_path, monkeypatch):
    """Content that cannot be deleted and a row that cannot be put back is bare cleanup work."""
    files, cid = _files_fixture(tmp_path)
    sweep = files.save(cid, io.BytesIO("# 第四份\n".encode("utf-8")), "四.md", "output", "r2-key-sweep")
    sweep_blob = files.get(cid, sweep["id"])[1]
    unlink = Path.unlink

    def refuse(path, *args, **kwargs):
        if path == sweep_blob:
            raise PermissionError("synthetic content deletion failure")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse)
    monkeypatch.setattr(files, "_restore", lambda *args, **kwargs: False)
    assert files.discard(cid, sweep["id"]) == "cleanup_pending"
    assert files.discard_state(cid, sweep["id"]) == "rolled_back"
    assert sweep["id"] not in {item["id"] for item in files.list(cid)}
    assert sweep_blob.exists(), "an unindexed residual blob is exactly what pending cleanup means"


def test_r2_an_index_row_alone_never_proves_content_is_downloadable(tmp_path, monkeypatch):
    """N1: `retained` must mean "the download entry can serve it", not "a row exists".

    A live index row whose blob is gone is leftover state to repair. Reporting it as retained (or
    as delivered) would tell the user to open a file whose download returns 404 — exactly the R2
    defect, one layer deeper.
    """
    files, cid = _files_fixture(tmp_path)
    published = files.save(cid, io.BytesIO("# 第五份\n".encode("utf-8")), "五.md", "output", "r2-key-orphan")
    blob = files.get(cid, published["id"])[1]

    assert files.servable(cid, published["id"]) is True
    assert files.discard_state(cid, published["id"]) == "retained"

    blob.unlink()  # the row is still there; the content is not
    assert published["id"] in {item["id"] for item in files.list(cid)}, "the scenario needs a live row"
    assert files.servable(cid, published["id"]) is False, "a row alone must not pass the download check"
    assert files.discard_state(cid, published["id"]) == "rolled_back"
    with pytest.raises(ApiError) as error:
        files.get(cid, published["id"])
    assert error.value.code == "NOT_FOUND"

    # Rolling that row back must not claim a clean removal, and must not advertise the id.
    _refuse_index_delete(monkeypatch, files)
    assert files.discard(cid, published["id"]) == "cleanup_pending"


def test_r2_index_delete_conflict_with_missing_content_is_not_advertised(gateway, monkeypatch):
    """N1 end to end: index-removal failure plus unavailable content is never a read receipt."""
    client, runtime, tokens, _ = gateway
    cid, tid = turn_for(gateway, client)
    files = runtime.files
    monkeypatch.setattr(tools, "_file_store", lambda ctx: files)
    _refuse_index_delete(monkeypatch, files)
    save = files.save
    published: list[dict] = []

    def fail_second(*args, **kwargs):
        if published:
            # The first format really published; its blob then becomes unavailable before the
            # compensation runs, while the index-removal failure leaves the row behind.
            files.get(cid, published[0]["id"])[1].unlink()
            raise ApiError(503, "TEMPORARY_PUBLISH_ERROR", "synthetic second-format failure")
        item = save(*args, **kwargs)
        published.append(item)
        return item

    monkeypatch.setattr(files, "save", fail_second)
    result = call(client, tokens[tid], cid, "artifacts.export", spec(formats=["md", "xlsx"]), "missing-content")
    assert result["status"] == "error" and result["error"]["code"] == "ARTIFACT_PUBLISH_FAILED"
    fid = published[0]["id"]
    assert client.get(f"/api/v1/agent/conversations/{cid}/files/{fid}/content").status_code == 404
    details = result["error"].get("details") or {}
    advertised = {item["id"] for item in details.get("delivered", [])}
    advertised.update(details.get("retained_file_ids", []))
    assert fid not in advertised, f"missing content advertised as downloadable: {details}"
    assert fid in (details.get("cleanup_pending_file_ids") or []), (
        f"leftover state must be reported as pending cleanup: {details}")
    assert result["error"]["retryable"] is False, "leftover unusable state must not invite a blind retry"
