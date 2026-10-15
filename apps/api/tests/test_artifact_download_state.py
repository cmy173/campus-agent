"""Independent controls retained from the Harness acceptance review."""
import io
import sqlite3

from test_artifacts_export import _files_fixture


def test_download_check_keeps_ownership_and_content_boundaries(tmp_path):
    files, cid = _files_fixture(tmp_path)
    item = files.save(cid, io.BytesIO(b"independent synthetic content"), "probe.md", "output", "probe")
    blob = files.get(cid, item["id"])[1]
    assert files.servable(cid, item["id"]) is True
    assert files.servable("another-conversation", item["id"]) is False
    assert blob.read_bytes() == b"independent synthetic content"
    blob.unlink()
    assert files.servable(cid, item["id"]) is False
    assert files.discard_state(cid, item["id"]) != "retained"


def test_unreadable_index_is_unknown_instead_of_a_download_claim(tmp_path, monkeypatch):
    files, cid = _files_fixture(tmp_path)
    item = files.save(cid, io.BytesIO(b"probe"), "probe.md", "output", "probe")

    def unavailable():
        raise sqlite3.OperationalError("synthetic index unavailable")

    monkeypatch.setattr(files.store, "connection", unavailable)
    assert files.servable(cid, item["id"]) is None
    assert files.discard_state(cid, item["id"]) == "unknown"
