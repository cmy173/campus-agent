#!/usr/bin/env python3
"""HTTP 传输抽象（xzzd / zykt 真实适配器用；离线可测的关键接缝）。

凭据红线（总纲/任务书，必须遵守）：
  - 会话（cookie/token）由**调用方提供的会话对象**持有（本地服务侧，A12 降级方案）；
    适配器只传相对路径与查询参数，**不接触、不保存、不落盘、不打印任何凭据**；
  - 传输层与适配器均不得记录完整 URL 与 Referer（A1 §3.4-9：zykt 某端点 JWT 入查询参数）；
  - 不做登录、不做重定向跟随到认证页后的自动提交。

测试替身：`ScriptedTransport` 由测试自行定义（返回排队中的 TransportResponse 并记录请求），
适配器测试不触网（A5b 受控验证在浏览器侧独立执行，不经过本传输层）。
流式下载（A22）：`DownloadTransport.download` 为可选能力，签名媒体链接为绝对 URL；
受限/超护栏不在此层表达——由适配器映射为 denial/gone/错误码。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, runtime_checkable


@dataclass
class TransportResponse:
    """与 requests.Response 同形的最小子集。"""

    status_code: int
    text: str

    def json(self) -> Any:
        """解析 JSON；非 JSON（如 SSO 登录页 HTML）抛 ValueError。"""
        return json.loads(self.text)


class DownloadError(Exception):
    """下载通道错误（非 2xx 状态携带 status_code，由适配器映射为 denial/gone/错误码）。"""

    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class DownloadTooLarge(DownloadError):
    """下载中途超过护栏上限（实现方已中止并清理临时件）。"""

    def __init__(self, max_bytes: int) -> None:
        super().__init__(f"下载超过护栏上限 {max_bytes} 字节")
        self.max_bytes = max_bytes


@runtime_checkable
class HttpTransport(Protocol):
    """相对路径 + 查询参数 → TransportResponse。base_url 与凭据由实现方持有。"""

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> TransportResponse: ...


@runtime_checkable
class DownloadTransport(Protocol):
    """可选能力：流式下载绝对 URL（xzzd fetch_resource 的真实获取路径需要）。

    护栏语义：``max_bytes`` 非空时，累计字节超过上限必须中止并抛 DownloadTooLarge
    （实现方负责清理半成品）；失败时抛 DownloadError（状态码可选）。
    """

    def download(
        self,
        url: str,
        dest: Path,
        *,
        max_bytes: Optional[int] = None,
        chunk_size: int = ...,
    ) -> tuple[int, str]: ...


class RequestsTransport:
    """requests.Session 薄封装。会话由调用方（本地服务）创建并持有，本类不读取其内容。

    用法（B 侧本地服务；宿主以 A5b 实测为准，xzzd 为 courses.zju.edu.cn）：
        session = requests.Session()          # 用户跳转登录过程中由服务端维护
        transport = RequestsTransport("https://courses.zju.edu.cn", session)
        adapter = XzzdAdapter(connection_id="conn_x", transport=transport)
    """

    def __init__(self, base_url: str, session: Any, *, timeout: float = 15.0,
                 headers: Optional[dict[str, str] | Callable[[str], dict[str, str]]] = None,
                 params_for_path: Optional[Callable[[str], dict[str, Any]]] = None) -> None:
        self._base = base_url.rstrip("/")
        self._session = session
        self._timeout = timeout
        self._headers = headers
        self._params_for_path = params_for_path

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> TransportResponse:
        headers = self._headers(path) if callable(self._headers) else self._headers
        if self._params_for_path:
            params = {**(params or {}), **self._params_for_path(path)}
        resp = self._session.request(
            method,
            self._base + path,
            params=params,
            json=json_body,
            timeout=self._timeout,
            **({"headers": headers} if headers else {}),
        )
        return TransportResponse(status_code=resp.status_code, text=resp.text)

    def download(
        self,
        url: str,
        dest: Path,
        *,
        max_bytes: Optional[int] = None,
        chunk_size: int = 65536,
    ) -> tuple[int, str]:
        """流式下载**绝对 URL**（签名媒体链接不复用 base_url）到 dest，返回 (字节数, sha256 hex)。

        原子落盘：先写 ``dest.part``，成功后改名；任何失败（含中途超护栏）清理临时件后上抛。
        不记录 URL（签名链接的查询参数含 token，属凭据物料）。
        """
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        hasher = hashlib.sha256()
        size = 0
        try:
            with self._session.get(url, stream=True, timeout=self._timeout) as resp:
                if resp.status_code != 200:
                    raise DownloadError(f"HTTP {resp.status_code}", status_code=resp.status_code)
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=chunk_size):
                        if not chunk:
                            continue
                        size += len(chunk)
                        if max_bytes is not None and size > max_bytes:
                            raise DownloadTooLarge(max_bytes)
                        hasher.update(chunk)
                        fh.write(chunk)
            tmp.replace(dest)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return size, hasher.hexdigest()


def is_true(value: Any) -> Optional[bool]:
    """平台布尔字段三态归一：True/"1"/"true"/1 → True；False/"0"/"false"/0 → False；缺失 → None。

    None 表示"未知"，调用方须按保守策略处理（不猜测，A4 §2）。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off", ""):
            return False
    return None
