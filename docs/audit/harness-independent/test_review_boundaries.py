"""A paper identity must survive the real worker and tool adaptation."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "apps/api/tests"))
from test_agent_gateway import gateway, conversation, turn  # noqa: E402,F401
from test_reviews_agent import (  # noqa: E402,F401
    synthetic_course, review_env, mock_model, make_provider,
    request_body, call, run_worker_job, RES_READY,
)


def test_read_preserves_completed_paper_identity(gateway, mock_model):
    client, runtime, tokens, engine = gateway
    cid = conversation(client)
    tid = turn(client, cid)
    provider = make_provider(client, mock_model)
    body = request_body(provider, materials={"resources": [{"resource_id": RES_READY}], "assignments": []})
    accepted = call(client, tokens[tid], cid, "reviews.generate", body, "generate")
    assert accepted["error"] is None
    paper_id = accepted["data"]["paper"]["paper_id"]
    result = run_worker_job(engine, accepted["job_id"])
    assert result.status == "succeeded"
    read = call(client, tokens[tid], cid, "reviews.read", {"paper_id": paper_id}, "read")
    assert read["error"] is None
    assert read["data"]["questions"]
    assert read["data"]["paper"]["paper_id"] == paper_id, "Adapter dropped the business paper id"
