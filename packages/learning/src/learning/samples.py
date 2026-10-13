"""基准样例与场景加载。

- `samples/baseline.json`：主基准样例（两门虚构课程、示例资料、五项作业、无字幕回放、一周空闲）。
- `samples/scenarios/*.json`：不可行与边界场景（零空闲、截止前窗口不足、不可拆分、依赖环等）。

样例既用于测试，也可作为 B/C 联调的输入契约（结构与 learning.models 的 to_dict/from_dict 对应）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import (
    Assignment,
    Availability,
    Course,
    LearningTask,
    Recording,
    Resource,
)
from .schedule.engine import ScheduleOptions, generate_plan
from .taskgen import TaskGenOptions, build_tasks
from .transcript import Transcript

SAMPLES_DIR = Path(__file__).resolve().parents[2] / "samples"


def list_samples() -> list[str]:
    names: list[str] = []
    if SAMPLES_DIR.is_dir():
        for path in sorted(SAMPLES_DIR.glob("*.json")):
            names.append(path.stem)
        scenario_dir = SAMPLES_DIR / "scenarios"
        if scenario_dir.is_dir():
            for path in sorted(scenario_dir.glob("*.json")):
                names.append(f"scenarios/{path.stem}")
    return names


def load_sample(name: str) -> dict[str, Any]:
    path = SAMPLES_DIR / f"{name}.json"
    if not path.is_file():
        raise FileNotFoundError(f"样例不存在：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def parse_inputs(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "courses": [Course.from_dict(item) for item in data.get("courses") or []],
        "resources": [Resource.from_dict(item) for item in data.get("resources") or []],
        "assignments": [Assignment.from_dict(item) for item in data.get("assignments") or []],
        "recordings": [Recording.from_dict(item) for item in data.get("recordings") or []],
        "tasks": [LearningTask.from_dict(item) for item in data.get("tasks") or []],
        "availability": Availability.from_dict(data.get("availability") or {}),
        "task_options": TaskGenOptions.from_dict(data.get("task_options")),
        "schedule_options": ScheduleOptions.from_dict(data.get("schedule_options") or {}),
        "expected": dict(data.get("expected") or {}),
    }


def run_sample(name: str) -> dict[str, Any]:
    """加载样例 → （需要时）生成任务 → 排程，返回完整上下文供测试/演示使用。"""
    data = load_sample(name)
    inputs = parse_inputs(data)
    tasks: list[LearningTask] = inputs["tasks"]
    taskgen_warnings = []
    if not tasks:
        result = build_tasks(
            inputs["resources"],
            inputs["assignments"],
            inputs["recordings"],
            inputs["task_options"],
        )
        tasks = result.tasks
        taskgen_warnings = result.warnings
    plan = generate_plan(tasks, inputs["availability"], inputs["schedule_options"])
    return {
        "sample": name,
        "inputs": inputs,
        "tasks": tasks,
        "taskgen_warnings": taskgen_warnings,
        "plan": plan,
        "expected": inputs["expected"],
    }


TRANSCRIPTS_DIR = SAMPLES_DIR / "transcript"


def list_transcripts() -> list[str]:
    if not TRANSCRIPTS_DIR.is_dir():
        return []
    return sorted(path.stem for path in TRANSCRIPTS_DIR.glob("*.json"))


def load_transcript(name: str) -> Transcript:
    """加载字幕样例（samples/transcript/*.json），按契约/A10 约定校验后返回。"""
    path = TRANSCRIPTS_DIR / f"{name}.json"
    if not path.is_file():
        raise FileNotFoundError(f"字幕样例不存在：{path}")
    return Transcript.from_dict(json.loads(path.read_text(encoding="utf-8")))
