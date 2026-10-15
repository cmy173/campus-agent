"""D16 · 三项产品裁决落地测试（coord v1.8《01-产品决策裁定》，2026-10-10）。

- PD-1：单文件解析上限 30MB → 64MB。默认值冻结；恰等上限通过、超 1 字节拒绝按新值验证。
- PD-2：mp4/视频类（kind=other）映射 reading → watching。识别（标题扩展名 / video MIME）、
  稳定 ID（watching 段）、估算（watching 默认值与校正系数）、完成标准、可正常排程。
- PD-3：已提交作业（submitted/graded）保留生成、标注 platform_submitted=True、不参与排程
  （不产生时间块、不进未安排清单）、不阻塞后继（取消除外）、修订释放旧块并给出变化原因。
  与「学习完成 ≠ 平台已提交」口径一致：status（学习进度）不被改写。
"""

from __future__ import annotations

from learning.models import (
    Availability,
    LearningTask,
    Plan,
    PlanBlock,
    Resource,
    WeeklyWindow,
)
from learning.parse import parse_document
from learning.parse.types import ParseErrorCode, ParseLimits
from learning.schedule.engine import ReasonCode, ScheduleConfig, ScheduleOptions, generate_plan
from learning.schedule.revise import ChangeKind, ChangeReason, revise_plan
from learning.schedule.validate import validate_plan
from learning.taskgen import (
    TaskGenConfig,
    build_tasks_json,
    resource_task_id,
    resource_task_type,
)

# ── 公共构造 ─────────────────────────────────────────────────────────────────

MONDAY = ((0, "19:00", "22:30"),)


def make_availability(windows=MONDAY, limit=None):
    return Availability(
        weekly_windows=[WeeklyWindow(weekday=w, start=s, end=e) for (w, s, e) in windows],
        daily_limit_minutes=limit,
    )


def make_options(**kwargs):
    return ScheduleOptions(
        horizon_start=kwargs.get("horizon_start", "2026-10-12T00:00+08:00"),
        horizon_end=kwargs.get("horizon_end", "2026-10-19T00:00+08:00"),
        reference_time=kwargs.get("reference_time", "2026-10-09T12:00+08:00"),
        locked_blocks=kwargs.get("locked_blocks", []),
        config=kwargs.get("config", ScheduleConfig()),
    )


def task(task_id, minutes, **kwargs):
    return LearningTask(
        id=task_id,
        course_id="crs_demo",
        type=kwargs.pop("type", "reading"),
        title=f"示例任务 {task_id}",
        estimated_minutes=minutes,
        **kwargs,
    )


def block(block_id, task_id, starts_at, ends_at, locked=False):
    return PlanBlock(id=block_id, task_id=task_id, starts_at=starts_at, ends_at=ends_at, locked=locked)


def make_plan(blocks):
    return Plan(
        horizon_start="2026-10-12T00:00+08:00",
        horizon_end="2026-10-19T00:00+08:00",
        input_revision=1,
        blocks=blocks,
    )


def unassigned_reasons(plan):
    return {item.task_id: item.reason_code for item in plan.unassigned}


def res_row(rid, title, kind="other", **over):
    row = {"id": rid, "course_id": "crs_v", "kind": kind, "title": title, "version": 1}
    row.update(over)
    return row


def asg_row(aid, title, status, due_at=None, **over):
    row = {
        "id": aid,
        "course_id": "crs_v",
        "title": title,
        "due_at": due_at,
        "due_precision": "datetime" if due_at else "unknown",
        "submission_status": status,
        "source_ref": {
            "platform": "xzzd",
            "external_id": f"xzzd-{aid}",
            "canonical_url": None,
            "observed_at": "2026-10-10T08:00:00Z",
        },
    }
    row.update(over)
    return row


# ── PD-1：解析护栏 30MB → 64MB ───────────────────────────────────────────────


def test_pd1_default_limit_is_64mb():
    assert ParseLimits().max_file_bytes == 64_000_000


