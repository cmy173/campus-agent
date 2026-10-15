#!/usr/bin/env python3
"""Mock 适配器（A3/A4）：由 connectors/samples/ 驱动的确定性实现。

用途：M1 主通道——B/C/D 在无真实账号时据此完成端到端闭环。
**全部输出为模拟验证数据；不得表述为真实采集成功。**

确定性：构造时固定虚拟时钟（默认 2026-10-08T09:10:00+08:00），游标确定性生成
（不透明字符串，消费方不得解析）；同一调用序列可在任何机器重放出字节一致的输出。

场景（scenario）：
  default          首同步=样例全量快照；凭游标再同步=无新增（无删除）【默认】
  empty            始终空增量（验证“空响应不产生删除”）
  version_update   首同步 res_01a7=v1；凭游标再同步=同名新版本 v2 增量
  partial_failure  复刻样例 sync-result.json（部分成功：upsert+显式删除+单项错误）
  auth_required    连接=登录失效；sync 抛 AdapterError(AUTH_EXPIRED)
  network_error    连接=网络错误；sync 抛 AdapterError(NETWORK_ERROR)
  blocked          连接=被平台拒绝；sync 抛 AdapterError(PERMISSION_DENIED)
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Sequence

try:
    from .base import AdapterError
except ImportError:  # 直接以脚本路径加载时
    from base import AdapterError  # type: ignore

SCENARIOS = (
    "default",
    "empty",
    "version_update",
    "partial_failure",
    "auth_required",
    "network_error",
    "blocked",
)

_ENTRY_URLS = {"xzzd": "https://course.zju.edu.cn/ua/login", "zykt": "https://classroom.zju.edu.cn/"}

_CONNECTION_STATE_BY_SCENARIO = {
    "default": "ok",
    "empty": "ok",
    "version_update": "ok",
    "partial_failure": "ok",
    "auth_required": "auth_required",
    "network_error": "network_error",
    "blocked": "blocked",
}

_RESOURCE_UPSERT_KEYS = (
    "id", "kind", "title", "version", "content_hash", "size_bytes", "mime_type",
    "access_level", "source_updated_at", "extent_value", "extent_unit",
)
_ASSIGNMENT_UPSERT_KEYS = (
    "id", "title", "instructions", "due_at", "due_precision", "submission_status",
)
_RECORDING_UPSERT_KEYS = (
    "id", "title", "started_at", "duration_seconds", "canonical_url",
    "transcript_status", "subtitle_languages", "transcript_next_check_at",
)

_MIME_SUFFIX = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
}


def _synth_prev_hash(external_id: str) -> str:
    """v1 的合成前值哈希（mock 专用；不与样例 v2 值冲突，且确定可复现）。"""
    return "sha256:" + hashlib.sha256(f"mock-prev:{external_id}".encode()).hexdigest()


class MockAdapter:
    """samples 驱动的确定性适配器。实现 PlatformAdapter 协议。"""

    connector_version = "0.1.0-mock"

    _DEFAULT_NOW = "2026-10-08T09:10:00+08:00"

    def __init__(
        self,
        *,
        platform: str = "xzzd",
        connection_id: str = "conn_mock",
        scenario: str = "default",
        samples_dir: Optional[Path] = None,
        now: Optional[str] = None,
    ) -> None:
        if scenario not in SCENARIOS:
            raise ValueError(f"未知场景 {scenario!r}；可用：{', '.join(SCENARIOS)}")
        self.platform = platform
        self.connection_id = connection_id
        self.scenario = scenario
        self._now = now or self._DEFAULT_NOW
        self._samples_dir = Path(samples_dir) if samples_dir else (
            Path(__file__).resolve().parents[1] / "samples"
        )
        self._cache: dict[str, Any] = {}

    # ---------- 五操作面 ----------

    def check_connection(self) -> dict[str, Any]:
        state = _CONNECTION_STATE_BY_SCENARIO[self.scenario]
        action = None
        detail = None
        if state == "auth_required":
            action = {
                "type": "user_login",
                "entry_url": _ENTRY_URLS[self.platform],
                "message": "登录已失效，请在连接器窗口完成登录后重试",
            }
            detail = "请求被重定向至统一身份认证（模拟）"
        elif state == "network_error":
            detail = "上游不可达（模拟）"
        elif state == "blocked":
            detail = "平台拒绝访问（模拟：风控/权限）"
        return {
            "platform": self.platform,
            "state": state,
            "checked_at": self._now,
            "action": action,
            "detail": detail,
        }

    def get_capabilities(self) -> dict[str, Any]:
        entry = next(
            p for p in self._load("capabilities.json")["platforms"]
            if p["platform"] == self.platform
        )
        caps = copy.deepcopy(entry)
        caps["connector_version"] = self.connector_version
        caps["checked_at"] = self._now
        return caps

    def list_courses(
        self, cursor: Optional[str] = None, term: Optional[str] = None
    ) -> dict[str, Any]:
        items = [
            copy.deepcopy(c)
            for c in self._load("courses.json")["courses"]
            if any(ref["platform"] == self.platform for ref in c["source_refs"])
        ]
        if term is not None:
            items = [c for c in items if c["term"] == term]
        return {"items": items, "next_cursor": None, "errors": []}

    def sync_course(
        self,
        course_id: str,
        since_cursor: Optional[str] = None,
        kinds: Optional[Sequence[str]] = None,
    ) -> dict[str, Any]:
        self._raise_if_fatal()
        stages = self._stages(course_id)
        issued = {self._cursor(course_id, k): k for k in range(len(stages))}

        if since_cursor is None:
            idx, next_cursor = 0, self._cursor(course_id, 0)
        elif since_cursor in issued:
            idx = issued[since_cursor]
            if idx >= len(stages) - 1:
                return self._empty_result(course_id, since_cursor, next_cursor=since_cursor)
            idx, next_cursor = idx + 1, self._cursor(course_id, idx + 1)
        else:
            # 游标失效：全量对照（A4 §3；明确标注 mode=resync，避免“静默全量”）
            result = self._full_snapshot_result(course_id, since_cursor, mode="resync")
            return self._apply_kinds(result, kinds)

        payload = stages[idx]
        result = self._build_result(
            course_id,
            upserts=payload["upserts"],
            deletions=payload["deletions"],
            errors=payload["errors"],
            since_cursor=since_cursor,
            next_cursor=next_cursor,
            mode=payload.get("mode", "incremental"),
            stats=payload.get("stats"),
        )
        return self._apply_kinds(result, kinds)

    def fetch_resource(self, resource_id: str) -> dict[str, Any]:
        if self.scenario == "auth_required":
            raise AdapterError(
                "AUTH_EXPIRED",
                f"登录已失效，无法获取资源 {resource_id}",
                retryable=False,
                details={"platform": self.platform, "action": self._recovery_action()},
            )
        if self.scenario == "blocked":
            raise AdapterError(
                "PERMISSION_DENIED",
                f"平台拒绝访问（模拟）：{resource_id}",
                retryable=False,
                details={"platform": self.platform},
            )
        if self.scenario == "network_error":
            return {"status": "transient_error", "handle": None, "denial": None}

        resource = next(
            (
                r for r in self._load("resources.json")["resources"]
                if r["id"] == resource_id and self._ref_platform(r) == self.platform
            ),
            None,
        )
        if resource is None:
            return {"status": "gone", "handle": None, "denial": None}

        access = resource.get("access_level", "full")
        if access == "preview_only":
            return self._denied("preview_only", f"仅可预览，不提供下载：{resource['title']}")
        if access == "restricted":
            return self._denied("copyright_restricted", f"版权或权限受限，不可获取：{resource['title']}")

        suffix = Path(resource["title"]).suffix or _MIME_SUFFIX.get(resource.get("mime_type") or "", ".bin")
        expires_at = (
            datetime.fromisoformat(self._now) + timedelta(minutes=30)
        ).isoformat()
        return {
            "status": "ok",
            "handle": {
                "kind": "local_file",
                "ref": f"mock-staging/{resource['id']}-v{resource['version']}{suffix}",
                "expires_at": expires_at,
                "sha256": resource["content_hash"],
                "size": resource["size_bytes"],
            },
            "denial": None,
        }

    # ---------- 内部 ----------

    def _load(self, name: str) -> Any:
        if name not in self._cache:
            self._cache[name] = json.loads(
                (self._samples_dir / name).read_text(encoding="utf-8")
            )
        return self._cache[name]

    def _recovery_action(self) -> dict[str, Any]:
        return {
            "type": "user_login",
            "entry_url": _ENTRY_URLS[self.platform],
            "message": "登录已失效，请在连接器窗口完成登录后重试",
        }

    def _raise_if_fatal(self) -> None:
        if self.scenario == "auth_required":
            raise AdapterError(
                "AUTH_EXPIRED",
                "登录已失效，同步中止（不循环重试）",
                retryable=False,
                details={"platform": self.platform, "action": self._recovery_action()},
            )
        if self.scenario == "network_error":
            raise AdapterError(
                "NETWORK_ERROR",
                "网络/上游不可达（模拟）",
                retryable=True,
                details={"platform": self.platform},
            )
        if self.scenario == "blocked":
            raise AdapterError(
                "PERMISSION_DENIED",
                "平台拒绝访问（模拟：风控/权限）",
                retryable=False,
                details={"platform": self.platform},
            )

    def _cursor(self, course_id: str, stage: int) -> str:
        digest = hashlib.sha256(
            f"mock:{self.platform}:{course_id}:stage{stage}".encode()
        ).hexdigest()[:8]
        return f"cur_{digest}"

    def _ref_platform(self, entity: dict[str, Any]) -> Optional[str]:
        refs = entity.get("source_refs") or [entity.get("source_ref")]
        return refs[0]["platform"] if refs and refs[0] else None

    def _resource_upsert(self, entity: dict[str, Any]) -> dict[str, Any]:
        upsert = {k: entity[k] for k in _RESOURCE_UPSERT_KEYS if k in entity}
        upsert["source_ref"] = copy.deepcopy(entity["source_refs"][0])
        return upsert

    def _assignment_upsert(self, entity: dict[str, Any]) -> dict[str, Any]:
        upsert = {k: entity[k] for k in _ASSIGNMENT_UPSERT_KEYS if k in entity}
        upsert["source_ref"] = copy.deepcopy(entity["source_ref"])
        return upsert

    def _recording_upsert(self, entity: dict[str, Any]) -> dict[str, Any]:
        upsert = {k: entity[k] for k in _RECORDING_UPSERT_KEYS if k in entity}
        upsert["source_ref"] = copy.deepcopy(entity["source_ref"])
        return upsert

    def _snapshot_upserts(self, course_id: str) -> dict[str, list[dict[str, Any]]]:
        resources = [
            self._resource_upsert(r)
            for r in self._load("resources.json")["resources"]
            if r["course_id"] == course_id and self._ref_platform(r) == self.platform
        ]
        assignments = [
            self._assignment_upsert(a)
            for a in self._load("assignments.json")["assignments"]
            if a["course_id"] == course_id and self._ref_platform(a) == self.platform
        ]
        recordings = [
            self._recording_upsert(r)
            for r in self._load("recordings.json")["recordings"]
            if r["course_id"] == course_id and self._ref_platform(r) == self.platform
        ]
        return {"resources": resources, "assignments": assignments, "recordings": recordings}

    def _stages(self, course_id: str) -> list[dict[str, Any]]:
        if self.scenario == "empty":
            return [{
                "upserts": {"resources": [], "assignments": [], "recordings": []},
                "deletions": [],
                "errors": [],
            }]
        if self.scenario == "partial_failure":
            sample = self._load("sync-result.json")
            return [{
                "upserts": copy.deepcopy(sample["upserts"]),
                "deletions": copy.deepcopy(sample["deletions"]),
                "errors": copy.deepcopy(sample["errors"]),
                "stats": copy.deepcopy(sample["stats"]),
            }]
        if self.scenario == "version_update":
            stage0_world = self._snapshot_upserts(course_id)
            for upsert in stage0_world["resources"]:
                if upsert["title"] == "第03讲-线性表.pdf":
                    upsert["version"] = 1
                    upsert["content_hash"] = _synth_prev_hash(upsert["source_ref"]["external_id"])
            latest = self._snapshot_upserts(course_id)
            delta_resources = [
                u for u in latest["resources"] if u["title"] == "第03讲-线性表.pdf"
            ]
            return [
                {"upserts": stage0_world, "deletions": [], "errors": []},
                {"upserts": {"resources": delta_resources, "assignments": [], "recordings": []},
                 "deletions": [], "errors": []},
            ]
        return [{"upserts": self._snapshot_upserts(course_id), "deletions": [], "errors": []}]

    def _build_result(
        self,
        course_id: str,
        *,
        upserts: dict[str, list[dict[str, Any]]],
        deletions: list[dict[str, Any]],
        errors: list[dict[str, Any]],
        since_cursor: Optional[str],
        next_cursor: Optional[str],
        mode: str = "incremental",
        stats: Optional[dict[str, int]] = None,
    ) -> dict[str, Any]:
        if stats is None:
            upserted = sum(len(v) for v in upserts.values())
            stats = {
                "scanned": upserted + len(deletions) + len(errors),
                "upserted": upserted,
                "deleted": len(deletions),
                "failed": len(errors),
            }
        else:
            stats = copy.deepcopy(stats)
        return {
            "platform": self.platform,
            "connection_id": self.connection_id,
            "course_id": course_id,
            "mode": mode,
            "cursor": {"previous": since_cursor, "next": next_cursor},
            "upserts": upserts,
            "deletions": deletions,
            "errors": errors,
            "stats": stats,
            "observed_at": self._now,
        }

    def _empty_result(
        self, course_id: str, since_cursor: Optional[str], *, next_cursor: Optional[str]
    ) -> dict[str, Any]:
        return self._build_result(
            course_id,
            upserts={"resources": [], "assignments": [], "recordings": []},
            deletions=[],
            errors=[],
            since_cursor=since_cursor,
            next_cursor=next_cursor,
        )

    def _full_snapshot_result(
        self, course_id: str, since_cursor: Optional[str], *, mode: str
    ) -> dict[str, Any]:
        return self._build_result(
            course_id,
            upserts=self._snapshot_upserts(course_id),
            deletions=[],
            errors=[],
            since_cursor=since_cursor,
            next_cursor=self._cursor(course_id, 0),
            mode=mode,
        )

    def _apply_kinds(
        self, result: dict[str, Any], kinds: Optional[Sequence[str]]
    ) -> dict[str, Any]:
        if kinds is None:
            return result
        allowed = set(kinds)
        result["upserts"] = {
            k: (v if k in allowed else []) for k, v in result["upserts"].items()
        }
        upserted = sum(len(v) for v in result["upserts"].values())
        result["stats"]["upserted"] = upserted
        result["stats"]["scanned"] = (
            upserted + result["stats"]["deleted"] + result["stats"]["failed"]
        )
        return result

    @staticmethod
    def _denied(reason: str, message: str) -> dict[str, Any]:
        return {"status": "denied", "handle": None, "denial": {"reason": reason, "message": message}}
