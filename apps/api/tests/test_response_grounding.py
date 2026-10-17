"""Answer-side citation verification must disclose, never rewrite, unobserved links."""
from app.agent_bridge.response_grounding import citation_grounding_summary


def _events(*texts: str) -> list[dict]:
    return [{"kind": "harness_event",
             "data": {"session_id": "session",
                      "event": {"type": "tool/result",
                                "data": {"message": {"content": [{"type": "text", "text": text}]}}}}}
            for text in texts]


def test_answers_without_links_are_not_touched() -> None:
    assert citation_grounding_summary(_events("no links here"), "没有问题") is None
    assert citation_grounding_summary(_events("x"), None) is None


def test_exact_links_observed_in_tool_results_are_aligned() -> None:
    answer = "见 https://example.org/a 与 https://example.org/b?x=1"
    result = citation_grounding_summary(
        _events("fetched https://example.org/a (HTTP 200)", "the page /b?x=1 was read"), answer)
    assert result is not None
    text, meta = result
    assert text == answer
    assert meta["status"] == "aligned"
    assert meta["checked"] == 2 and meta["ungrounded"] == 0


def test_relative_path_in_tool_result_grounds_absolute_answer_url() -> None:
    answer = "[活动](https://rd.zju.edu.cn/2026/1016/c25011a3215744/page.htm)"
    result = citation_grounding_summary(_events('href="/2026/1016/c25011a3215744/page.htm"'), answer)
    assert result is not None
    assert result[1]["status"] == "aligned"


def test_unobserved_link_is_disclosed_once_without_rewriting_the_answer() -> None:
    answer = "来源一 https://example.org/ok；来源二 https://rd.zju.edu.cn/2026/1016/c25011a9999999/page.htm"
    result = citation_grounding_summary(_events("https://example.org/ok"), answer)
    assert result is not None
    text, meta = result
    assert meta["status"] == "partially_ungrounded"
    assert meta["checked"] == 2 and meta["grounded"] == 1 and meta["ungrounded"] == 1
    assert meta["links_rewritten"] is False
    assert text.startswith(answer), "the model answer must be preserved verbatim"
    assert "来源核对" in text and "1 个未能" in text
    # The disclosure must not introduce the unobserved URL again or drop the real one.
    assert text.count("https://rd.zju.edu.cn/2026/1016/c25011a9999999/page.htm") == 1
    assert text.count("https://example.org/ok") == 1


def test_duplicate_links_are_counted_once() -> None:
    result = citation_grounding_summary(_events("https://example.org/ok"), "a https://example.org/ok b https://example.org/ok")
    assert result is not None
    assert result[1]["checked"] == 1
