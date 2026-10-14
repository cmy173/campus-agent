"""D15 一次性执行工具：A21 真实快照（B 的 X1 导出）→ 任务生成 + 排程 + 观察报告（X2 收尾证据工具）。

做什么：
    - 读取 B 的 X1 导出目录（默认 .worktrees/agent-b/apps/api/var/b17-a21-export）：
      course.json / resources.json / assignments.json / recordings.json / manifest.json。
      该导出由 apps/api/tools/b17_replay.py 从 A21 真实同步快照重建（回放库 → API → 本地导出，
      含真实 ID，本地 gitignored，不入提交物）；
    - 导出完整性：逐文件 sha256 与 manifest 核对、条目数与 manifest.counts 核对；
    - A21 归一化指纹独立复算（envelope 递归剔除 cursor/observed_at → json sort_keys →
      sha256 前 16 位；同 A21 §3 / B17 工具口径），与 manifest 记录及预期 323afc775e093580 核对；
    - 契约边界换算镜像 B 的 apps/api/app/plan_engine.py：weekday ISO 1-7 → 引擎 0-6（减 1）、
      日期例外 windows=[] → unavailable、horizon 含尾日 → 引擎半开区间（end+1 天 00:00）；
    - build_tasks_json（config 读自 contracts/config-defaults.json 的 taskgen 段，并与包内
      默认值断言一致 = 生产同构）→ 观察 extent 可用性 / 兜底比例 / warnings / platform_submitted；
    - generate_plan + validate_plan。可用性输入默认 contracts/examples/api/availability.json
      （契约样例；该回放库无 availability 行，故为【模拟输入】，报告如实标注）；
    - 冗余独立台账：无重叠 / 窗口内 / 每日上限 / horizon 内（不依赖 validate_plan 实现）；
    - PD-3 观察：平台已提交任务不进入 blocks/unassigned；反事实（翻转 platform_submitted
      后重排）给出对照；
    - 输出本地 JSON 报告（默认导出目录下 d15-observations.json，含真实标识，不入提交物）
      + UTF-8 控制台摘要。

边界：
    - 脚本源码与提交物不含任何真实课号/资源 ID（全部运行期从导出读取）；
    - 纯本地计算：不触网、不访问数据库、不调用 B 服务；
    - 退出码：核对全部通过 0，否则 1（报告仍会写出）。
    本工具为一次性证据工具（非产品代码），随 X2 交接文档一并提交。

运行：
    cd packages/learning && .venv/Scripts/python tools/d15_run.py
    # 可选：--export-dir / --evidence-dir / --today YYYY-MM-DD / --horizon-days 14 / --out PATH
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import io
import json
import os
import sys
from collections import Counter
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
PKG_DIR = TOOL_DIR.parent  # packages/learning
WORKTREE_ROOT = PKG_DIR.parent.parent  # 本 worktree（.worktrees/agent-d）根
sys.path.insert(0, str(PKG_DIR / "src"))

from learning.models import (  # noqa: E402
    Assignment,
    Availability,
    DateException,
    LearningTask,
    WeeklyWindow,
    WindowSpan,
)
from learning.schedule.engine import (  # noqa: E402
    ScheduleConfig,
    ScheduleOptions,
    generate_plan,
)
from learning.schedule.validate import validate_plan  # noqa: E402
from learning.taskgen import TaskGenConfig, build_tasks_json  # noqa: E402
from learning.timeutil import (  # noqa: E402
    expand_availability,
    parse_dt,
    resolve_tz,
    split_by_date,
)

DEFAULT_EXPORT_DIR = WORKTREE_ROOT.parent / "agent-b" / "apps" / "api" / "var" / "b17-a21-export"
DEFAULT_EVIDENCE_DIR = (
    Path(os.environ.get("LOCALAPPDATA") or "") / "zjulearn" / "controlled-browser" / "a20-results"
)
FINGERPRINT_EXPECTED = "323afc775e093580"  # A21 §3 双次同步归一化指纹（本工具独立复算交叉验证）

CHECKS: list[dict] = []
FAILED: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> bool:
    CHECKS.append({"name": name, "ok": bool(ok), "detail": str(detail)})
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  | {detail}" if detail != "" else ""))
    if not ok:
        FAILED.append(name)
    return bool(ok)


def strip_volatile(value: object) -> object:
    """A21 §3 归一化口径：递归剔除 cursor 与 observed_at（含条目嵌套）。"""
    if isinstance(value, dict):
        return {k: strip_volatile(v) for k, v in value.items() if k not in ("cursor", "observed_at")}
    if isinstance(value, list):
        return [strip_volatile(v) for v in value]
    return value


def fingerprint(envelope: dict) -> str:
    data = json.dumps(strip_volatile(envelope), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def recompute_fingerprints(evidence_dir: Path) -> dict:
    """从 A21 证据文件独立复算归一化指纹（作用于 doc['envelope']，同 B17 口径）。"""
    out: dict = {"performed": False, "note": ""}
    if not evidence_dir or not evidence_dir.is_dir():
        out["note"] = f"证据目录不可用：{evidence_dir}（跳过独立复算，指纹以 manifest 为准）"
        return out
    matches1 = sorted(evidence_dir.glob("a21-sync1-*.json"))
    matches2 = sorted(evidence_dir.glob("a21-sync2-*.json"))
    if not matches1 or not matches2:
        out["note"] = f"未找到 a21-sync1/sync2 证据文件于 {evidence_dir}（跳过独立复算）"
        return out
    out["performed"] = True
    for tag, matches in (("sync1", matches1), ("sync2", matches2)):
        doc = load_json(matches[-1])
        env = doc.get("envelope")
        out[tag] = fingerprint(env) if isinstance(env, dict) else None
        out[f"{tag}_file"] = matches[-1].name
    return out


def map_availability_contract_to_engine(raw: dict) -> Availability:
    """镜像 apps/api/app/plan_engine.py `_map_availability` 的边界换算（契约形状 → 引擎形状）。"""
    weekly = [
        WeeklyWindow(weekday=int(w["weekday"]) - 1, start=w["start"], end=w["end"])  # ISO 1-7 → 0-6
        for w in (raw.get("weekly_windows") or [])
    ]
    exceptions: list[DateException] = []
    for e in raw.get("date_exceptions") or []:
        windows = e.get("windows")
        if windows == []:
            exceptions.append(DateException(date=e["date"], unavailable=True))  # 空数组=当日不可用
        elif windows:
            exceptions.append(
                DateException(
                    date=e["date"],
                    windows=[WindowSpan(start=w["start"], end=w["end"]) for w in windows],
                )
            )
        else:
            exceptions.append(DateException(date=e["date"]))
    return Availability(
        timezone=raw.get("timezone", "Asia/Shanghai"),
        weekly_windows=weekly,
        date_exceptions=exceptions,
        daily_limit_minutes=raw.get("daily_limit_minutes"),
    )


def independent_ledger(plan, availability: Availability) -> dict:
    """冗余台账（不依赖 validate_plan）：无重叠 / 窗口内 / 每上限 / horizon 内。"""
    tz = resolve_tz(availability.timezone)
    hs = parse_dt(plan.horizon_start)
    he = parse_dt(plan.horizon_end)
    day_map = {d.date: d for d in expand_availability(availability, hs, he)}

    timed = [(b.id, parse_dt(b.starts_at), parse_dt(b.ends_at)) for b in plan.blocks]

    overlaps: list[dict] = []
    ordered = sorted(timed, key=lambda x: (x[1], x[0]))
    sweep_block, sweep_end = None, None
    for bid, s, e in ordered:
        if sweep_end is not None and s < sweep_end:
            overlaps.append({"a": sweep_block, "b": bid})
        if sweep_end is None or e > sweep_end:
            sweep_end, sweep_block = e, bid

    outside_window: list[str] = []
    out_of_horizon: list[str] = []
    for bid, s, e in timed:
        if not (hs <= s and e <= he):
            out_of_horizon.append(bid)
        day = day_map.get(s.astimezone(tz).date())
        inside = day is not None and any(g0 <= s and e <= g1 for g0, g1 in day.gaps)
        if not inside:
            outside_window.append(bid)

    used: dict[dt.date, int] = {}
    for _bid, s, e in timed:
        for day, minutes in split_by_date(s, e, tz):
            used[day] = used.get(day, 0) + minutes
    over_limit = [
        {"date": d.isoformat(), "used": m, "limit": day_map[d].daily_limit_minutes}
        for d, m in sorted(used.items())
        if day_map.get(d) is not None
        and day_map[d].daily_limit_minutes is not None
        and m > day_map[d].daily_limit_minutes
    ]

    return {
        "overlaps": overlaps,
        "outside_window": outside_window,
        "out_of_horizon": out_of_horizon,
        "over_daily_limit": over_limit,
        "daily_usage": {d.isoformat(): m for d, m in sorted(used.items())},
        "pass": not (overlaps or outside_window or out_of_horizon or over_limit),
    }


def task_observation(task: LearningTask, res_by_ext: dict, asg_by_ext: dict, defaults: dict) -> dict:
    ref = task.source_refs[0] if task.source_refs else None
    ext = ref.external_id if ref else None
    obs: dict = {
        "label": task.title,
        "id": task.id,
        "title": task.title,
        "type": task.type,
        "estimated_minutes": task.estimated_minutes,
        "estimate_provenance": task.estimate_provenance,
        "due_at": task.due_at,
        "due_precision": task.due_precision,
        "platform_submitted": task.platform_submitted,
        "source": {"kind": None, "external_id": ext},
        "fallback": {"used": False, "reason": None},
    }
    res = res_by_ext.get(ext)
    if task.type != "assignment" and res is not None:
        obs["source"].update(
            {
                "kind": "resource",
                "title": res.get("title"),
                "kind_field": res.get("kind"),
                "mime_type": res.get("mime_type"),
                "parse_status": res.get("parse_status"),
                "fetched_at": res.get("fetched_at"),
                "extent_value": res.get("extent_value"),
                "extent_unit": res.get("extent_unit"),
                "size_bytes": res.get("size_bytes"),
            }
        )
        if res.get("extent_value") is None:
            obs["fallback"] = {
                "used": True,
                "reason": (
                    "资源 extent 缺失（fetched_at=null、parse_status=pending）→ "
                    f"default_minutes_by_type[{task.type}]="
                    f"{defaults.get(task.type, 60)}（静默兜底，不产生告警）"
                ),
            }
        else:
            obs["fallback"] = {
                "used": False,
                "reason": f"extent 可用：{res.get('extent_value')} {res.get('extent_unit')} × 单位分钟",
            }
    else:
        asg = asg_by_ext.get(ext)
        obs["source"].update(
            {
                "kind": "assignment",
                "title": asg.get("title") if asg else None,
                "submission_status": asg.get("submission_status") if asg else None,
            }
        )
        est = Assignment.from_dict(asg).estimated_minutes if asg else None
        obs["fallback"] = {
            "used": True,
            "reason": (
                "作业无平台时长数据（导出 estimated_minutes="
                f"{est!r}）→ default_minutes_by_type[assignment]="
                f"{defaults.get('assignment', 90)}"
            ),
        }
    return obs


def summarize_plan(plan) -> dict:
    return {
        "blocks": [
            {"id": b.id, "task_id": b.task_id, "starts_at": b.starts_at, "ends_at": b.ends_at}
            for b in plan.blocks
        ],
        "unassigned": [u.to_dict() for u in plan.unassigned],
        "warnings": [w.to_dict() for w in plan.warnings],
        "summary": dict(plan.summary),
    }


def main(argv: list[str] | None = None) -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="D15 真实快照任务生成与排程观察（X2）")
    parser.add_argument("--export-dir", default=str(DEFAULT_EXPORT_DIR), help="B 的 X1 导出目录")
    parser.add_argument("--evidence-dir", default=str(DEFAULT_EVIDENCE_DIR), help="A21 证据目录（指纹复算）")
    parser.add_argument("--today", default=None, help="计划起点（YYYY-MM-DD），默认系统当天")
    parser.add_argument("--horizon-days", type=int, default=14, help="计划天数（含首尾，默认 14）")
    parser.add_argument("--availability", default=str(WORKTREE_ROOT / "contracts" / "examples" / "api" / "availability.json"),
                        help="可用性输入（契约形状 JSON；默认契约样例=模拟输入）")
    parser.add_argument("--out", default=None, help="报告输出路径（默认导出目录/d15-observations.json）")
    args = parser.parse_args(argv)

    export_dir = Path(args.export_dir)
    if not export_dir.is_dir():
        print(f"导出目录不存在：{export_dir}\n请先运行 B 的 X1 工具重建（apps/api/tools/b17_replay.py）。")
        return 1
    out_path = Path(args.out) if args.out else export_dir / "d15-observations.json"

    report: dict = {
        "tool": "d15_run",
        "purpose": "X2/D15 真实数据任务生成与排程观察（本地，含真实标识，不入提交物）",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "export_dir": str(export_dir),
    }
    print("=== D15 真实快照任务生成与排程观察（X2） ===")

    # 0) 导出完整性
    print("\n-- 0) 导出完整性 --")
    manifest = load_json(export_dir / "manifest.json")
    report["manifest"] = {"generated_at": manifest.get("generated_at"), "course": manifest.get("course"),
                          "counts": manifest.get("counts")}
    for name, meta in (manifest.get("files") or {}).items():
        actual = sha256_file(export_dir / name)
        check(f"导出 sha256：{name}", actual == meta.get("sha256"), actual[:16])
    resources_doc = load_json(export_dir / "resources.json")
    assignments_doc = load_json(export_dir / "assignments.json")
    recordings_doc = load_json(export_dir / "recordings.json")
    resources = resources_doc["items"]
    assignments = assignments_doc["items"]
    recordings = recordings_doc["items"]
    counts = {"resources": len(resources), "assignments": len(assignments), "recordings": len(recordings)}
    check("导出条目数与 manifest.counts 一致", counts == manifest.get("counts"), f"{counts}")

    # 1) A21 指纹独立复算
    print("\n-- 1) A21 归一化指纹独立复算 --")
    fp = recompute_fingerprints(Path(args.evidence_dir))
    report["fingerprint_recompute"] = fp
    manifest_fp = (manifest.get("evidence") or {}).get("fingerprint")
    if fp["performed"]:
        check("复算 sync1 = 预期 323afc775e093580", fp["sync1"] == FINGERPRINT_EXPECTED, fp["sync1"])
        check("复算 sync2 = 预期 323afc775e093580", fp["sync2"] == FINGERPRINT_EXPECTED, fp["sync2"])
        check("复算与 manifest 记录一致", manifest_fp == fp["sync1"] == fp["sync2"],
              f"manifest={manifest_fp}")
    else:
        print(f"WARN  {fp['note']}（manifest 记录 fingerprint={manifest_fp}）")

    # 2) 契约配置（与包内默认值同构断言）
    print("\n-- 2) 契约配置装载 --")
    config_defaults = load_json(WORKTREE_ROOT / "contracts" / "config-defaults.json")
    tg_cfg_raw = config_defaults.get("taskgen") or {}
    sc_cfg_raw = config_defaults.get("schedule") or {}
    tg_parity = TaskGenConfig.from_dict(tg_cfg_raw).to_dict() == TaskGenConfig().to_dict()
    sc_parity = ScheduleConfig.from_dict(sc_cfg_raw).to_dict() == ScheduleConfig().to_dict()
    report["config"] = {
        "source": "contracts/config-defaults.json",
        "taskgen": tg_cfg_raw,
        "taskgen_parity_with_package_defaults": tg_parity,
        "schedule": sc_cfg_raw,
        "schedule_parity_with_package_defaults": sc_parity,
    }
    check("taskgen 契约配置与包内默认值同构", tg_parity)
    check("schedule 契约配置与包内默认值同构", sc_parity)

    # 3) build_tasks_json → 观察
    print("\n-- 3) build_tasks_json（真实快照） --")
    payload = {
        "resources": resources,
        "assignments": assignments,
        "recordings": recordings,
        "config": tg_cfg_raw,
    }
    gen = build_tasks_json(payload)
    tasks_raw = gen["tasks"]
    task_warnings = gen["warnings"]
    tasks = [LearningTask.from_dict(t) for t in tasks_raw]
    res_by_ext = {
        (r.get("source_refs") or [{}])[0].get("external_id"): r for r in resources if r.get("source_refs")
    }
    asg_by_ext = {a["source_ref"]["external_id"]: a for a in assignments if a.get("source_ref")}
    defaults = tg_cfg_raw.get("default_minutes_by_type") or {}
    observations = [task_observation(t, res_by_ext, asg_by_ext, defaults) for t in tasks]
    fallback_used = [o for o in observations if o["fallback"]["used"]]
    extent_present = sum(1 for r in resources if r.get("extent_value") is not None)
    fetched_present = sum(1 for r in resources if r.get("fetched_at"))
    report["taskgen"] = {
        "task_count": len(tasks),
        "warnings": task_warnings,
        "extent_availability": {
            "resources_total": len(resources),
            "extent_present": extent_present,
            "fetched_present": fetched_present,
            "parse_status": dict(Counter(r.get("parse_status") for r in resources)),
            "mime_type": dict(Counter(str(r.get("mime_type")) for r in resources)),
            "kind": dict(Counter(r.get("kind") for r in resources)),
        },
        "fallback_ratio": f"{len(fallback_used)}/{len(observations)}",
        "platform_submitted_true": [o["id"] for o in observations if o["platform_submitted"]],
        "tasks": observations,
    }
    print(f"任务数：{len(tasks)}；extent 可用 {extent_present}/{len(resources)}；"
          f"兜底 {len(fallback_used)}/{len(observations)}；taskgen warnings {len(task_warnings)} 条")
    for o in observations:
        mark = "兜底" if o["fallback"]["used"] else "extent"
        subm = "（平台已提交）" if o["platform_submitted"] else ""
        print(f"  - [{mark}] {o['title']}｜{o['type']}｜{o['estimated_minutes']}min｜"
              f"{o['estimate_provenance']}{subm}")

    # 4) 可用性输入 + 排程
    print("\n-- 4) generate_plan --")
    avail_raw = load_json(Path(args.availability))
    availability = map_availability_contract_to_engine(avail_raw)
    tz = resolve_tz(availability.timezone)
    today_date = dt.date.fromisoformat(args.today) if args.today else dt.datetime.now(tz).date()
    horizon_start_dt = dt.datetime.combine(today_date, dt.time.min, tzinfo=tz)
    horizon_end_dt = dt.datetime.combine(
        today_date + dt.timedelta(days=args.horizon_days), dt.time.min, tzinfo=tz
    )
    options = ScheduleOptions(
        horizon_start=horizon_start_dt.isoformat(timespec="minutes"),
        horizon_end=horizon_end_dt.isoformat(timespec="minutes"),
        reference_time=horizon_start_dt.isoformat(timespec="minutes"),
        config=ScheduleConfig.from_dict(sc_cfg_raw),
    )
    report["plan_input"] = {
        "availability_source": str(args.availability),
        "availability_note": "契约样例 availability.json = 模拟输入（回放库无 availability 行）；非用户真实配置",
        "availability": avail_raw,
        "horizon_start": options.horizon_start,
        "horizon_end": options.horizon_end,
        "reference_time": options.reference_time,
        "reference_time_note": "取 horizon 起点（镜像 B 的含尾日/半开区间换算；B 生产用 now()，此处固定起点保可复现）",
        "locked_blocks": [],
    }
    plan = generate_plan(tasks, availability, options)
    report["plan"] = summarize_plan(plan)
    print(f"时间块 {len(plan.blocks)} 个、未安排 {len(plan.unassigned)} 项、plan warnings {len(plan.warnings)} 条")
    for b in plan.blocks:
        print(f"  - {b.task_id}｜{b.starts_at} ~ {b.ends_at}")
    for u in plan.unassigned:
        print(f"  ! {u.task_id}｜{u.reason_code}｜{u.message}")

    # 5) validate_plan + 独立台账
    print("\n-- 5) validate_plan + 独立台账 --")
    violations = validate_plan(plan, tasks, availability, options)
    report["validate"] = {"violations": [v.to_dict() for v in violations], "pass": not violations}
    check("validate_plan 无违规（无重叠/窗口内/每日上限/截止内/时长完整）", not violations,
          f"{len(violations)} 项")
    ledger = independent_ledger(plan, availability)
    report["ledger"] = ledger
    check("独立台账：无重叠", not ledger["overlaps"], f"{ledger['overlaps']}")
    check("独立台账：全部在可用窗口内", not ledger["outside_window"], f"{ledger['outside_window']}")
    check("独立台账：不超每日上限", not ledger["over_daily_limit"], f"{ledger['over_daily_limit']}")
    check("独立台账：全部在 horizon 内", not ledger["out_of_horizon"], f"{ledger['out_of_horizon']}")

    # 6) PD-3
    print("\n-- 6) PD-3：平台已提交任务与排程容量 --")
    submitted_ids = {t.id for t in tasks if t.platform_submitted}
    in_blocks = sorted({b.task_id for b in plan.blocks if b.task_id in submitted_ids})
    in_unassigned = sorted({u.task_id for u in plan.unassigned if u.task_id in submitted_ids})
    report["pd3"] = {
        "submitted_task_ids": sorted(submitted_ids),
        "in_blocks": in_blocks,
        "in_unassigned": in_unassigned,
    }
    check("PD-3：平台已提交任务不进入任何时间块", not in_blocks, f"in_blocks={in_blocks}")
    check("PD-3：平台已提交任务不进入未安排清单", not in_unassigned, f"in_unassigned={in_unassigned}")

    cf_tasks = [dataclasses.replace(t, platform_submitted=False) for t in tasks]
    cf_plan = generate_plan(cf_tasks, availability, options)
    cf_in_blocks = sorted({b.task_id for b in cf_plan.blocks if b.task_id in submitted_ids})
    cf_in_unassigned = [
        {"task_id": u.task_id, "reason_code": u.reason_code}
        for u in cf_plan.unassigned
        if u.task_id in submitted_ids
    ]
    report["counterfactual"] = {
        "note": "反事实观察（非真实数据改动）：仅把 platform_submitted 翻转为 False 后重排，对照 PD-3 的排除效果",
        "blocks": len(cf_plan.blocks),
        "unassigned": len(cf_plan.unassigned),
        "submitted_in_blocks": cf_in_blocks,
        "submitted_in_unassigned": cf_in_unassigned,
    }
    print(f"反事实（翻转 platform_submitted）：提交任务进入时间块={cf_in_blocks}，"
          f"进入未安排={cf_in_unassigned}；总块数 {len(plan.blocks)} → {len(cf_plan.blocks)}")

    report["checks"] = CHECKS
    report["failed"] = FAILED
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写出：{out_path}")
    print(f"\n=== 核对结果：{'全部通过' if not FAILED else '存在失败项 ' + str(FAILED)} ===")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