def test_pd1_boundary_at_and_over_64mb_default():
    """恰等上限：通过大小护栏（随后因虚假文件损坏失败，但错误码非 FILE_TOO_LARGE）；
    超 1 字节：FILE_TOO_LARGE（按新护栏值验证）。"""
    data = b"\x00" * 64_000_000
    at_limit = parse_document(data, filename="big.docx")
    assert at_limit.error_code != ParseErrorCode.FILE_TOO_LARGE

    over = parse_document(data + b"\x00", filename="big.docx")
    assert over.error_code == ParseErrorCode.FILE_TOO_LARGE
    assert "64000000" in (over.message or "")


# ── PD-2：mp4/视频类 → watching ──────────────────────────────────────────────


def test_pd2_video_detection_by_extension_and_mime():
    assert resource_task_type(Resource(id="r1", course_id="c", kind="other", title="第1讲.mp4")) == "watching"
    assert resource_task_type(Resource(id="r2", course_id="c", kind="other", title="第1讲.MP4")) == "watching"
    assert resource_task_type(Resource(id="r3", course_id="c", kind="other", title="回放-01.mov")) == "watching"
    assert (
        resource_task_type(
            Resource(id="r4", course_id="c", kind="other", title="无扩展名视频", mime_type="video/mp4")
        )
        == "watching"
    )
    # 非视频保持 reading（含 kind=other 的非视频文件与扩展名中途出现的情况）
    assert resource_task_type(Resource(id="r5", course_id="c", kind="document", title="讲义.pdf")) == "reading"
    assert resource_task_type(Resource(id="r6", course_id="c", kind="other", title="课件01")) == "reading"
    assert resource_task_type(Resource(id="r7", course_id="c", kind="other", title="压缩资料.zip")) == "reading"
    assert (
        resource_task_type(Resource(id="r8", course_id="c", kind="other", title="疑似视频.mp4.txt")) == "reading"
    )


def test_pd2_video_task_type_id_criteria():
    result = build_tasks_json(
        {
            "resources": [
                res_row("res_pdf", "第1讲 讲义.pdf", kind="document"),
                res_row("res_mp4", "第1讲 课堂实录.mp4"),
            ]
        }
    )
    by_title = {t["title"]: t for t in result["tasks"]}
    video = by_title["第1讲 课堂实录.mp4"]
    assert video["type"] == "watching"
    assert video["id"] == "t:crs_v:watching:第1讲-课堂实录.mp4"
    assert video["completion_criteria"] == "观看视频资料并记录要点"  # 不写成“阅读完”
    assert video["estimated_minutes"] == 60  # watching 默认兜底

    pdf = by_title["第1讲 讲义.pdf"]
    assert pdf["type"] == "reading"
    assert pdf["id"] == "t:crs_v:reading:第1讲-讲义.pdf"

    # resource_task_id 显式带类型；缺省保持 reading（向后兼容）
    assert resource_task_id("crs_v", "第1讲 课堂实录.mp4", "watching") == video["id"]
    assert resource_task_id("crs_v", "第1讲 讲义.pdf") == pdf["id"]

    # 确定性：同输入重复生成逐字节一致
    again = build_tasks_json({"resources": [res_row("res_pdf", "第1讲 讲义.pdf", kind="document"), res_row("res_mp4", "第1讲 课堂实录.mp4")]})
    assert again == result


def test_pd2_watching_estimate_uses_distinct_config_and_factor():
    """watching 的时长规则与 reading 区分：独立默认值 + 独立校正系数。"""
    config = TaskGenConfig(
        default_minutes_by_type={"reading": 30, "watching": 45, "assignment": 90},
    )
    config.type_estimate_factors = {"watching": 2.0}
    result = build_tasks_json(
        {
            "resources": [
                res_row("res_mp4", "视频A.mp4"),
                res_row("res_other", "资料A.zip"),
            ],
            "config": config.to_dict(),
        }
    )
    by_title = {t["title"]: t for t in result["tasks"]}
    assert by_title["视频A.mp4"]["estimated_minutes"] == 90  # 45 × 2.0（watching 系数）
    assert by_title["资料A.zip"]["estimated_minutes"] == 30  # reading 默认值，不受 watching 系数影响


