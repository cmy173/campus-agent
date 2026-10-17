"""D3 学习任务生成（无模型规则路径）。

原则（对齐《04-内容处理与计划引擎》D3）：
- 输入资料、作业、回放与用户任务提示，输出候选学习任务；每项带预计分钟数、完成标准与来源。
- 任务 ID 稳定：重复生成不制造重复任务；同名新版本合并到同一任务并刷新来源引用。
- 保留用户修正：已有任务的耗时、完成状态、完成标准、拆分策略、优先级、前置关系不因重新解析被覆盖。
- 不做猜测：缺失截止时间保持 null；模型可建议的拆分/难度留待 D6，本模块只做确定性规则。
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from .models import (
    Assignment,
    LearningTask,
    Recording,
    Resource,
    SourceRef,
    WarningItem,
)
from .transcript import TranscriptStatus

# 契约资料类型（resource.schema.json kind 枚举：slides/handout/syllabus/scan/document/link/other）
# 缺省生成 reading 任务；未知 kind 同样按 reading 兜底（向前兼容平台新增类型）。
RESOURCE_TASK_TYPE = "reading"

# PD-2（coord v1.8 裁定，2026-10-10）：mp4/视频类资源由 reading 改为 watching（语义修正）。
# 识别依据：① mime_type 为 video/*；② 标题文件扩展名落入视频集合（真实样例 4 个 mp4 的
# mime_type 全为 null，扩展名是唯一可用证据）。不可下载者只产出观看任务，不伪造可解析内容。
VIDEO_RESOURCE_TASK_TYPE = "watching"
VIDEO_TITLE_EXTENSIONS = (
    ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".mpg", ".mpeg", ".ts", ".m2ts",
)
VIDEO_RESOURCE_COMPLETION_CRITERIA = "观看视频资料并记录要点"

# PD-3：平台提交状态中视为“已提交”的取值（graded 蕴含已提交）；其余（not_submitted/unknown）为否。
PLATFORM_SUBMITTED_STATUSES = ("submitted", "graded")

DEFAULT_COMPLETION_CRITERIA = {
    "reading": "通读资料并标记疑问点",
    "watching": "观看回放并记录要点",
    "assignment": "完成作业内容；学习完成不等于平台已提交",
    "practice": "完成练习题并核对答案",
    "review": "完成复习自测",
}


def slugify_title(title: str) -> str:
    """把标题规范化为稳定的 ID 片段（NFKC；空白、斜杠与冒号转 -，避免与 ID 分隔符混淆）。"""
    text = unicodedata.normalize("NFKC", title).strip()
    text = re.sub(r"[\s/\\|:]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")


def _build_task_id(course_id: str, task_type: str, key: str) -> str:
    """稳定匹配键：同一来源重复生成得到同一 ID；超长时截断并附内容摘要。

    说明：契约建议内部 ID 为不透明字符串（tsk_ 前缀）。本模块输出的是确定性“稳定匹配键”，
    B 持久化时可映射为不透明 ID；字段语义详见交接说明。
    """
    full = f"t:{course_id}:{task_type}:{key}"
    if len(full) > 120:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
        prefix = f"t:{course_id}:{task_type}:"
        keep = max(8, 120 - len(prefix) - len(digest) - 1)
        full = f"{prefix}{key[:keep]}-{digest}"
    return full


def resource_task_id(course_id: str, title: str, task_type: str = RESOURCE_TASK_TYPE) -> str:
    """资料任务稳定 ID；task_type 与任务类型保持一致（PD-2 后视频类为 watching）。

    默认 reading 仅为向后兼容的兜底；taskgen 内部一律传入实际映射所得的任务类型。
    """
    return _build_task_id(course_id, task_type, slugify_title(title))


def _looks_like_video(resource: Resource) -> bool:
    """视频证据：MIME 为 video/*，或标题扩展名落入视频集合（真实样例 mime 为 null）。"""
    mime = (resource.mime_type or "").strip().lower()
    if mime.startswith("video/"):
        return True
    title = resource.title.strip().lower()
    return any(title.endswith(ext) for ext in VIDEO_TITLE_EXTENSIONS)


def resource_task_type(resource: Resource) -> str:
    """资料 → 任务类型映射（PD-2）：视频类 watching，其余 reading。"""
    return VIDEO_RESOURCE_TASK_TYPE if _looks_like_video(resource) else RESOURCE_TASK_TYPE


def _external_key(external_id: str | None, title: str) -> str:
    return external_id if external_id else slugify_title(title)


def assignment_task_id(course_id: str, assignment: Assignment) -> str:
    key = _external_key(assignment.source_ref.external_id if assignment.source_ref else None, assignment.title)
    return _build_task_id(course_id, "assignment", key)


def recording_task_id(course_id: str, recording: Recording) -> str:
    key = _external_key(recording.source_ref.external_id if recording.source_ref else None, recording.title)
    return _build_task_id(course_id, "watching", key)


def _merge_refs(refs: list[SourceRef]) -> list[SourceRef]:
    unique: dict[tuple[str, str], SourceRef] = {}
    for ref in refs:
        unique.setdefault(ref.key(), ref)
    return [unique[k] for k in sorted(unique)]


@dataclass
class TaskGenConfig:
    minutes_per_unit: dict[str, int] = field(
        default_factory=lambda: {"page": 3, "slide": 2}
    )
    watch_minutes_factor: float = 1.0
    default_minutes_by_type: dict[str, int] = field(
        default_factory=lambda: {
            "reading": 60,
            "watching": 60,
            "assignment": 90,
            "practice": 60,
            "review": 45,
        }
    )
    # D6 估计校正系数（由 estimate.calibrate_estimates 产出，B 持久化后回传）；
    # 缺省为空 = 系数 1.0。仅作用于规则推断的耗时，不改写用户自设耗时。
    type_estimate_factors: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "minutes_per_unit": dict(self.minutes_per_unit),
            "watch_minutes_factor": self.watch_minutes_factor,
            "default_minutes_by_type": dict(self.default_minutes_by_type),
            "type_estimate_factors": dict(self.type_estimate_factors),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "TaskGenConfig":
        data = data or {}
        cfg = cls()
        if data.get("minutes_per_unit"):
            cfg.minutes_per_unit = {str(k): int(v) for k, v in data["minutes_per_unit"].items()}
        if data.get("watch_minutes_factor") is not None:
            cfg.watch_minutes_factor = float(data["watch_minutes_factor"])
        if data.get("default_minutes_by_type"):
            cfg.default_minutes_by_type = {str(k): int(v) for k, v in data["default_minutes_by_type"].items()}
        if data.get("type_estimate_factors"):
            cfg.type_estimate_factors = {str(k): float(v) for k, v in data["type_estimate_factors"].items()}
        return cfg


def scaled_minutes(minutes: int, task_type: str, config: TaskGenConfig) -> int:
    """按校正系数缩放推断耗时（就近取整、至少 1 分钟）；系数无效时不缩放。"""
    factor = config.type_estimate_factors.get(task_type)
    if not isinstance(factor, (int, float)) or isinstance(factor, bool) or not (0 < float(factor)):
        return minutes
    return max(1, int(minutes * float(factor) + 0.5))


@dataclass
class TaskHint:
    """用户修正提示：按 match 定位已生成任务，覆盖耗时/优先级/拆分策略，或补充前置关系。

    match / prerequisites 的引用形式：{course_id, type, external_id 或 title}。
    """

    match: dict[str, str] = field(default_factory=dict)
    estimate_minutes: int | None = None
    priority: int | None = None
    splittable: bool | None = None
    prerequisites: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "match": dict(self.match),
            "estimate_minutes": self.estimate_minutes,
            "priority": self.priority,
            "splittable": self.splittable,
            "prerequisites": [dict(p) for p in self.prerequisites],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskHint":
        return cls(
            match=dict(data.get("match") or {}),
            estimate_minutes=data.get("estimate_minutes"),
            priority=data.get("priority"),
            splittable=data.get("splittable"),
            prerequisites=[dict(p) for p in (data.get("prerequisites") or [])],
        )


@dataclass
class TaskGenOptions:
    existing_tasks: list[LearningTask] = field(default_factory=list)
    hints: list[TaskHint] = field(default_factory=list)
    user_tasks: list[LearningTask] = field(default_factory=list)
    config: TaskGenConfig = field(default_factory=TaskGenConfig)

    def to_dict(self) -> dict[str, Any]:
        return {
            "existing_tasks": [t.to_dict() for t in self.existing_tasks],
            "hints": [h.to_dict() for h in self.hints],
            "user_tasks": [t.to_dict() for t in self.user_tasks],
            "config": self.config.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "TaskGenOptions":
        data = data or {}
        return cls(
            existing_tasks=[LearningTask.from_dict(t) for t in (data.get("existing_tasks") or [])],
            hints=[TaskHint.from_dict(h) for h in (data.get("hints") or [])],
            user_tasks=[LearningTask.from_dict(t) for t in (data.get("user_tasks") or [])],
            config=TaskGenConfig.from_dict(data.get("config")),
        )


@dataclass
class TaskGenResult:
    tasks: list[LearningTask]
    warnings: list[WarningItem]

    def to_dict(self) -> dict[str, Any]:
        return {
            "tasks": [t.to_dict() for t in self.tasks],
            "warnings": [w.to_dict() for w in self.warnings],
        }


def _task_id_for_ref(ref: dict[str, str]) -> str | None:
    course_id = ref.get("course_id")
    task_type = ref.get("type")
    if not course_id or not task_type:
        return None
    key = ref.get("external_id") or (slugify_title(ref["title"]) if ref.get("title") else None)
    if not key:
        return None
    return _build_task_id(course_id, task_type, key)


def _estimate_resource_minutes(
    resource: Resource, config: TaskGenConfig, task_type: str = RESOURCE_TASK_TYPE
) -> tuple[int, str, str | None]:
    unit = resource.extent_unit
    value = resource.extent_value
    if value and unit in config.minutes_per_unit:
        return max(1, int(value) * int(config.minutes_per_unit[unit])), "inferred", None
    note = None
    if unit and unit not in config.minutes_per_unit:
        note = f"无法识别的篇幅单位 {unit!r}，按类型默认耗时估算"
    return int(config.default_minutes_by_type.get(task_type, 60)), "inferred", note


def build_tasks(
    resources: list[Resource],
    assignments: list[Assignment],
    recordings: list[Recording],
    options: TaskGenOptions | None = None,
) -> TaskGenResult:
    """资料、作业、回放、用户任务与提示 → 候选学习任务（确定性，无模型调用）。"""
    options = options or TaskGenOptions()
    config = options.config
    warnings: list[WarningItem] = []
    tasks: dict[str, LearningTask] = {}

    # 1) 资料：按 (课程, 类型, 标题) 归并为资源族（版本合并），族间再按内容哈希合并重复文件。
    #    族排序优先“版本更高、文件更多”的族，保证同名新版本/重复副本合并到信息最完整的族。
    family_members: dict[tuple[str, str, str], list[Resource]] = {}
    for resource in sorted(
        resources, key=lambda r: (r.course_id, r.kind, slugify_title(r.title), r.version, r.id)
    ):
        if resource.mime_type == "application/zip":
            continue  # 容器本身不生成学习任务，解包后的文档独立生成。
        family_members.setdefault(
            (resource.course_id, resource.kind, slugify_title(resource.title)), []
        ).append(resource)

    families_sorted = sorted(
        family_members.items(),
        key=lambda kv: (kv[0][0], kv[0][1], -max(m.version for m in kv[1]), -len(kv[1]), kv[0][2]),
    )
    seen_hash: dict[tuple[str, str], Resource] = {}
    kept_by_key: dict[tuple[str, str, str], list[Resource]] = {}
    kept_order: list[tuple[str, str, str]] = []
    for key, members in families_sorted:
        overlap: tuple[Resource, Resource] | None = None
        for member in sorted(members, key=lambda r: (r.version, r.id)):
            if member.content_hash and (key[0], member.content_hash) in seen_hash:
                overlap = (member, seen_hash[(key[0], member.content_hash)])
                break
        if overlap is not None:
            skipped, previous = overlap
            target_key = (previous.course_id, previous.kind, slugify_title(previous.title))
            kept_by_key[target_key].extend(members)
            warnings.append(
                WarningItem(
                    code="DUPLICATE_CONTENT",
                    message=(
                        f"资料《{skipped.title}》（{skipped.id}）与《{previous.title}》"
                        f"（{previous.id}）内容相同，已合并到同一任务，不重复生成"
                    ),
                    details={
                        "kept_resource_id": previous.id,
                        "skipped_resource_id": skipped.id,
                    },
                )
            )
            continue
        for member in members:
            if member.content_hash:
                seen_hash.setdefault((key[0], member.content_hash), member)
        kept_by_key[key] = members
        kept_order.append(key)

    for course_id, kind, slug in kept_order:
        members_sorted = sorted(kept_by_key[(course_id, kind, slug)], key=lambda r: (r.version, r.id))
        current = members_sorted[-1]
        versions = sorted({m.version for m in members_sorted})
        if len(versions) > 1:
            warnings.append(
                WarningItem(
                    code="TASK_VERSION_UPDATED",
                    message=(
                        f"《{current.title}》存在 {len(versions)} 个版本（{len(members_sorted)} 个文件），"
                        f"任务按最新版本 v{current.version} 估算并保留全部来源引用"
                    ),
                    details={"course_id": course_id, "versions": versions},
                )
            )
        task_type = resource_task_type(current)
        minutes, provenance, note = _estimate_resource_minutes(current, config, task_type)
        minutes = scaled_minutes(minutes, task_type, config)
        if note:
            warnings.append(
                WarningItem(
                    code="UNSUPPORTED_EXTENT",
                    message=f"《{current.title}》：{note}",
                    details={"resource_id": current.id},
                )
            )
        criteria = (
            VIDEO_RESOURCE_COMPLETION_CRITERIA
            if task_type == VIDEO_RESOURCE_TASK_TYPE
            else DEFAULT_COMPLETION_CRITERIA["reading"]
        )
        tasks[resource_task_id(course_id, current.title, task_type)] = LearningTask(
            id=resource_task_id(course_id, current.title, task_type),
            course_id=course_id,
            type=task_type,
            title=current.title,
            estimated_minutes=minutes,
            due_at=None,
            source_refs=_merge_refs([ref for m in members_sorted for ref in m.source_refs]),
            completion_criteria=criteria,
            estimate_provenance=provenance,
        )

    # 2) 作业：与 PD-3 一致——平台已提交（submitted/graded）仍生成任务，但标注
    #    platform_submitted=True（排程排除由 engine/revise 承担）；due_at 等平台事实原样透传。
    for assignment in sorted(assignments, key=lambda a: a.id):
        task_id = assignment_task_id(assignment.course_id, assignment)
        if assignment.estimated_minutes and assignment.estimated_minutes > 0:
            minutes, provenance = int(assignment.estimated_minutes), "user"
        else:
            minutes = scaled_minutes(int(config.default_minutes_by_type.get("assignment", 90)), "assignment", config)
            provenance = "inferred"
        refs = [assignment.source_ref] if assignment.source_ref else []
        tasks[task_id] = LearningTask(
            id=task_id,
            course_id=assignment.course_id,
            type="assignment",
            title=assignment.title,
            estimated_minutes=minutes,
            due_at=assignment.due_at,
            due_precision=assignment.due_precision,
            source_refs=_merge_refs(refs),
            completion_criteria=DEFAULT_COMPLETION_CRITERIA["assignment"],
            estimate_provenance=provenance,
            platform_submitted=assignment.submission_status in PLATFORM_SUBMITTED_STATUSES,
        )

    # 3) 回放：仅按 ready 字幕做内容解析（A10 §2 消费分支）；其余状态只按时长创建观看任务，
    #    完成标准按六态分别备注（等待更新 / 无字幕 / 暂不可得 / 状态未知）
    for recording in sorted(recordings, key=lambda r: r.id):
        task_id = recording_task_id(recording.course_id, recording)
        transcript_ready = TranscriptStatus.is_ready(recording.transcript_status)
        if recording.duration_seconds and recording.duration_seconds > 0:
            minutes = max(1, math.ceil(recording.duration_seconds / 60 * config.watch_minutes_factor))
            provenance = "inferred"
        else:
            minutes = int(config.default_minutes_by_type.get("watching", 60))
            provenance = "inferred"
        minutes = scaled_minutes(minutes, "watching", config)
        if not recording.duration_seconds or recording.duration_seconds <= 0:
            warnings.append(
                WarningItem(
                    code="MISSING_DURATION",
                    message=f"回放《{recording.title}》缺少时长，按默认 {minutes} 分钟估算",
                    details={"recording_id": recording.id},
                )
            )
        criteria = DEFAULT_COMPLETION_CRITERIA["watching"]
        if not transcript_ready:
            criteria += TranscriptStatus.non_ready_note(recording.transcript_status)
        refs = [recording.source_ref] if recording.source_ref else []
        tasks[task_id] = LearningTask(
            id=task_id,
            course_id=recording.course_id,
            type="watching",
            title=recording.title,
            estimated_minutes=minutes,
            due_at=None,
            source_refs=_merge_refs(refs),
            completion_criteria=criteria,
            estimate_provenance=provenance,
        )

    # 4) 用户自建任务
    for task in sorted(options.user_tasks, key=lambda t: t.id):
        if task.id in tasks:
            warnings.append(
                WarningItem(
                    code="DUPLICATE_TASK_ID",
                    message=f"用户任务 {task.id} 与生成任务 ID 冲突，已保留先出现者",
                    task_ids=[task.id],
                )
            )
            continue
        tasks[task.id] = task

    # 5) 保留已有任务的用户修正字段（重新解析不覆盖人工设定）
    for previous in options.existing_tasks:
        current = tasks.get(previous.id)
        if current is None:
            tasks[previous.id] = previous
            warnings.append(
                WarningItem(
                    code="EXISTING_TASK_UNMATCHED",
                    message=f"已有任务 {previous.id} 在本次输入中无对应来源，已原样保留（可能为用户自建或来源已移除）",
                    task_ids=[previous.id],
                )
            )
            continue
        current.status = previous.status
        if previous.estimate_provenance == "user" and previous.estimated_minutes and previous.estimated_minutes > 0:
            current.estimated_minutes = previous.estimated_minutes
            current.estimate_provenance = previous.estimate_provenance
        if previous.completion_criteria:
            current.completion_criteria = previous.completion_criteria
        if previous.splittable is not None:
            current.splittable = previous.splittable
        if previous.priority is not None:
            current.priority = previous.priority
        if previous.prerequisite_ids:
            current.prerequisite_ids = list(previous.prerequisite_ids)

    # 6) 应用用户提示（可补充前置关系、调整耗时/优先级/拆分策略）
    for hint in options.hints:
        target_id = _task_id_for_ref(hint.match)
        target = tasks.get(target_id) if target_id else None
        if target is None:
            warnings.append(
                WarningItem(
                    code="HINT_UNRESOLVED",
                    message=f"任务提示未能匹配到任务：{hint.match}",
                    details={"match": dict(hint.match)},
                )
            )
            continue
        if hint.estimate_minutes and hint.estimate_minutes > 0:
            target.estimated_minutes = int(hint.estimate_minutes)
            target.estimate_provenance = "user"
        if hint.priority is not None:
            target.priority = int(hint.priority)
        if hint.splittable is not None:
            target.splittable = bool(hint.splittable)
        if hint.prerequisites:
            resolved: list[str] = []
            for ref in hint.prerequisites:
                ref_id = _task_id_for_ref(ref)
                if ref_id and ref_id in tasks:
                    resolved.append(ref_id)
                else:
                    warnings.append(
                        WarningItem(
                            code="HINT_UNRESOLVED",
                            message=f"前置依赖未能匹配到任务：{ref}（提示目标 {target.id}）",
                            task_ids=[target.id],
                            details={"ref": dict(ref)},
                        )
                    )
            if resolved:
                target.prerequisite_ids = sorted(set(resolved))

    ordered = [tasks[tid] for tid in sorted(tasks)]
    return TaskGenResult(tasks=ordered, warnings=warnings)


def build_tasks_json(payload: dict[str, Any]) -> dict[str, Any]:
    """B13 集成入口（D12）：契约形状 JSON → 候选任务与告警（全部 JSON 可序列化）。

    输入 payload（除注明外均为契约形状 dict，缺省键视为空；B 落库行的 ``to_contract()``
    输出可直接传入，多余键忽略）：
    - ``resources`` / ``assignments`` / ``recordings``：候选任务的三类来源；
    - ``existing_tasks``：该课程全部既有任务行（原样传入即可）——
      未归档且有稳定键（``match_key``，或 ``t:`` 前缀的 ``id``）→ 与再生成结果按稳定键
      对齐并保留人工修正（完成状态/耗时/完成标准/拆分策略/优先级/前置）；
      未归档且无稳定键 → 人工自建任务，原样保留（不产生“未匹配”告警）；
      已归档（``archived_at`` 非 null）→ 忽略（软删除不复活；B13 按 ``match_key``
      命中已归档行时应跳过，不要重新插入）；
    - ``user_tasks`` / ``hints`` / ``config``：可选，语义同 ``TaskGenOptions``。

    输出 ``{"tasks": [...], "warnings": [...]}``：``tasks[].id`` 即稳定匹配键（B 存入
    任务的 ``match_key`` 列；插入时另配不透明主键）；``warnings[].code`` 为稳定码，
    ``message`` 为面向用户的中文文案（供 C 直接展示）。
    本函数为纯函数：不访问数据库、网络或平台。
    """
    data = payload or {}
    user_entries: list[dict[str, Any]] = list(data.get("user_tasks") or [])
    existing: list[LearningTask] = []
    for entry in data.get("existing_tasks") or []:
        row = dict(entry)
        if row.get("archived_at"):
            continue
        key = row.get("match_key")
        if not key and isinstance(row.get("id"), str) and row["id"].startswith("t:"):
            key = row["id"]  # 兼容直接回传 build_tasks 输出（id 即稳定键）
        if key:
            row["id"] = key
            existing.append(LearningTask.from_dict(row))
        else:
            user_entries.append(row)
    result = build_tasks(
        [Resource.from_dict(item) for item in (data.get("resources") or [])],
        [Assignment.from_dict(item) for item in (data.get("assignments") or [])],
        [Recording.from_dict(item) for item in (data.get("recordings") or [])],
        TaskGenOptions(
            existing_tasks=existing,
            hints=[TaskHint.from_dict(h) for h in (data.get("hints") or [])],
            user_tasks=[LearningTask.from_dict(t) for t in user_entries],
            config=TaskGenConfig.from_dict(data.get("config")),
        ),
    )
    return result.to_dict()
