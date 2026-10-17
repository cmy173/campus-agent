"""Independent acceptance probes; production code and submitted tests are unchanged.

Rework note (F1): the render executor moved from an in-process thread to a server-owned child
process (`app.agent_bridge.artifacts._render`), because a thread could not be stopped. The
setup of `test_timeout_stops_renderer_before_reporting_abort` therefore drives the *new*
execution unit — a real controllable process — while keeping the original assertion intent
(and strengthening it): after the API reports the abort, the renderer must be terminated,
reaped, and unable to perform any further IO. Nothing was skipped, weakened or deleted.
"""
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "apps/api/tests"))
from test_agent_gateway import gateway  # noqa: E402,F401
from test_artifacts_export import call, spec, turn_for, outputs  # noqa: E402
from app.agent_bridge import artifacts, tools  # noqa: E402
from app.errors import ApiError  # noqa: E402


# A renderer that stays alive until it is stopped, and records its own PID. A surviving process
# is then directly observable as a live PID and as a file it keeps writing.
SLEEPING_RENDERER = """\
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
work = Path(args[args.index('--output-dir') + 1])
(work / 'renderer.pid').write_text(str(os.getpid()), encoding='utf-8')
notice = work / 'renderer-still-alive.txt'
stop = work / 'stop-renderer'
while not stop.exists():
    notice.write_text('alive', encoding='utf-8')
    time.sleep(0.05)
print('stopped', flush=True)
"""


def _pid_running(pid: int) -> bool:
    if os.name == "nt":  # pragma: no cover - Windows only
        import subprocess
        listing = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True).stdout
        return str(pid) in listing
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _deadline_passed(function, seconds: float) -> bool:
    """Bounded check: True when the call finished, False when it exceeded the deadline."""
    outcome = {}

    def run():
        try:
            outcome["value"] = function()
        except OSError:
            outcome["value"] = True

    import threading
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    return bool(outcome.get("value"))


def test_timeout_stops_renderer_before_reporting_abort(tmp_path, monkeypatch):
    """F1: the API must not report an abort while the renderer can still run and write."""
    script = tmp_path / "sleeping-renderer.py"
    script.write_text(SLEEPING_RENDERER, encoding="utf-8")
    work = tmp_path / "staging"
    work.mkdir()
    monkeypatch.setattr(artifacts, "RENDER_COMMAND", script)
    monkeypatch.setattr(artifacts, "RENDER_TIMEOUT_SECONDS", 0.5)

    started = time.monotonic()
    try:
        with pytest.raises(ApiError) as caught:
            artifacts._render({"title": "探针"}, work, "doc", ["md"])
        assert caught.value.code == "ARTIFACT_TIMEOUT"
        elapsed = time.monotonic() - started
        assert elapsed < 10, f"the abort must not wait for the renderer: {elapsed:.1f}s"
        pid_file = work / "renderer.pid"
        assert pid_file.is_file(), "the renderer never started; the probe proves nothing"
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        # Liveness comes from the OS, so a surviving process cannot hide: the API has already
        # reported the abort, and this checks the process is gone rather than merely idle.
        assert _deadline_passed(lambda: not _pid_running(pid), 5), (
            f"the API reported ARTIFACT_TIMEOUT while renderer pid {pid} was still alive")
        assert (work / "renderer-still-alive.txt").is_file(), "the renderer never did any IO"
    finally:
        (work / "stop-renderer").write_text("stop", encoding="utf-8")




def test_second_format_publish_failure_has_no_hidden_partial_delivery(gateway, monkeypatch):
    client, runtime, tokens, _ = gateway
    cid, tid = turn_for(gateway, client)
    files = runtime.files
    save = files.save
    monkeypatch.setattr(tools, "_file_store", lambda ctx: files)
    count = 0

    def fail_second(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise ApiError(503, "TEMPORARY_PUBLISH_ERROR", "synthetic second-format failure")
        return save(*args, **kwargs)

    monkeypatch.setattr(files, "save", fail_second)
    result = call(client, tokens[tid], cid, "artifacts.export", spec(formats=["md", "xlsx"]), "partial-publish")
    assert result["error"]["code"] == "ARTIFACT_PUBLISH_FAILED"
    delivered = outputs(client, cid)
    assert delivered == [], f"Error reports 'not delivered' but real file remains: {delivered}"


def test_publish_transient_failure_same_call_can_recover(gateway, monkeypatch):
    client, runtime, tokens, _ = gateway
    cid, tid = turn_for(gateway, client)
    files = runtime.files
    save = files.save
    monkeypatch.setattr(tools, "_file_store", lambda ctx: files)
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ApiError(503, "TEMPORARY_PUBLISH_ERROR", "synthetic one-off failure")
        return save(*args, **kwargs)

    monkeypatch.setattr(files, "save", fail_once)
    first = call(client, tokens[tid], cid, "artifacts.export", spec(formats=["md"]), "publish-retry")
    assert first["error"]["code"] == "ARTIFACT_PUBLISH_FAILED"
    second = call(client, tokens[tid], cid, "artifacts.export", spec(formats=["md"]), "publish-retry")
    assert second["error"] is None, f"Recommended retry replays permanent cached error; save attempts={attempts}"
