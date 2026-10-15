"""Budget and scope boundary regression independent of model or real school data."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app import models as m
from app.db import Base
from app.agent_bridge.tools import execute, ToolContext
from app.errors import ApiError


@pytest.fixture
def scoped():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([m.Course(id="a", title="A", term="test"), m.Course(id="b", title="B", term="test")])
        db.flush()
        db.add_all([m.Resource(id="ra", course_id="a", title="A", kind="document", version=1, parse_status="ready"),
                    m.Resource(id="rb", course_id="b", title="B", kind="document", version=1, parse_status="ready")])
        db.flush()
        yield db, ToolContext("local", "audit", frozenset({"a"}))
    engine.dispose()


def chunk(db, i, text, resource="ra"):
    db.add(m.ContentChunk(id=f"{resource}_{i}", resource_id=resource, resource_version=1,
                          ordinal=i, text=text, locator={"page": i+1}, provenance={"kind": "synthetic"}))
    db.commit()


def test_search_scope_applied_before_pagination_and_score(scoped):
    db, ctx = scoped
    for i in range(3):
        chunk(db, i, "needle authorized", "ra")
        chunk(db, i, "needle " * 100, "rb")  # foreign higher-scoring hits
    seen, cursor = [], None
    for _ in range(4):
        args = {"query": "needle", "limit": 1}
        if cursor: args["cursor"] = cursor
        r = execute("materials.search", args, db, ctx)
        seen.extend(i["chunk_id"] for i in r["data"]["items"])
        cursor = r["next_cursor"]
        assert "rb" not in str(r)
        if not cursor: break
    assert seen == ["ra_0", "ra_1", "ra_2"]


def test_single_huge_chunk_is_not_silently_lost(scoped):
    db, ctx = scoped
    chunk(db, 0, "文" * 24001)
    with pytest.raises(ApiError) as exc:
        execute("materials.read", {"resource_id": "ra", "expected_version": 1}, db, ctx)
    assert exc.value.code == "BUDGET_EXCEEDED"
    assert "单个正文片段" in exc.value.message


def test_read_limit_and_text_budget_share_cursor(scoped):
    db, ctx = scoped
    for i in range(3): chunk(db, i, "文" * 6000)
    first = execute("materials.read", {"resource_id": "ra", "expected_version": 1, "limit": 1}, db, ctx)
    second = execute("materials.read", {"resource_id": "ra", "expected_version": 1, "cursor": first["next_cursor"]}, db, ctx)
    assert [i["id"] for i in first["data"]["items"] + second["data"]["items"]] == ["ra_0", "ra_1", "ra_2"]
    assert second["next_cursor"] is None and not second["coverage"]["complete"]


def test_fifty_hits_stay_bounded_and_match_centered(scoped):
    db, ctx = scoped
    for i in range(50): chunk(db, i, "前" * 5000 + "needle" + "后" * 994)
    r = execute("materials.search", {"query": "needle", "limit": 50}, db, ctx)
    assert len(r["data"]["items"]) == 50
    assert sum(len(i["text"]) for i in r["data"]["items"]) == 24000
    for i in r["data"]["items"]:
        assert "needle" in i["text"] and i["text_is_excerpt"] and i["original_text_chars"] == 6000
        assert i["excerpt_start_offset"] > 0
    assert r["status"] == "partial" and not r["coverage"]["complete"]