def test_pd2_watching_task_schedules_and_validates():
    """watching 任务与其它类型同规则参与排程（PD-2 注意项：影响估算与排程，须无回归）。"""
    watching = LearningTask(
        id="t_watch", course_id="crs_demo", type="watching", title="观看：第1讲", estimated_minutes=50
    )
    tasks = [watching, task("t_read", 60)]
    availability = make_availability()
    options = make_options()
    plan = generate_plan(tasks, availability, options)
    assert {b.task_id for b in plan.blocks} == {"t_watch", "t_read"}
    assert plan.unassigned == []
    assert validate_plan(plan, tasks, availability, options) == []


# ── PD-3：已提交作业保留、标注、不排程 ──────────────────────────────────────


def test_pd3_submitted_and_graded_flagged_others_not():
    result = build_tasks_json(
        {
            "assignments": [
                asg_row("a_sub", "已提交作业", "submitted", due_at="2026-10-12T15:59:00Z"),
                asg_row("a_grd", "已批改作业", "graded"),
                asg_row("a_no", "未提交作业", "not_submitted"),
                asg_row("a_unk", "状态未知作业", "unknown"),
            ]
        }
    )
    by_title = {t["title"]: t for t in result["tasks"]}
    assert by_title["已提交作业"]["platform_submitted"] is True
    assert by_title["已批改作业"]["platform_submitted"] is True  # graded 蕴含已提交
    assert by_title["未提交作业"]["platform_submitted"] is False
    assert by_title["状态未知作业"]["platform_submitted"] is False
    # 学习状态与平台事实均未被改写
    assert {t["status"] for t in result["tasks"]} == {"pending"}
    assert by_title["已提交作业"]["due_at"] == "2026-10-12T15:59:00Z"


def test_pd3_flag_refreshes_from_platform_input_not_stale_row():
    """platform_submitted 是平台派生事实：以当次输入为准，不被既有行/人工修正冻结。"""
    first = build_tasks_json({"assignments": [asg_row("a_sub", "作业X", "submitted")]})
    row = dict(first["tasks"][0])
    row["id"] = "tsk_001"  # B 落库行形状：不透明主键 + match_key
    row["match_key"] = first["tasks"][0]["id"]
    row["fact_source"] = "inferred"
    row["revision"] = 1
    row["archived_at"] = None
    row["created_at"] = "2026-10-10T08:00:00Z"
    row["updated_at"] = "2026-10-10T08:00:00Z"

    # 平台侧状态未变（submitted）+ 既有行 → 重建后依旧为 True
    second = build_tasks_json({"assignments": [asg_row("a_sub", "作业X", "submitted")], "existing_tasks": [row]})
    assert second["tasks"][0]["platform_submitted"] is True

    # 平台侧更正为 not_submitted → 以输入为准（False），即使既有行是 True
    third = build_tasks_json({"assignments": [asg_row("a_sub", "作业X", "not_submitted")], "existing_tasks": [row]})
    assert third["tasks"][0]["platform_submitted"] is False


def test_pd3_submitted_task_not_scheduled_not_unassigned():
    submitted = task(
        "t_hw", 90, type="assignment", platform_submitted=True, due_at="2026-10-16T15:59:00+08:00"
    )
    plain = task("t_plain", 60)
    tasks = [submitted, plain]
    availability = make_availability()
    options = make_options()
    plan = generate_plan(tasks, availability, options)
    assert {b.task_id for b in plan.blocks} == {"t_plain"}
    assert "t_hw" not in unassigned_reasons(plan)  # 既无时间块，也不进未安排清单
    assert validate_plan(plan, tasks, availability, options) == []

    # 修订路径同样排除：全新事项下不产生新块
    previous = make_plan([block("blk_old", "t_hw", "2026-10-12T19:00+08:00", "2026-10-12T20:30+08:00")])
    revision = revise_plan(previous, tasks, availability, options)
    assert {b.task_id for b in revision.plan.blocks} == {"t_plain"}
    assert revision.plan.unassigned == []
    assert revision.fully_valid


