#!/usr/bin/env python3
"""同步编排与故障恢复（A7；A4 §1–§4 的代码落地）。

职责边界：编排器只驱动适配器（只读），不落库、不做变更判定——
持久化、版本递增与幂等由 B 侧实现（A3 §4 / sync-result 语义保证）。

要点：
  - 两阶段分离：阶段一 `sync_course`（元数据）；阶段二 `fetch_contents`（大文件/内容获取，独立触发）；
  - 游标不透明：原样传入传出，编排器不解析；**不以时间（last_success_at 等）判定变更**；
  - 有界重试 + 指数退避：仅对 retryable 错误重试，次数上限 `max_retries`；
  - `AUTH_EXPIRED`（retryable=false）立即上抛、不循环重试，恢复路径随 `error.details.action` 下发；
  - 频率限制可配置（默认 0.5 rps，即最小间隔 2s）；
  - 协作式取消：`cancel()` 后在检查点抛 `SyncCancelled`；恢复 = 调用方以已保存游标重新同步
    （游标只在整门课程成功后由调用方推进）；
  - 单项失败不抹除整门课程：阶段一单项错误在 `result.errors` 内隔离；
    阶段二逐项重试，仍失败则记为该条目的 transient_error；非重试类单项错误映射为
    终态 outcome（PERMISSION_DENIED→denied、RESOURCE_NOT_FOUND→gone）并继续；
    连接级错误（AUTH_EXPIRED：等待用户登录；UPSTREAM_CHANGED：形态变更需报警降级）中止整批。

所有日志经 `redact` 统一脱敏（A8；模块 logger 已挂 RedactingFilter）。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional, Sequence

from .adapters.base import AdapterError
from .redact import RedactingFilter

# 整批中止的错误码（A4 §5）：认证失效=任务暂停等待用户；形态变更=报警+降级。
# 其余非重试错误（PERMISSION_DENIED / RESOURCE_NOT_FOUND）为单项永久失败，记录后继续。
_BATCH_ABORT_CODES = frozenset({"AUTH_EXPIRED", "UPSTREAM_CHANGED"})

_logger = logging.getLogger("connectors.sync")
if not any(isinstance(f, RedactingFilter) for f in _logger.filters):
    _logger.addFilter(RedactingFilter())


class SyncCancelled(Exception):
    """协作式取消：`completed` 为已完成条目 ID（供调用方记录断点）。"""

    def __init__(self, completed: Optional[list[str]] = None) -> None:
        self.completed = list(completed or [])
        super().__init__(f"sync cancelled（已完成 {len(self.completed)} 项）")


class SyncOrchestrator:
    """驱动 PlatformAdapter 的同步编排器（阶段一/阶段二分离）。"""

    def __init__(
        self,
        adapter: Any,
        *,
        rate_limit_rps: float = 0.5,
        max_retries: int = 3,
        backoff_base_seconds: float = 1.0,
        sleep: Optional[Callable[[float], None]] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        if rate_limit_rps <= 0:
            raise ValueError("rate_limit_rps 必须 > 0")
        if max_retries < 0:
            raise ValueError("max_retries 不得为负")
        self._adapter = adapter
        self._min_interval = 1.0 / rate_limit_rps
        self._max_retries = max_retries
        self._backoff_base = backoff_base_seconds
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._last_call_at: Optional[float] = None
        self._cancel_requested = False

    # ---------- 取消 ----------

    def cancel(self) -> None:
        """协作式取消：在下一个检查点（每次平台调用前）生效。"""
        self._cancel_requested = True

    @property
    def cancelled(self) -> bool:
        return self._cancel_requested

    def _check_cancelled(self, completed: Optional[list[str]] = None) -> None:
        if self._cancel_requested:
            raise SyncCancelled(completed)

    # ---------- 阶段一：元数据 ----------

    def sync_course(
        self,
        course_id: str,
        *,
        since_cursor: Optional[str] = None,
        kinds: Optional[Sequence[str]] = None,
    ) -> dict[str, Any]:
        """同步一门课程的元数据（SyncResult）。

        游标原样透传（不解析）；单项错误保留在 `result.errors`，不因非空而失败。
        """
        result = self._call_with_retry(
            lambda: self._adapter.sync_course(
                course_id, since_cursor=since_cursor, kinds=kinds
            ),
            label=f"sync_course({course_id})",
        )
        stats = result.get("stats", {})
        _logger.info(
            "sync 完成 course=%s mode=%s upserted=%s deleted=%s failed=%s",
            course_id, result.get("mode"), stats.get("upserted"),
            stats.get("deleted"), stats.get("failed"),
        )
        return result

    # ---------- 阶段二：内容获取 ----------

    def fetch_contents(
        self,
        resource_ids: Sequence[str],
        *,
        on_outcome: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> list[dict[str, Any]]:
        """逐项获取资源内容（FetchOutcome）。返回顺序与入参一致。

        单项语义：denied/gone 为终态（不重试）；transient_error 有界重试后仍失败记为该项失败。
        非重试类错误映射终态（PERMISSION_DENIED→denied、RESOURCE_NOT_FOUND→gone）并继续；
        连接级错误（AUTH_EXPIRED / UPSTREAM_CHANGED）中止整批并上抛。
        """
        results: list[dict[str, Any]] = []
        for resource_id in resource_ids:
            self._check_cancelled([r["resource_id"] for r in results])
            wrapper = self._fetch_one(resource_id)
            results.append(wrapper)
            if on_outcome is not None:
                on_outcome(wrapper)
        return results

    def _fetch_one(self, resource_id: str) -> dict[str, Any]:
        attempt = 0
        while True:
            self._check_cancelled()
            self._throttle()
            try:
                outcome = self._adapter.fetch_resource(resource_id)
            except AdapterError as err:
                if err.code in _BATCH_ABORT_CODES:
                    raise
                if not err.retryable:
                    # 单项永久失败：映射终态并继续其他项（A4 §5）
                    return {
                        "resource_id": resource_id,
                        "outcome": self._terminal_outcome(err),
                        "attempts": attempt + 1,
                        "error": err.error,
                    }
                if attempt >= self._max_retries:
                    _logger.warning("fetch 失败 resource=%s code=%s（重试耗尽）", resource_id, err.code)
                    return {
                        "resource_id": resource_id,
                        "outcome": {"status": "transient_error", "handle": None, "denial": None},
                        "attempts": attempt + 1,
                        "error": err.error,
                    }
                self._backoff(attempt, f"fetch({resource_id})", err.code)
                attempt += 1
                continue

            if outcome.get("status") == "transient_error" and attempt < self._max_retries:
                self._backoff(attempt, f"fetch({resource_id})", "TRANSIENT")
                attempt += 1
                continue
            return {"resource_id": resource_id, "outcome": outcome, "attempts": attempt + 1}

    @staticmethod
    def _terminal_outcome(err: AdapterError) -> dict[str, Any]:
        """非重试类单项错误 → 终态 FetchOutcome 映射（其余码由调用方报警处置）。"""
        if err.code == "PERMISSION_DENIED":
            return {
                "status": "denied",
                "handle": None,
                "denial": {"reason": "permission_denied", "message": err.error["message"]},
            }
        if err.code == "RESOURCE_NOT_FOUND":
            return {"status": "gone", "handle": None, "denial": None}
        raise err

    # ---------- 重试 / 限速 ----------

    def _call_with_retry(self, fn: Callable[[], Any], *, label: str) -> Any:
        attempt = 0
        while True:
            self._check_cancelled()
            self._throttle()
            try:
                return fn()
            except AdapterError as err:
                # AUTH_EXPIRED 等 retryable=false：立即上抛，不循环重试（恢复路径随 details.action）
                if not err.retryable or attempt >= self._max_retries:
                    raise
                self._backoff(attempt, label, err.code)
                attempt += 1

    def _backoff(self, attempt: int, label: str, code: str) -> None:
        delay = self._backoff_base * (2 ** attempt)
        _logger.warning(
            "%s 失败（%s，可重试），%.1fs 后进行第 %d 次重试", label, code, delay, attempt + 1
        )
        self._sleep(delay)

    def _throttle(self) -> None:
        if self._last_call_at is not None:
            wait = self._min_interval - (self._clock() - self._last_call_at)
            if wait > 0:
                self._sleep(wait)
        self._last_call_at = self._clock()
