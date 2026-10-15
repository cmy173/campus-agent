"""Offline history pagination/search: synthetic journal only, no student data."""
import base64
import json

import pytest

from app.agent_bridge.store import Store
from app.errors import ApiError
from test_agent_gateway import gateway  # isolated HTTP/domain fixture


def populated(tmp_path, count=105):
    store = Store(tmp_path / "history.sqlite")
    ids = []
    for index in range(count):
        cid = store.create("owner-a", {"course_ids": [], "send_to_provider": True})["id"]
        tid, _ = store.enqueue(cid, "one", f"课表查询 {index}", None)
        store.finish(tid["id"], "succeeded", response="来源：合成数据")
        ids.append(cid)
    store.create("owner-b", {"course_ids": []})
    return store, ids


def test_history_pages_beyond_legacy_hundred_without_loss_or_duplicate(tmp_path):
    store, ids = populated(tmp_path)
    assert len(store.conversations("owner-a")) == 100  # legacy baseline
    cursor, seen, calls = None, [], 0
    while True:
        page = store.conversation_page("owner-a", cursor=cursor, limit=50)
        calls += 1
        seen += [item["id"] for item in page["items"]]
        assert all(item["title"].startswith("课表查询") for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert calls == 3
    assert len(seen) == len(set(seen)) == 105
    assert set(seen) == set(ids)


def test_equal_timestamps_have_stable_keyset_order(tmp_path):
    store, ids = populated(tmp_path, 6)
    with store.connection() as db:
        db.execute("UPDATE conversations SET updated_at='2026-10-15T02:00:00+00:00'")
    first = store.conversation_page("owner-a", limit=3)
    second = store.conversation_page("owner-a", limit=3, cursor=first["next_cursor"])
    assert [c["id"] for c in first["items"] + second["items"]] == sorted(ids, reverse=True)
    assert second["next_cursor"] is None


def test_search_literal_case_insensitive_and_only_own_messages(tmp_path):
    store, _ = populated(tmp_path, 1)
    cid = store.create("owner-a", {"course_ids": []})["id"]
    tid, _ = store.enqueue(cid, "one", "\n 明天有哪些课 \n" + "x" * 100, None)
    store.finish(tid["id"], "succeeded", response="WebVPN 100%_success")
    private = store.create("owner-b", {"course_ids": []})["id"]
    tid, _ = store.enqueue(private, "one", "WebVPN private", None)
    store.finish(tid["id"], "succeeded")
    for query in ("webvpn", "%_", " 明天有哪些课 "):
        page = store.conversation_page("owner-a", search=query)
        assert len(page["items"]) == 1
        assert page["items"][0]["id"] == cid
        assert len(page["items"][0]["title"]) <= 80
        assert "response" not in page["items"][0]
    assert store.conversation_page("owner-a", search="'")['items'] == []


def test_cursor_binding_cannot_cross_owner_or_search(tmp_path):
    store, _ = populated(tmp_path, 3)
    cursor = store.conversation_page("owner-a", search="课表", limit=1)["next_cursor"]
    for owner, search in (("owner-b", "课表"), ("owner-a", "other")):
        with pytest.raises(ApiError) as error:
            store.conversation_page(owner, search=search, cursor=cursor)
        assert error.value.code == "INVALID_INPUT"


@pytest.mark.parametrize("cursor", ["", "not-base64", "x" * 1025, base64.urlsafe_b64encode(b"[]").decode(),
                                    base64.urlsafe_b64encode(b"null").decode()])
def test_malformed_cursor_is_safe_user_error(tmp_path, cursor):
    store = Store(tmp_path / "empty.sqlite")
    with pytest.raises(ApiError) as error:
        store.conversation_page("owner-a", cursor=cursor)
    assert error.value.status_code == 400


def test_http_history_pagination_owner_and_validation(gateway):
    client, runtime, _, _ = gateway
    first = client.post("/api/v1/agent/conversations", json={"provider_id": "provider_a", "send_to_provider": True}).json()
    # HTTP owner is intentionally hidden; derive it from this isolated journal only.
    with runtime.store.connection() as db:
        owner = db.execute("SELECT owner FROM conversations WHERE id=?", (first["id"],)).fetchone()[0]
        for index in range(3):
            db.execute("INSERT INTO conversations VALUES (?,?,?,?,?)", (f"conv_synthetic{index}", owner,
                       json.dumps({"provider": {"id": "provider_a"}, "course_ids": [], "send_to_provider": True}),
                       "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"))
    response = client.get("/api/v1/agent/conversations?limit=2")
    assert response.status_code == 200
    page = response.json()
    assert len(page["items"]) == 2 and page["next_cursor"]
    next_page = client.get("/api/v1/agent/conversations", params={"limit": 2, "cursor": page["next_cursor"]}).json()
    assert len(next_page["items"]) == 2 and next_page["next_cursor"] is None
    assert {c["id"] for c in page["items"]}.isdisjoint(c["id"] for c in next_page["items"])
    assert "settings" not in page["items"][0]
    assert client.get("/api/v1/agent/conversations?limit=0").status_code == 400
    assert client.get("/api/v1/agent/conversations?cursor=broken").status_code == 400
