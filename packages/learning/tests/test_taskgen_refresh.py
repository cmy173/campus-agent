from learning.models import Resource
from learning.taskgen import build_tasks, TaskGenOptions


def test_inferred_estimate_refreshes_after_parse_but_user_estimate_is_preserved():
    resource = Resource(id="r", course_id="c", kind="slides", title="课件")
    initial = build_tasks([resource], [], []).tasks[0]
    assert initial.estimated_minutes == 60
    resource.extent_value = 10
    resource.extent_unit = "slide"
    refreshed = build_tasks([resource], [], [], TaskGenOptions(existing_tasks=[initial])).tasks[0]
    assert refreshed.estimated_minutes != 60
    refreshed.estimated_minutes = 37
    refreshed.estimate_provenance = "user"
    resource.extent_value = 20
    final = build_tasks([resource], [], [], TaskGenOptions(existing_tasks=[refreshed])).tasks[0]
    assert final.estimated_minutes == 37
