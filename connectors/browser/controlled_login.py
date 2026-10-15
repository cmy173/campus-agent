#!/usr/bin/env python3
"""受控浏览器登录可行性原型（A17）：持久化 profile + 用户自行登录 + 会话只读验证。

定位：M2 起步轮任务书 A17 的**最小可跑原型**（设计依据：A5-跳转式登录设计.md v0.2 §4）。
六条成功判定（任务书 §3.1）对应实现：
  1) `launch_persistent_context(user_data_dir=<独立目录>, headless=False)`——可见窗口、独立 profile；
  2) 打开平台登录入口常量（`adapters.xzzd.LOGIN_ENTRY_URL`，与 B14 定稿一致），平台自行 302 至 CAS；
  3) 凭据只发生在窗口内官方页；本模块**无任何凭据入参**（代码审查硬性检查项，见 tests）；
  4) `cookie_key_summary` 读浏览器上下文 cookie **键名与属性**；值从不进入结果/日志/磁盘；
  5) `run_verify` 用该会话发真实只读请求（页面内 fetch 路径 + requests 会话路径双证）；
  6) 可复现：同一命令可重复执行（独立会话＝关闭浏览器后重开同一 profile）。

运行（可复制）：
    python -m connectors.browser.controlled_login verify --tag run1          # 登录 + 只读验证
    python -m connectors.browser.controlled_login logout-test --tag logout   # A18：登出后失效形态
    python -m connectors.browser.controlled_login reset-session --tag reset  # 会话处置：清理凭证并实测失效形态

红线（本模块实现约束）：
  - 不接受以口令为参数的形态（无 password 入参/配置/环境变量/命令行）；
  - cookie 值仅存在于浏览器进程与请求会话对象内存；本模块不导出、不打印、不写盘 cookie 值；
  - 结果 JSON 仅含键名与响应形态（状态码/内容类型/条目数）；URL 记录前经 `redact_url` 剔除查询参数；
  - 日志挂 `RedactingFilter`（A8 兜底）；独立 profile 目录，默认在用户数据目录下、仓库之外。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_HERE = Path(__file__).resolve()
_ROOT = _HERE.parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from connectors.adapters.transport import RequestsTransport  # noqa: E402
from connectors.adapters.xzzd import LOGIN_ENTRY_URL, XzzdAdapter  # noqa: E402
from connectors.browser.session_cookies import _requests_session
from connectors.redact import RedactingFilter, redact, redact_url  # noqa: E402

LOG = logging.getLogger("connectors.browser")
if not LOG.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(_h)
    LOG.addFilter(RedactingFilter())
LOG.setLevel(logging.INFO)

# 辅助信号：A5b 曾观测 JWTUser（对 JS 可见）；A17 受控实测（2026-10-10，Keycloak 登录流）
# 四次观测均未见该 cookie——判定不依赖单一标记，以只读探针实测为准（启动即试 + 计数变化/120s 兜底）。
SESSION_MARKER_COOKIES = ("JWTUser",)
READ_CHECK_PAGE = "https://courses.zju.edu.cn/"
READ_CHECK_API = "/api/my-courses"
LOGOUT_URL = "https://courses.zju.edu.cn/ua/logout?platform=WEB"

# 结果文件默认写在 profile 目录同级（仓库之外），只含脱敏信息
DEFAULT_RESULT_DIRNAME = "a17-results"


def default_profile_dir() -> Path:
    """独立持久化 profile 目录（不读写用户主浏览器 profile；默认在仓库之外）。"""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "zjulearn" / "controlled-browser" / "profile"
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "zjulearn" / "controlled-browser" / "profile"


BROWSER_TEMP_ENV = "STUDY_BROWSER_TEMP_DIR"
BROWSER_PROFILE_ENV = "STUDY_BROWSER_PROFILE_DIR"


def _normalized_path(path: Path) -> Path:
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    # 只做 normpath；不要 resolve()，避免把 8.3 短路径展开到 ACL/完整性级别不同的长路径。
    return Path(os.path.normpath(str(path)))


def _temp_dir_candidates(profile_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    explicit = os.environ.get(BROWSER_TEMP_ENV, "").strip()
    if explicit:
        candidates.append(_normalized_path(Path(explicit)))
    profile = _normalized_path(profile_dir)
    candidates.append(profile.parent / "tmp")
    candidates.append(_ROOT / "var" / "tmp")
    return candidates


def _ensure_writable_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    probe = path / f".campus-agent-write-test-{os.getpid()}-{time.time_ns()}"
    try:
        with probe.open("xb") as stream:
            stream.write(b"")
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
    return path


def browser_temp_dir(profile_dir: Path) -> Path:
    """返回一个确实可写的浏览器临时目录。

    Playwright/Chromium 在 Windows 上会调用 ``mkdtemp``。若系统 ``%TEMP%``
    被安全软件、ACL 或完整性级别限制，用户会看到
    ``EPERM: operation not permitted, mkdtemp ...playwright-artifacts-XXXXXX``。
    这里不依赖系统临时目录，优先使用 profile 同级目录；可用
    ``STUDY_BROWSER_TEMP_DIR`` 显式覆盖。
    """
    errors: list[str] = []
    for candidate in _temp_dir_candidates(profile_dir):
        try:
            return _ensure_writable_dir(candidate)
        except OSError as exc:
            errors.append(f"{candidate}: {exc}")
    raise RuntimeError(
        "找不到可写的浏览器临时目录；可用 STUDY_BROWSER_TEMP_DIR 指定一个可写目录。"
        + "；".join(errors)
    )


def _profile_dir_candidates(profile_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    explicit = os.environ.get(BROWSER_PROFILE_ENV, "").strip()
    if explicit:
        candidates.append(_normalized_path(Path(explicit)))
    requested = _normalized_path(profile_dir)
    candidates.append(requested)
    candidates.append(_ROOT / "var" / "controlled-browser" / requested.name)
    return candidates


def writable_profile_dir(profile_dir: Path) -> Path:
    r"""返回一个确实可写的持久化 profile 目录。

    用户数据目录可能被 ACL/完整性级别限制到无法创建
    ``%LOCALAPPDATA%\zjulearn``。优先使用调用方传入的目录；不可写时回退到
    仓库的 ``var/controlled-browser/``，可用 ``STUDY_BROWSER_PROFILE_DIR``
    显式覆盖。
    """
    errors: list[str] = []
    for candidate in _profile_dir_candidates(profile_dir):
        try:
            return _ensure_writable_dir(candidate)
        except OSError as exc:
            errors.append(f"{candidate}: {exc}")
    raise RuntimeError(
        "找不到可写的浏览器 profile 目录；可用 STUDY_BROWSER_PROFILE_DIR 指定一个可写目录。"
        + "；".join(errors)
    )


def configure_browser_temp(profile_dir: Path) -> Path:
    """把当前进程及 Playwright Node/browser 子进程的临时目录切到可写位置。"""
    temp_dir = browser_temp_dir(profile_dir)
    os.environ["TEMP"] = str(temp_dir)
    os.environ["TMP"] = str(temp_dir)
    tempfile.tempdir = str(temp_dir)
    return temp_dir


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def cookie_key_summary(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """cookie **键名与属性**摘要——值字段绝不复制进输出（红线：不导出/不打印值）。"""
    summary = []
    for ck in cookies:
        summary.append({
            "name": str(ck.get("name", "")),
            "domain": str(ck.get("domain", "")),
            "path": str(ck.get("path", "")),
            "http_only": bool(ck.get("httpOnly", False)),
            "secure": bool(ck.get("secure", False)),
            "same_site": ck.get("sameSite"),
            "session": ck.get("expires", -1) in (-1, None),
        })
    return sorted(summary, key=lambda k: (k["name"], k["domain"], k["path"]))


def has_session_marker(cookies: list[dict[str, Any]],
                       markers: tuple[str, ...] = SESSION_MARKER_COOKIES) -> bool:
    return any(str(ck.get("name", "")) in markers for ck in cookies)


def domain_distribution(summary: list[dict[str, Any]]) -> dict[str, int]:
    dist: dict[str, int] = {}
    for item in summary:
        dist[item["domain"]] = dist.get(item["domain"], 0) + 1
    return dict(sorted(dist.items()))


def classify_read_result(status: int, content_type: str, json_parsed: bool,
                         redirected: bool) -> str:
    """只读请求结果判定：ok / auth_required / unexpected（不含任何响应正文）。"""
    ct = (content_type or "").lower()
    if redirected or status in (301, 302, 303, 307, 308, 401, 419):
        return "auth_required"
    if status == 200 and "json" in ct and json_parsed:
        return "ok"
    if status == 200 and "html" in ct:
        return "auth_required"  # 200 但落到登录页 HTML（A5b 实测形态）
    return "unexpected"


_READ_JS = """
async () => {
  const ctl = new AbortController();
  const tid = setTimeout(() => ctl.abort(), 15000);
  try {
    const r = await fetch(%(api)r, {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({page: 1, page_size: 1}), signal: ctl.signal});
    const ct = (r.headers.get('content-type') || '').toLowerCase();
    const loc = new URL(r.url);
    let parsed = false, count = null, authFailed = false;
    if (ct.includes('json')) {
      try {
        const j = await r.json();
        authFailed = !!j && [401, 419, '401', '419'].includes(j.code);
        if (j && j.success !== false && !authFailed) {
          for (const k of ['list', 'courses', 'data']) {
            if (Array.isArray(j[k])) { parsed = true; count = j[k].length; break; }
          }
        }
      } catch (e) {}
    }
    return {status: authFailed ? 401 : r.status, content_type: ct, json_parsed: parsed,
            item_count: count, redirected: r.redirected,
            final_origin: loc.origin, final_path: loc.pathname};
  } catch (e) {
    return {status: 0, content_type: '', json_parsed: false,
            item_count: null, redirected: false,
            final_origin: '', final_path: '', error: String(e && e.name || e)};
  } finally {
    clearTimeout(tid);
  }
}
"""


def read_check_in_page(page: Any, description: str = "read-check/page") -> dict[str, Any]:
    """路径(a)：受控浏览器页面上下文内发只读请求（会话由浏览器自动携带）。

    `goto` 的 `ERR_ABORTED`（被页面自身跳转接管，例如已登录时的即时重定向链）不视为失败：
    短暂等待后仍在当前文档执行只读请求，结果如实标注导航告警。
    """
    started = utc_now()
    try:
        nav_warning = None
        try:
            page.goto(READ_CHECK_PAGE, wait_until="domcontentloaded", timeout=45000)
        except Exception as nav_exc:
            if "ERR_ABORTED" not in str(nav_exc):
                raise
            nav_warning = type(nav_exc).__name__
            page.wait_for_timeout(1500)
        # 绝对 URL：文档被跳转接管时（例如未登录跳 SSO），相对路径会被解析到 SSO 域
        api_url = READ_CHECK_PAGE.rstrip("/") + READ_CHECK_API
        raw = page.evaluate(_READ_JS % {"api": api_url})
        raw["verdict"] = classify_read_result(
            int(raw.get("status", 0)), str(raw.get("content_type", "")),
            bool(raw.get("json_parsed")), bool(raw.get("redirected")),
        )
        raw["page_final_url"] = redact_url(page.url or "")
        if nav_warning:
            raw["nav_warning"] = nav_warning
        raw["step"] = description
        raw["at"] = started
        return raw
    except Exception as exc:  # 记录形态，不抛穿（可行性验证需如实记录）
        return {"step": description, "at": started, "verdict": "unexpected",
                "error": type(exc).__name__}


def read_check_with_requests(cookies: list[dict[str, Any]]) -> dict[str, Any]:
    """路径(b)：读取 cookie 到内存 → requests.Session → 走既有适配器/传输代码链。"""
    started = utc_now()
    result: dict[str, Any] = {"step": "read-check/requests", "at": started}
    session = None
    try:
        session = _requests_session(cookies, "courses.zju.edu.cn")
        if session is None:
            result.update({"cookies_loaded": 0, "state": "auth_required", "verdict": "auth_required"})
            return result
        transport = RequestsTransport("https://courses.zju.edu.cn", session, timeout=20.0)
        adapter = XzzdAdapter(connection_id="a17-feasibility", transport=transport)
        state = adapter.check_connection()
        result.update({
            "cookies_loaded": len(session.cookies), "cookies_skipped": len(cookies) - len(session.cookies),
            "state": state.get("state"),
            "verdict": state.get("state"),
        })
        if state.get("detail"):
            result["detail"] = redact(str(state["detail"]))
    except Exception as exc:
        result.update({"verdict": "unexpected", "error": type(exc).__name__})
    finally:
        if session is not None:
            session.close()
    return result


def _write_result(result_dir: Path, tag: str, payload: dict[str, Any]) -> Path:
    result_dir.mkdir(parents=True, exist_ok=True)
    path = result_dir / f"{tag}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _engine_info() -> dict[str, Any]:
    info = {"python": sys.version.split()[0]}
    try:
        from importlib.metadata import version
        info["playwright"] = version("playwright")
    except Exception:
        info["playwright"] = "unknown"
    return info


def _launch(profile_dir: Path, channel: str) -> tuple[Any, Any, Any]:
    """返回 (playwright, context, page)。playwright 延迟导入（离线测试无需安装）。"""
    from playwright.sync_api import sync_playwright
    # Windows 上系统 %TEMP% 可能不可写；必须在启动 Node driver 前切换。
    configure_browser_temp(profile_dir)
    pw = sync_playwright().start()
    proxy = os.environ.get("STUDY_PLATFORM_PROXY")
    extra = {"proxy": {"server": proxy}} if proxy else {}
    context = pw.chromium.launch_persistent_context(
        str(profile_dir), headless=False, channel=channel,
        args=["--no-first-run", "--no-default-browser-check",
              "--window-position=80,80", "--window-size=1280,860"] + ([] if proxy else ["--no-proxy-server"]),
        no_viewport=True,
        **extra,
    )
    page = context.pages[0] if context.pages else context.new_page()
    try:
        page.bring_to_front()
    except Exception:
        pass
    return pw, context, page


def _wait_for_session(context: Any, timeout_s: float) -> dict[str, Any]:
    """轮询会话信号（标记 cookie／计数变化／120s 周期兜底）→ 只读探针实测确认真实可用。

    探针在独立后台页执行，不影响用户当前标签页；cookie 只记录计数与键名，值不进入任何输出。
    """
    t0 = time.time()
    last_probe: Optional[float] = None
    last_probe_count: Optional[int] = None
    attempts = 0
    probe_page = None
    last_progress = -30.0
    history: list[dict[str, Any]] = []
    last_count: Optional[int] = None
    try:
        while time.time() - t0 < timeout_s:
            cookies = context.cookies()
            count = len(cookies)
            if count != last_count:
                last_count = count
                history.append({"at": utc_now(), "count": count})
            changed = count != last_probe_count
            due = last_probe is None or time.time() - last_probe >= 120.0
            cooled = last_probe is None or time.time() - last_probe >= 15.0
            if cooled and (has_session_marker(cookies) or changed or due):
                last_probe = time.time()
                last_probe_count = count
                attempts += 1
                if probe_page is None:
                    probe_page = context.new_page()
                r = read_check_in_page(probe_page, "session-probe")
                if r.get("error"):
                    try:
                        probe_page.close()
                    except Exception:
                        pass
                    probe_page = None  # 探针页异常（例如被关闭），下次重建
                if r.get("verdict") == "ok":
                    return {"detected": True, "elapsed_s": round(time.time() - t0, 1),
                            "probe_attempts": attempts, "probe": r,
                            "cookie_count_history": history}
            now = time.time() - t0
            if now - last_progress >= 30:
                last_progress = now
                LOG.info("等待登录中… 已 %.0fs（当前 cookie 数 %d）", now, count)
            time.sleep(3)
        return {"detected": False, "elapsed_s": round(time.time() - t0, 1),
                "probe_attempts": attempts, "cookie_count_history": history}
    finally:
        if probe_page is not None:
            try:
                probe_page.close()
            except Exception:
                pass


def run_verify(profile_dir: Path, entry_url: str, channel: str, timeout_s: float,
               result_dir: Path, tag: str, read_mode: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "tag": tag, "started_at": utc_now(), "engine": _engine_info(),
        "config": {"channel": channel, "headless": False,
                   "profile_dir_outside_repo": not str(profile_dir).startswith(str(_ROOT)),
                   "entry_url": redact_url(entry_url), "read_mode": read_mode},
        "steps": [],
    }
    pw = context = None
    try:
        profile_dir.mkdir(parents=True, exist_ok=True)
        pw, context, page = _launch(profile_dir, channel)
        result["engine"]["browser_version"] = context.browser.version
        pre_cookies = context.cookies()
        pre_marker = has_session_marker(pre_cookies)
        result["steps"].append({"at": utc_now(), "step": "launch",
                                "pre_existing_session": pre_marker,
                                "cookie_count": len(pre_cookies)})
        # 标记只用于诊断，不足以证明有效。先到平台首页复用持久化/学校 SSO；
        # 由平台决定是否需要登录，而不是强制跳登录表单或相信陈旧 JWTUser。
        entry_step: dict[str, Any] = {"at": utc_now(), "step": "open-platform-home"}
        try:
            page.goto(READ_CHECK_PAGE, wait_until="domcontentloaded", timeout=60000)
        except Exception as nav_exc:
            if "ERR_ABORTED" not in str(nav_exc):
                raise
            entry_step["nav_warning"] = type(nav_exc).__name__
            page.wait_for_timeout(1500)
        entry_step["landed_host"] = redact_url(page.url or "")
        result["steps"].append(entry_step)
        LOG.info("== 受控浏览器已启动；先验证持久化/学校 SSO 会话 ==")
        LOG.info("如平台需要登录，请在官方页面自行登录，连接器不会提交凭据。")
        wait = _wait_for_session(context, timeout_s)
        result["steps"].append({"at": utc_now(), "step": "session-wait", **wait})

        cookies = context.cookies()
        summary = cookie_key_summary(cookies)
        result["session"] = {
            "cookie_keys": summary,
            "domains": domain_distribution(summary),
            "markers_found": sorted({k["name"] for k in summary
                                     if k["name"] in SESSION_MARKER_COOKIES}),
            "cookie_count": len(cookies),
            "cookie_count_history": wait.get("cookie_count_history", []),
        }

        if not wait.get("detected"):
            result["verdict"] = "not_detected_within_timeout"
            return result

        if read_mode in ("both", "page"):
            r = read_check_in_page(page, "read-check/page")
            result["steps"].append({"at": utc_now(), **r})
        if read_mode in ("both", "requests"):
            r = read_check_with_requests(context.cookies())
            result["steps"].append(r)

        page_verdicts = [s.get("verdict") for s in result["steps"]
                         if str(s.get("step", "")).startswith("read-check")]
        result["verdict"] = "ok" if page_verdicts and all(v == "ok" for v in page_verdicts) \
            else f"degraded:{','.join(str(v) for v in page_verdicts)}"
    except Exception as exc:
        result["verdict"] = "error"
        result["error"] = type(exc).__name__
        LOG.error("verify 失败：%s", type(exc).__name__)
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass
        result["finished_at"] = utc_now()
        path = _write_result(result_dir, tag, result)
        LOG.info("结果已写入：%s（仅脱敏字段）", path)
    return result


def run_logout_test(profile_dir: Path, channel: str, result_dir: Path, tag: str) -> dict[str, Any]:
    """A18：受控会话内主动登出 → 重放只读请求，记录失效形态（须先经用户同意后执行）。"""
    result: dict[str, Any] = {"tag": tag, "started_at": utc_now(),
                              "engine": _engine_info(), "steps": []}
    pw = context = None
    try:
        pw, context, page = _launch(profile_dir, channel)
        pre = context.cookies()
        result["steps"].append({"at": utc_now(), "step": "launch",
                                "pre_existing_session": has_session_marker(pre),
                                "cookie_count": len(pre)})
        pre_check = read_check_in_page(page, "read-check/pre-logout")
        result["steps"].append(pre_check)
        if pre_check.get("verdict") != "ok":
            result["verdict"] = "no_valid_session_to_logout"
            return result

        result["before"] = {"cookie_keys": cookie_key_summary(context.cookies())}
        page.goto(LOGOUT_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        result["steps"].append({"at": utc_now(), "step": "logout-navigated",
                                "landed": redact_url(page.url or "")})
        result["after"] = {"cookie_keys": cookie_key_summary(context.cookies())}
        r = read_check_in_page(page, "read-check/post-logout")
        result["steps"].append(r)
        result["verdict"] = f"post_logout:{r.get('verdict')}"
    except Exception as exc:
        result["verdict"] = "error"
        result["error"] = type(exc).__name__
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass
        result["finished_at"] = utc_now()
        path = _write_result(result_dir, tag, result)
        LOG.info("结果已写入：%s（仅脱敏字段）", path)
    return result


def run_reset_session(profile_dir: Path, channel: str, result_dir: Path, tag: str) -> dict[str, Any]:
    """会话处置：清理受控 profile 的凭证（cookie＋来源本地存储），并实测清理后的只读请求形态。

    A18 观测：平台登出端点直接导航**未**使会话失效（logout-*.json 中重放仍 200）——
    故处置手段采用凭证清理（仅限本模块独立 profile）。清理后重放只读请求，得到
    「登录已失效」的真实形态，作为恢复检测（recovery 路径）的基线证据。
    """
    result: dict[str, Any] = {"tag": tag, "started_at": utc_now(),
                              "engine": _engine_info(), "steps": []}
    pw = context = None
    try:
        pw, context, page = _launch(profile_dir, channel)
        result["steps"].append({"at": utc_now(), "step": "launch",
                                "cookie_count": len(context.cookies())})
        try:
            page.goto(READ_CHECK_PAGE, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass  # 只为取得同源上下文以清本地存储；导航失败不阻断清理
        context.clear_cookies()
        try:
            page.evaluate("() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }")
        except Exception:
            pass
        result["steps"].append({"at": utc_now(), "step": "credentials-cleared",
                                "cookie_count": len(context.cookies())})
        r = read_check_in_page(page, "read-check/after-reset")
        result["steps"].append(r)
        result["verdict"] = f"reset:{r.get('verdict')}"
    except Exception as exc:
        result["verdict"] = "error"
        result["error"] = type(exc).__name__
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass
        result["finished_at"] = utc_now()
        path = _write_result(result_dir, tag, result)
        LOG.info("结果已写入：%s（仅脱敏字段）", path)
    return result


def _default_result_dir(profile_dir: Path) -> Path:
    return profile_dir.parent / DEFAULT_RESULT_DIRNAME


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="controlled_login",
        description="受控浏览器登录可行性原型（A17）——用户自行登录、会话仅内存、只读验证")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--profile-dir", type=Path, default=default_profile_dir(),
                        help="独立持久化 profile 目录（默认在用户数据目录下、仓库之外）")
        sp.add_argument("--channel", default="msedge", help="浏览器 channel（默认 msedge，可 chrome）")
        sp.add_argument("--result-dir", type=Path, default=None, help="结果 JSON 目录（默认 profile 同级）")
        sp.add_argument("--tag", default="run", help="结果文件标签（如 run1/run2/logout）")

    sp_verify = sub.add_parser("verify", help="启动受控浏览器并验证会话与只读请求")
    add_common(sp_verify)
    sp_verify.add_argument("--entry-url", default=LOGIN_ENTRY_URL, help="平台登录入口（默认 B14 常量）")
    sp_verify.add_argument("--timeout", type=float, default=900.0, help="等待用户登录秒数（默认 900）")
    sp_verify.add_argument("--read-mode", choices=["both", "page", "requests"], default="both")

    sp_logout = sub.add_parser("logout-test", help="A18：登出后重放只读请求（须用户同意）")
    add_common(sp_logout)

    sp_reset = sub.add_parser("reset-session", help="会话处置：清理受控 profile 凭证并实测失效形态")
    add_common(sp_reset)

    args = parser.parse_args(argv)
    args.profile_dir = writable_profile_dir(args.profile_dir)
    result_dir = args.result_dir or _default_result_dir(args.profile_dir)

    if args.cmd == "verify":
        r = run_verify(args.profile_dir, args.entry_url, args.channel,
                       args.timeout, result_dir, args.tag, args.read_mode)
        ok = str(r.get("verdict")) == "ok"
    elif args.cmd == "logout-test":
        r = run_logout_test(args.profile_dir, args.channel, result_dir, args.tag)
        # 登出测试的成功形态＝重放得到非 ok（失效）且流程完整走完
        ok = str(r.get("verdict", "")).startswith("post_logout:")
    else:
        r = run_reset_session(args.profile_dir, args.channel, result_dir, args.tag)
        # 处置的成功形态＝清理后重放得到登录失效形态（恢复检测基线）
        ok = str(r.get("verdict")) == "reset:auth_required"

    print(json.dumps({"tag": r.get("tag"), "verdict": r.get("verdict")},
                     ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
