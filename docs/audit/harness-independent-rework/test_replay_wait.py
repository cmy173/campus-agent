"""Unseen replay input: the first generation call already contains a wait window."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "apps/api/tests"))
from test_agent_gateway import gateway, conversation, turn  # noqa: E402,F401
from test_reviews_agent import (  # noqa: E402,F401
    synthetic_course, review_env, mock_model, make_provider, request_body, RES_READY,
)
from test_artifacts_export import spec, call, turn_for  # noqa: E402
from app.agent_bridge import tools  # noqa: E402
from app.errors import ApiError  # noqa: E402


def test_first_call_with_explicit_wait_replays_under_same_id(gateway, mock_model):
    client, runtime, tokens, _ = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    provider = make_provider(client, mock_model)
    body = request_body(provider, materials={"resources": [{"resource_id": RES_READY}], "assignments": []})
    body["wait_seconds"] = 1
    headers = {"Authorization": "Bearer " + tokens[tid]}
    payload = {"conversation_id": cid, "name": "reviews.generate", "arguments": body, "call_id": "explicit-wait"}
    first = client.post("/api/v1/agent/internal/tool", headers=headers, json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["error"] is None, first.text
    second = client.post("/api/v1/agent/internal/tool", headers=headers, json=payload)
    assert second.status_code == 200, second.text
    assert second.json()["job_id"] == first.json()["job_id"]


def test_failed_blob_cleanup_does_not_advertise_deleted_file_receipt(gateway, monkeypatch):
    client, runtime, tokens, _ = gateway
    cid, tid = turn_for(gateway, client)
    files = runtime.files
    save, unlink = files.save, Path.unlink
    published = []
    monkeypatch.setattr(tools, "_file_store", lambda ctx: files)

    def fail_second(*args, **kwargs):
        if published:
            raise ApiError(503, "TEMPORARY_PUBLISH_ERROR", "synthetic second format failure")
        result = save(*args, **kwargs)
        published.append(result)
        return result

    def fail_blob_delete(path, *args, **kwargs):
        if published and path.parent == files.directory and path.name == published[0]["id"] + ".md":
            raise PermissionError("synthetic blob deletion failure after index removal")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(files, "save", fail_second)
    monkeypatch.setattr(Path, "unlink", fail_blob_delete)
    result = call(client, tokens[tid], cid, "artifacts.export", spec(formats=["md", "xlsx"]), "rollback-delete")
    assert result["error"]["code"] == "ARTIFACT_PUBLISH_FAILED"
    details = result["error"].get("details") or {}
    retained = details.get("retained_file_ids") or []
    assert retained, "Probe must reach the retained-files reporting path"
    for file_id in retained:
        response = client.get(f"/api/v1/agent/conversations/{cid}/files/{file_id}/content")
        assert response.status_code == 200, f"Error advertises retained delivered file {file_id}, but download returns {response.status_code}"
