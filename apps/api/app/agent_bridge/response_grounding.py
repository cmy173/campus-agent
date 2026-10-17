"""Trusted renderers for narrow, structured Agent answers."""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


_URL_PATTERN = re.compile(r"https?://[^\s<>()\[\]{}（）【】「」，,、；;'\"]+")


_ASSIGNMENT_NEXT_TOOLS = {"assignments.next_by_course", "campus_assignments_next_by_course"}
_ALLOWED_COMPANION_TOOLS = _ASSIGNMENT_NEXT_TOOLS | {
    "assignments.list", "campus_assignments_list", "courses.list", "campus_courses_list",
    "connections.status", "campus_connections_status", "campus.status", "campus_campus_status",
}
_ASSIGNMENT_LIST_TOOLS = {"assignments.list", "campus_assignments_list"}


def assignment_top_n_summary(events: list[dict], user_input: str | None) -> tuple[str, dict] | None:
    """Render an explicitly requested top-N due list from verified cached rows.

    This narrow renderer activates only when the user states a numeric top-N,
    the executor used due_soon with that exact limit, and every row includes its
    course label. Other assignment tasks keep the model's answer.
    """
    if not isinstance(user_input, str):
        return None
    match = re.search(r"(?:最近|最早|截止时间最近|截止日期最近)\s*(?:的)?\s*(\d+)\s*(?:项|个|条)?\s*作业", user_input)
    if not match:
        match = re.search(r"(?:作业.{0,12}?(?:前|最近)\s*)(\d+)\s*(?:项|个|条)", user_input)
    if not match:
        return None
    requested_limit = int(match.group(1))
    if not 1 <= requested_limit <= 50:
        return None

    observed_tools: list[str] = []
    results: list[dict] = []
    requested_query = False
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
            continue
        kind, data = event["kind"], event["data"]
        if kind == "tool_result":
            name = str(data.get("name") or "").lower()
            if name:
                observed_tools.append(name)
            if name not in _ASSIGNMENT_LIST_TOOLS:
                continue
            arguments = data.get("arguments")
            if isinstance(arguments, dict) and arguments.get("redacted") is True:
                arguments = arguments.get("safe_options")
            if isinstance(arguments, dict) and arguments.get("order_by") == "due_soon" and arguments.get("limit") == requested_limit:
                requested_query = True
            result = data.get("result")
            if isinstance(result, dict):
                results.append(result)
        elif kind == "tool_started":
            name = data.get("name")
            if isinstance(name, str):
                observed_tools.append(name.lower())
        elif kind == "harness_event":
            wrapper = data.get("event")
            if not isinstance(wrapper, dict) or wrapper.get("type") != "tool/call":
                continue
            payload = wrapper.get("data") if isinstance(wrapper.get("data"), dict) else {}
            name = str(payload.get("name") or "").lower()
            if name:
                observed_tools.append(name)
            if name not in _ASSIGNMENT_LIST_TOOLS:
                continue
            arguments = payload.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except (TypeError, ValueError):
                    arguments = None
            if isinstance(arguments, dict) and arguments.get("order_by") == "due_soon" and arguments.get("limit") == requested_limit:
                requested_query = True

    allowed = _ASSIGNMENT_LIST_TOOLS | {
        "connections.status", "campus_connections_status", "campus.status", "campus_campus_status",
        "campus_assignments_list",
    }
    if not results or not requested_query or any(name not in allowed for name in observed_tools):
        return None

    rows_by_identity: dict[tuple, dict] = {}
    input_row_count = 0
    saw_valid_result = False
    for result in results:
        if result.get("status") not in {"ok", "partial"}:
            continue
        saw_valid_result = True
        payload = result.get("data") if isinstance(result.get("data"), dict) else {}
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            course = _cell(item.get("course_title"))
            title = _cell(item.get("title"))
            if not course or not title:
                return None
            input_row_count += 1
            due = item.get("due_at")
            instant = None
            rendered_due = "截止时间未知"
            if isinstance(due, str):
                try:
                    instant = datetime.fromisoformat(due.replace("Z", "+00:00"))
                    if instant.tzinfo is None:
                        instant = instant.replace(tzinfo=timezone.utc)
                    rendered_due = instant.astimezone(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
                except ValueError:
                    return None
            term = _cell(item.get("course_term"))
            identity = item.get("id")
            if isinstance(identity, str) and identity:
                identity = ("id", identity)
            else:
                identity = ("row", course, term, title, due, _cell(item.get("submission_status")))
            rows_by_identity.setdefault(identity, {"course": course, "term": term,
                "title": title, "due": rendered_due, "instant": instant,
                "submission": _cell(item.get("submission_status"))})

    rows = list(rows_by_identity.values())
    duplicate_rows_collapsed = input_row_count - len(rows)
    rows.sort(key=lambda row: (row["instant"] is None,
                               row["instant"] or datetime.max.replace(tzinfo=timezone.utc),
                               row["course"], row["title"]))
    rows = rows[:requested_limit]
    if not saw_valid_result:
        return None
    if not rows:
        answer = "本机缓存中没有可列出的作业记录；这不能证明学校平台没有作业。"
        answer += "\n数据仅覆盖当前会话可读取的本机缓存，不能证明学校平台全量。"
        return answer, {"rows": 0, "courses": 0, "source": "assignments.list",
                        "rendered_from_tool_data": True, "verified": True,
                        "duplicate_rows_collapsed": duplicate_rows_collapsed}

    course_terms: dict[str, set[str]] = {}
    for row in rows:
        course_terms.setdefault(row["course"], set()).add(row["term"])
    lines = [f"按截止时间由近到远列出本机缓存中的前 {len(rows)} 项作业（时间为中国标准时间）：",
             "", "| 课程 | 作业 | 截止时间 | 提交状态 |", "| --- | --- | --- | --- |"]
    for row in rows:
        course = row["course"]
        if len(course_terms[course]) > 1 and row["term"]:
            course += f"（{row['term']}）"
        lines.append(f"| {course} | {row['title']} | {row['due']} | {row['submission'] or '未知'} |")
    lines.extend(["", "数据仅覆盖当前会话可读取的本机缓存，不能证明学校平台全量。"])
    return "\n".join(lines), {"rows": len(rows), "courses": len(course_terms),
                              "source": "assignments.list", "rendered_from_tool_data": True,
                              "verified": True, "unsupported_dates": 0,
                              "duplicate_rows_collapsed": duplicate_rows_collapsed}


def _cell(value: object, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    text = text[:limit].replace("\\", "\\\\").replace("|", "\\|")
    return text


def assignment_deadline_summary(events: list[dict]) -> tuple[str, dict] | None:
    """Render only verified facts when the turn is solely a per-course deadline lookup.

    A composed turn that used another tool family keeps the model's response. The
    dedicated query has an exact output contract, so its data can be presented without
    asking the model to rewrite date facts.
    """
    observed_tools: list[str] = []
    results: list[dict] = []
    cursor_inputs: dict[str, str] = {}
    successful_result_ids: set[str] = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        kind, data = event.get("kind"), event.get("data")
        if not isinstance(data, dict):
            continue
        if kind == "tool_result":
            name = data.get("name")
            if isinstance(name, str):
                observed_tools.append(name.lower())
            if str(name or "").lower() in _ASSIGNMENT_NEXT_TOOLS:
                result = data.get("result")
                if isinstance(result, dict):
                    results.append(result)
                    call_id = data.get("call_id") or data.get("callId")
                    if isinstance(call_id, str) and result.get("status") in {"ok", "partial"}:
                        successful_result_ids.add(call_id)
        elif kind == "harness_event":
            wrapper = data.get("event")
            if not isinstance(wrapper, dict) or wrapper.get("type") != "tool/call":
                continue
            payload = wrapper.get("data") if isinstance(wrapper.get("data"), dict) else {}
            name = payload.get("name")
            if isinstance(name, str):
                observed_tools.append(name.lower())
            if str(name or "").lower() in _ASSIGNMENT_NEXT_TOOLS:
                call_id = payload.get("callId") or payload.get("call_id")
                arguments = payload.get("arguments")
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except (TypeError, ValueError):
                        arguments = None
                if isinstance(call_id, str) and isinstance(arguments, dict):
                    cursor = arguments.get("cursor")
                    if isinstance(cursor, str) and cursor:
                        cursor_inputs[call_id] = cursor

    if not results or any(name not in _ALLOWED_COMPANION_TOOLS for name in observed_tools):
        return None

    # Later pages may add courses. Use the last returned row for any repeated course.
    rows_by_course: dict[str, dict] = {}
    course_states: dict[str, tuple[str, str, str, int]] = {}
    saw_valid_result = False
    produced_cursors: Counter[str] = Counter()
    consumed_cursors: Counter[str] = Counter()
    for result in results:
        if result.get("status") not in {"ok", "partial"}:
            continue
        saw_valid_result = True
        payload = result.get("data") if isinstance(result.get("data"), dict) else {}
        cursor = payload.get("next_cursor")
        if isinstance(cursor, str) and cursor:
            produced_cursors[cursor] += 1
        rows = payload.get("items") if isinstance(payload.get("items"), list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            course = _cell(row.get("course_title"))
            if course:
                course_key = _cell(row.get("course_id")) or course
                course_states[course_key] = (course, _cell(row.get("course_term")),
                    str(row.get("course_status") or ""), int(row.get("cached_assignment_count", 0) or 0))
            title = _cell(row.get("assignment_title"))
            due = _cell(row.get("due_at_local"), 64)
            if course and title and re.fullmatch(r"20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?\+08:00", due):
                course_key = _cell(row.get("course_id")) or course
                term = _cell(row.get("course_term"))
                rows_by_course[course_key] = {"course": course, "term": term, "title": title, "due": due}
    for event in events:
        if not isinstance(event, dict) or event.get("kind") != "tool_result":
            continue
        data = event.get("data")
        if not isinstance(data, dict) or str(data.get("name", "")).lower() not in _ASSIGNMENT_NEXT_TOOLS:
            continue
        call_id = data.get("call_id") or data.get("callId")
        if not isinstance(call_id, str) or call_id not in successful_result_ids:
            continue
        cursor = cursor_inputs.get(call_id)
        if cursor:
            consumed_cursors[cursor] += 1
    outstanding_cursors = produced_cursors - consumed_cursors
    has_more = any(count > 0 for count in outstanding_cursors.values())
    if not saw_valid_result:
        answer = ("我无法从本机缓存核验逐课程截止作业，因此没有给出截止日期。"
                  "请检查作业缓存；此任务没有触发校园同步或登录。")
        return answer, {"rows": 0, "courses": 0, "source": "assignments.next_by_course",
                        "rendered_from_tool_data": True, "verified": False}

    title_counts = {}
    for course_title, _, _, _ in course_states.values():
        title_counts[course_title] = title_counts.get(course_title, 0) + 1
    def course_label(title, term):
        return f"{title}（{term}）" if title_counts.get(title, 0) > 1 and term else title

    rows = sorted(rows_by_course.values(), key=lambda item: (item["due"], item["course"], item["term"]))
    if not course_states:
        answer = ("当前会话范围下没有找到本机缓存课程。数据只覆盖本机已导入课程，"
                  "不能据此推断学校平台没有课程。")
    else:
        lines = ["以下是当前会话范围内本机缓存课程的作业状态。已知截止时间的最近作业按时间排序（中国标准时间）："]
        if rows:
            lines.extend(["", "| 课程 | 下一项作业 | 截止时间 |", "| --- | --- | --- |"])
            lines.extend(f"| {course_label(item['course'], item['term'])} | {item['title']} | {item['due']} |" for item in rows)
        no_known_due = [(course_label(title, term), state, count)
                        for title, term, state, count in course_states.values()
                        if state in {"unknown_deadline_only", "no_known_deadline",
                                     "no_matching_deadline_in_range", "no_cached_assignments"}]
        if no_known_due:
            lines.extend(["", "没有可列出的已知截止日期："])
            for course, state, count in sorted(no_known_due):
                if state == "unknown_deadline_only":
                    detail = f"有 {count} 项缓存作业，但截止时间未知"
                elif state == "no_cached_assignments":
                    detail = "本机缓存没有作业记录（不代表学校端没有作业）"
                elif state == "no_matching_deadline_in_range":
                    detail = f"没有匹配所给日期范围的已知截止作业；未知截止缓存 {count} 项"
                else:
                    detail = f"没有已知截止日期；未知截止缓存 {count} 项"
                lines.append(f"- {course}：{detail}")
        lines.extend(["", "数据仅覆盖当前会话可读取的本机缓存，不能证明学校平台全量。"])
        if has_more:
            lines.append("结果仍有未读取的后续课程页，因此这不是全部缓存课程。")
        answer = "\n".join(lines)
    return answer, {"rows": len(rows), "courses": len(course_states), "source": "assignments.next_by_course",
                    "rendered_from_tool_data": True, "verified": saw_valid_result}


def citation_grounding_summary(events: list[dict], answer: str | None) -> tuple[str, dict] | None:
    """Disclose how many of the answer's links were actually observed in this turn.

    A real long-task answer can contain links the model reconstructed by guessing an ID
    pattern even though it never fetched that page. Such links must not look verified.
    This check is deliberately conservative and never rewrites links: a URL counts as
    observed only when its exact text or its path appears in this turn's tool results.
    When some links are unobserved, one short note is appended; links themselves stay
    untouched so the user can inspect them.
    """
    if not isinstance(answer, str) or not answer.strip():
        return None
    urls: list[str] = []
    for match in _URL_PATTERN.finditer(answer):
        url = match.group(0).rstrip(".,;:!?")
        if url and url not in urls:
            urls.append(url)
    if not urls:
        return None
    observed = "\n".join(json.dumps(event.get("data"), ensure_ascii=False)
                         for event in events if isinstance(event, dict))
    grounded, ungrounded = [], []
    for url in urls:
        path = urlsplit(url).path
        if url in observed or (len(path) > 1 and path in observed):
            grounded.append(url)
        else:
            ungrounded.append(url)
    meta = {"checked": len(urls), "grounded": len(grounded), "ungrounded": len(ungrounded),
            "method": "answer URL exact-or-path match against this turn's tool results",
            "links_rewritten": False}
    if not ungrounded:
        meta["status"] = "aligned"
        return answer, meta
    meta["status"] = "partially_ungrounded"
    note = (f"\n\n> 来源核对：本轮回答引用了 {len(urls)} 个链接，其中 {len(ungrounded)} 个未能在本轮"
            "工具结果中找到对应访问记录（可能是模型推测或记忆内容），请谨慎引用。")
    return answer + note, meta
