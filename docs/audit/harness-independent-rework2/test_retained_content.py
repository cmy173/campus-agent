"""Independent boundary: an index row alone must not prove a downloadable artifact."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "apps/api/tests"))
from test_agent_gateway import gateway  # noqa: E402,F401
from test_artifacts_export import call, spec, turn_for, _refuse_index_delete  # noqa: E402
from app.agent_bridge import tools  # noqa: E402
from app.errors import ApiError  # noqa: E402


def test_index_delete_failure_does_not_advertise_missing_content(gateway, monkeypatch):
    client, runtime, tokens, _ = gateway
    cid, tid = turn_for(gateway, client)
    files = runtime.files
    monkeypatch.setattr(tools, "_file_store", lambda ctx: files)
    _refuse_index_delete(monkeypatch, files)
    save = files.save
    published = []

    def fail_second(*args, **kwargs):
        if published:
            # The first save really succeeded. Simulate its blob becoming unavailable before
            # compensation, while the index-removal failure leaves the row behind.
            files.get(cid, published[0]["id"])[1].unlink()
            raise ApiError(503, "PUBLISH_UNAVAILABLE", "synthetic second-format failure")
        result = save(*args, **kwargs)
        published.append(result)
        return result

    monkeypatch.setattr(files, "save", fail_second)
    result = call(client, tokens[tid], cid, "artifacts.export", spec(formats=["md", "xlsx"]), "missing-blob")
    assert result["error"]["code"] == "ARTIFACT_PUBLISH_FAILED"
    fid = published[0]["id"]
    response = client.get(f"/api/v1/agent/conversations/{cid}/files/{fid}/content")
    assert response.status_code == 404, "Probe must reach the missing-content path"
    details = result["error"].get("details") or {}
    advertised = {item["id"] for item in details.get("delivered", [])}
    advertised.update(details.get("retained_file_ids", []))
    assert fid not in advertised, f"Missing content advertised as retained/downloadable: {details}"
