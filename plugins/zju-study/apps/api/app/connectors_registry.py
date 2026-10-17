"""平台连接器注册（进程内默认注册；A20 桥接）。

**为什么需要本模块**：`sync_engine` 的适配器解析顺序是"进程内注册表 → `STUDY_SYNC_ADAPTERS`
环境变量"。此前 API 进程**既不注册、也不读环境变量时的默认值**，导致：

- 未设 `STUDY_SYNC_ADAPTERS` 时，`POST /connections/{id}/check` 返回 502
  `DEPENDENCY_UNAVAILABLE`，`POST /connections/{id}/sync` 同理——
  即"配置漏了"被表现为"功能坏了"；
- 协调者在 2026-10-11 的实测中正是这样启动服务并把这个现象报告为"接线缺失"
  （见 `docs/coord/07-收尾轮评估报告.md` 之后的更正：桥接其实存在，是**配置未接线**）。

本模块在 API 启动时**默认注册两个适配器**，使默认部署开箱可用：

1. `xzzd`（学在浙大）→ `connectors.bridge:XzzdBridgeAdapter`：转发到本机受控浏览器
   会话服务（`python -m connectors.browser.session_runner serve`，缺省
   `http://127.0.0.1:8765`，可用 `XZZD_BRIDGE_URL` 覆盖）。**不含任何会话物料**。
2. `zykt`（智云课堂）→ `connectors.bridge:ZyktBridgeAdapter`：独立端口 8766，
   支持个人课表/学习记录发现课程、目录、场次详情与按需字幕；真实账号联调待确认。

**环境变量仍可覆盖**：`STUDY_SYNC_ADAPTERS` 中显式给出的平台会覆盖本模块的默认注册
（`_apply_env_overrides` 在注册后按环境变量再注册一次，注册表后写胜出）。

**导入方式**：`connectors` 包在仓库根，不在 `apps/api` 下；本模块用 `config.REPO_ROOT`
把自己所在的仓库根加入 `sys.path` 后再导入。若仓库结构变化导致导入失败，
**显式降级并记录**（不静默当作"已注册"）。
"""
from __future__ import annotations

import importlib
import logging
import sys
from typing import Any, Optional

from . import sync_engine
from .config import REPO_ROOT

LOG = logging.getLogger("app.connectors")

#: 默认注册表：平台 → "模块:属性"（与 STUDY_SYNC_ADAPTERS 同格式，便于审计与测试）
DEFAULT_ADAPTERS: dict[str, str] = {
    "xzzd": "connectors.bridge:XzzdBridgeAdapter",
    "zykt": "connectors.bridge:ZyktBridgeAdapter",
}

#: 本模块启动时实际注册成功的平台（供 /health 诊断与测试断言）
REGISTERED: dict[str, str] = {}
PROTOCOL_FACTORIES: dict[str, Any] = {}


def _ensure_repo_root_on_path() -> None:
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def _load(factory_spec: str) -> Optional[Any]:
    """按 "模块:属性" 加载；失败返回 None（调用方记录，不静默成功）。"""
    module_name, _, attr = factory_spec.partition(":")
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 —— 降级为未注册，错误留痕
        LOG.warning("连接器模块导入失败：%s（%s: %s）", module_name, type(exc).__name__, exc)
        return None
    entry = getattr(module, attr or "", None)
    if entry is None:
        LOG.warning("连接器入口不存在：%s", factory_spec)
        return None
    return entry


def _register(platform: str, factory: Any, spec: str) -> None:
    """注册到 sync（check/sync/fetch）与 check 两个解析面。"""
    # 类入口由 sync_engine 的协议桥接包裹（以 platform/connection_id 关键字实例化）
    sync_engine.register_sync_adapter(platform, sync_engine._bridge_protocol_adapter(factory))  # noqa: SLF001
    sync_engine.register_check_adapter(platform, sync_engine._bridge_check_adapter(factory))  # noqa: SLF001
    REGISTERED[platform] = spec
    PROTOCOL_FACTORIES[platform] = factory


def protocol_adapter(platform: str, connection_id: str) -> Any:
    factory = PROTOCOL_FACTORIES.get(platform)
    if factory is None:
        raise sync_engine.AdapterMissing(platform)
    return factory(platform=platform, connection_id=connection_id)


def register_default_adapters() -> dict[str, str]:
    """注册默认连接器；返回实际注册成功的映射（平台 → "模块:属性"）。

    幂等：重复调用结果一致（注册表后写覆盖同键）。
    """
    _ensure_repo_root_on_path()
    for platform, spec in DEFAULT_ADAPTERS.items():
        factory = _load(spec)
        if factory is None:
            # 未注册 → B 侧表现为 DEPENDENCY_UNAVAILABLE（如实报错，不假装可用）
            continue
        _register(platform, factory, spec)
    _apply_env_overrides()
    if REGISTERED:
        LOG.info("已注册平台连接器：%s（会话服务缺省 %s）",
                 ", ".join(f"{p}={s}" for p, s in sorted(REGISTERED.items())),
                 "http://127.0.0.1:8765")
    else:
        LOG.warning("未注册任何平台连接器：/connections/{id}/check 与 /sync 将返回 DEPENDENCY_UNAVAILABLE")
    return dict(REGISTERED)


def _apply_env_overrides() -> None:
    """`STUDY_SYNC_ADAPTERS` 显式配置覆盖默认注册（后写胜出）。"""
    for platform, spec in sync_engine.config.SYNC_ADAPTERS.items():
        factory = _load(spec)
        if factory is None:
            LOG.warning("环境变量指定的连接器不可用：%s → %s（保留已注册入口）", platform, spec)
            continue
        _register(platform, factory, spec)
        LOG.info("环境变量覆盖连接器：%s → %s", platform, spec)