def test_pd3_submitted_prereq_does_not_block_successor():
    """已提交任务不排程，其“前提前置”视同已满足——后继不因它被永久阻塞。"""
    submitted = task("t_hw", 90, type="assignment", platform_submitted=True)
    follower = task("t_follow", 60, prerequisite_ids=["t_hw"])
    tasks = [submitted, follower]
    availability = make_availability()
    options = make_options()
    plan = generate_plan(tasks, availability, options)
    assert {b.task_id for b in plan.blocks} == {"t_follow"}
    assert plan.unassigned == []
    assert validate_plan(plan, tasks, availability, options) == []


def test_pd3_cancelled_wins_over_submitted_for_prereq():
    """取消除外：既取消又已提交的前置仍不可满足（与既有 cancelled 语义一致）。"""
    prereq = task("t_hw", 90, type="assignment", platform_submitted=True, status="cancelled")
    follower = task("t_follow", 60, prerequisite_ids=["t_hw"])
    plan = generate_plan([prereq, follower], make_availability(), make_options())
    assert plan.blocks == []
    assert unassigned_reasons(plan) == {"t_follow": ReasonCode.DEPENDENCY_CONFLICT}


def test_pd3_revise_releases_old_blocks_with_reason():
    """任务转为已提交后，修订释放其旧块，变化原因 = PLATFORM_SUBMITTED，且不进未安排。"""
    before = task("t_hw", 90, type="assignment")
    settled = task("t_settled", 60)
    availability = make_availability()
    options = make_options()
    previous = make_plan(
        [
            block("blk_hw", "t_hw", "2026-10-12T19:00+08:00", "2026-10-12T20:30+08:00"),
            block("blk_settled", "t_settled", "2026-10-15T19:00+08:00", "2026-10-15T20:00+08:00"),
        ]
    )

    now_submitted = task("t_hw", 90, type="assignment", platform_submitted=True)
    revision = revise_plan(previous, [now_submitted, settled], availability, options)
    assert {b.task_id for b in revision.plan.blocks} == {"t_settled"}
    assert revision.plan.unassigned == []
    removed = [
        e for e in revision.changes.entries
        if e.kind == ChangeKind.REMOVED and e.task_id == "t_hw"
    ]
    assert len(removed) == 1 and removed[0].reason == ChangeReason.PLATFORM_SUBMITTED
    assert removed[0].to_dict()["reason_text"] == "平台已提交，不参与排程"
    assert revision.fully_valid
    assert validate_plan(revision.plan, [now_submitted, settled], availability, options) == []


def test_pd3_validate_flags_new_block_for_submitted_task():
    """校验器把“为已提交任务产生新块”记为违规（锁定块不受限——用户显式钉住的安排保留）。"""
    submitted = task("t_hw", 90, type="assignment", platform_submitted=True)
    ok = task("t_ok", 60)
    availability = make_availability()

    bad_plan = make_plan(
        [
            block("blk_new", "t_hw", "2026-10-12T19:00+08:00", "2026-10-12T20:00+08:00"),
            block("blk_ok", "t_ok", "2026-10-12T20:00+08:00", "2026-10-12T21:00+08:00"),
        ]
    )
    codes = {v.code for v in validate_plan(bad_plan, [submitted, ok], availability, make_options())}
    assert "PLATFORM_SUBMITTED_TASK_SCHEDULED" in codes

    locked = block("blk_locked", "t_hw", "2026-10-12T19:00+08:00", "2026-10-12T20:00+08:00", locked=True)
    locked_plan = make_plan([locked])
    codes_locked = {v.code for v in validate_plan(locked_plan, [submitted], availability, make_options())}
    assert "PLATFORM_SUBMITTED_TASK_SCHEDULED" not in codes_locked

    # 引擎侧同一语义：锁定块原样保留（用户钉住的时间不被 PD-3 抹掉）
    engine_plan = generate_plan([submitted], availability, make_options(locked_blocks=[locked]))
    assert [b.id for b in engine_plan.blocks] == ["blk_locked"]
    assert validate_plan(engine_plan, [submitted], availability, make_options(locked_blocks=[locked])) == []
