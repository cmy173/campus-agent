"""ZJU 学业数据采集向导（交互式）。

流程（按“办法二”：先读教务，再让用户确认）：
    1. 打开浏览器，由**你自己**在窗口里登录浙大通行证（程序不接触你的密码）
    2. 登录成功后**自动读取教务个人信息**：专业 / 年级（入学年份）/ 学院 / 班级
       —— 来源是教务个人页与接口字段，不是凭空猜的
    3. 用读到的专业去匹配培养方案页标题，做**交叉校验**
    4. 回显给用户确认（可直接回车采用，也可手工纠正）
    5. 抓取：培养方案 / 已修课程（成绩） / 课表 / 选课目录（可选课程池）
    6. 保存原始页面 + 归一化 JSON（带来源与获取时间）；生成规划报告

字段来源与别名对齐 packages/learning 与旧版 profile_fields（zymc/nj/yxmc/bjmc…）。

安全性：
    - 只在你的电脑上运行；登录态保存在本机 var/guide-profile
    - 不读取、不打印、不上传任何密码；页面内容只落在 var/academics/
    - 只读：不点按钮、不提交表单、不触发选课/报名

用法：
    python tools/academic_guide.py                     # 完整向导（推荐）
    python tools/academic_guide.py --skip-select       # 跳过选课目录
    python tools/academic_guide.py --no-fetch          # 只用已保存文件出报告
    python tools/academic_guide.py --major X --enroll-year 2025   # 跳过确认
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time

_HERE = pathlib.Path(__file__).resolve().parent


def _detect_layout():
    """识别目录布局，返回 (程序目录, 数据目录)。

    本工具只负责抓取与整理材料，**不依赖 packages/learning**：
    解析工作交给 App 里的大模型完成。
    """
    prog_dir = _HERE
    data_root = _HERE.parent
    return prog_dir, data_root


PROG_DIR, ROOT = _detect_layout()
OUT_DIR = ROOT / "var" / "academics"
PROFILE_DIR = ROOT / "var" / "guide-profile"
READ_PROFILE_DIR = ROOT / "var" / "guide-read-profile"   # 读取阶段用，避免与登录窗口争用
CONFIG_PATH = ROOT / "var" / "academic-guide.json"
STATE_PATH = ROOT / "var" / "guide-session.json"

# 目标站点。可用环境变量 CAMPUS_ZDBK_BASE 覆盖（便于自测，或走不同校内入口）。
BASE = os.environ.get("CAMPUS_ZDBK_BASE") or "https://zdbk.zju.edu.cn/jwglxt/"

PAGE_PLAN = BASE + "pyfagl/pyfaxxcx_cxPyfaxscxIndex.html?gnmkdm=N153020&layout=default&su={su}"
PAGE_TIMETABLE = BASE + "kbcx/xskbcx_cxXskbcxIndex.html?gnmkdm=N253508&layout=default&su={su}"
PAGE_GRADE = BASE + "cxdy/xscjcx_cxXscjIndex.html?gnmkdm=N253508&layout=default&su={su}"
PAGE_SELECT = BASE + "xsxk/zzxkghb_cxZzxkGhbIndex.html?gnmkdm=N253530&layout=default&su={su}"
# 学籍信息：实测可用入口（学籍管理-主修专业确认，含本人专业/学院/班级/学号/姓名）。
# 注意：/xsxxxggl/xsgrxxwh_* 那个路径是 404，不要用。
PAGE_PROFILE = BASE + "zxzygl/zxzyqr_cxZxzyxsxxIndex.html?gnmkdm=N3015&layout=default&su={su}"

# 成绩接口：返回 items（含 kcmc/xf/cj/jd/xkkh）
API_GRADE = BASE + "cxdy/xscjcx_cxXscjIndex.html?doType=query&gnmkdm=N5083&su={su}"
# 培养方案查询：走 Index 页 + doType=query（实测可用；`_cxPyfaxx.html` 会返回 910 错误）
API_PLAN_LIST = BASE + "pyfagl/pyfaxxcx_cxPyfaxscxIndex.html?doType=query&gnmkdm=N153020&su={su}"
API_TIMETABLE = BASE + "kbcx/xskbcx_cxXsKb.html?gnmkdm=N253508&su={su}"

# 浙大统一身份认证登录页（带 service 参数，登录后回跳教务）
PAGE_CAS_LOGIN = ("https://zjuam.zju.edu.cn/cas/login"
                  "?service=https%3A%2F%2Fzdbk.zju.edu.cn%2Fjwglxt%2Fxtgl%2Flogin_ssologin.html")

DEFAULT_CONFIG = {
    "_comment": "本文件由向导读写；学号仅用于拼接教务地址，专业/年级优先从教务读取",
    "su": "",
    "major": "",
    "enroll_year": 0,
    "select_api": "",
}
_LOGIN_MARKERS = ("统一身份认证", "zjuam.zju.edu.cn", "userName", "请输入用户名", "账号登录")

# 与旧版 profile_fields.py 对齐的字段别名（含机器码与中文标签）
PROFILE_ALIASES: dict[str, tuple[str, ...]] = {
    "student_id": ("xh", "studentid", "student_id", "学号"),
    "name": ("xm", "name", "姓名"),
    # yxmc=院系名称 / jgmc=机构名称 都是教务常见字段，必须都收进来
    "college": ("jgmc", "yxmc", "xymc", "yxbmc", "bmmc", "collegename", "college",
                "学院", "院系", "学院名称", "院系名称"),
    "major": ("zymc", "majorname", "major", "专业", "专业名称", "主修专业", "所属专业"),
    "class_name": ("bjmc", "classname", "class_name", "行政班", "行政班级", "班级", "班级名称"),
    "year": ("njdm", "njmc", "nj", "rxnf", "rxnj", "enrollmentyear", "grade", "year",
             "年级", "入学年份", "入学年级"),
}


def log(msg: str = "") -> None:
    print(msg, flush=True)


def load_config() -> dict:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    merged = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            merged.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")) or {})
        except json.JSONDecodeError:
            pass
    return merged


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def looks_logged_out(html: str, url: str) -> bool:
    if "zjuam.zju.edu.cn" in url:
        return True
    return any(m in html for m in _LOGIN_MARKERS)


# ---------------------------------------------------------------------------
# 步骤 1：登录
# ---------------------------------------------------------------------------

def start_browser_and_wait_login(pw, su: str, login_timeout: int = 600, max_restarts: int = 3,
                                 already_logged_in: bool = False,
                                 storage_state: dict | None = None):
    """打开浏览器并等待用户登录；浏览器意外关闭时自动重开继续等。

    重要：等待期间**绝不对主标签页做 goto**。用重新导航轮询会把用户正在填写的
    登录表单清空（表现为"窗口闪一下、密码没输完就没了"）。这里主标签页只做一次
    初始跳转，轮询全部在隐藏标签页进行。

    另外：登录过程中浏览器可能被意外关闭（崩溃、误关、安全软件拦截）。旧版直接
    退出，用户看到的就是"窗口没了、程序也没了"。现在改为自动重开并继续等待。
    """
    attempt = 0
    last_error: str | None = None
    while attempt <= max_restarts:
        attempt += 1
        try:
            return _login_once(pw, su, login_timeout=login_timeout, attempt=attempt,
                               already_logged_in=already_logged_in,
                               storage_state=storage_state)
        except SystemExit as exc:
            msg = str(exc)
            if "浏览器窗口被关闭" not in msg:
                raise
            last_error = msg
            _log_line("第 %d 次：浏览器被关闭" % attempt)
            if attempt > max_restarts:
                break
            log("")
            log("浏览器窗口被关闭了（可能是误关、崩溃或被安全软件拦截）。")
            log("正在第 %d 次重新打开，请在弹出的窗口里登录..." % attempt)
            _log_line("准备第 %d 次重开" % attempt)
            time.sleep(2)
    _log_line("重开次数用尽：%s" % last_error)
    raise SystemExit(
        "浏览器反复被关闭（已重试 %d 次）。\n"
        "可尝试：1) 关闭可能拦截浏览器的安全软件；2) 检查 var\\guide-run.log；"
        "3) 手动另存教务页面后运行 run-guide.cmd --no-fetch" % max_restarts
    )


def _save_session(context) -> None:
    """把登录态显式落盘。

    持久化上下文本应自己保存 cookie，但进程被强杀时会丢——之前正是这样反复要求
    重新登录。这里额外把 storage_state 写到文件，作为可复用的登录态备份。
    """
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(STATE_PATH))
        _log_line("会话已保存：%s" % STATE_PATH)
    except Exception as exc:  # noqa: BLE001
        _log_line("会话保存失败（不致命）：%r" % exc)


def _login_once(pw, su: str, *, login_timeout: int, attempt: int,
                already_logged_in: bool = False, storage_state: dict | None = None):
    # 启动一个**独立的临时配置目录**，每次运行都不一样。
    #
    # 为什么不再复用 guide-profile：Edge 对同一 user-data-dir 只允许一个实例，
    # 一旦有残留进程（上次没退干净、或用户自己开的窗口）占着它，新实例就会
    # 立刻退出并输出"你已在此会话中打开"（表现为 TargetClosedError）。
    # 而 Playwright 的会话本来就活在进程内，不需要靠目录共享登录态；
    # 需要持久化时用 storage_state（下方 _save_session）即可。
    # 因此改用一次性目录，彻底消除这类冲突。
    use_profile = PROFILE_DIR.parent / ("run-profile-%d-%d" % (int(time.time()), attempt))
    try:
        if use_profile.exists():
            import shutil
            shutil.rmtree(use_profile, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass
    use_profile.mkdir(parents=True, exist_ok=True)
    _log_line("启动浏览器（第 %d 次），profile=%s" % (attempt, use_profile.name))
    log("正在启动浏览器（独立配置，不影响你日常的 Edge）...")

    def _launch():
        return pw.chromium.launch_persistent_context(
            user_data_dir=str(use_profile), channel="msedge", headless=False, args=[],
            timeout=120000,   # 冷启动可能较慢，给足时间
        )

    try:
        ctx = _launch()
    except Exception as exc:  # noqa: BLE001
        # 独立目录理论上不会冲突；仍失败则结束占用该目录的残留进程后重试一次。
        _log_line("首次启动失败：%r；清理残留进程与目录后重试" % exc)
        log("首次启动失败（%s），正在清理后重试..." % type(exc).__name__)
        for pid in _pids_with_profile(use_profile.name):
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=20)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(2)
        import shutil as _sh
        _sh.rmtree(use_profile, ignore_errors=True)
        use_profile.mkdir(parents=True, exist_ok=True)
        try:
            ctx = _launch()
            _log_line("清理后重试成功")
        except Exception as exc2:  # noqa: BLE001
            _log_line("重试仍失败：%r" % exc2)
            log("")
            log("浏览器启动失败两次。可能原因：")
            log("  1) Edge 被安全软件拦截（可在 var\\guide-run.log 查看细节）")
            log("  2) 磁盘空间不足或 var\\guide-profile 无写权限")
            log("  3) 需要以普通用户身份运行，而非管理员")
            raise

    # 注入原生登录取得的 Cookie，避免再次登录
    if storage_state and storage_state.get("cookies"):
        try:
            ctx.add_cookies(storage_state["cookies"])
            _log_line("已注入 %d 个 Cookie" % len(storage_state["cookies"]))
            log("已复用你的登录状态（%d 个 Cookie）。" % len(storage_state["cookies"]))
        except Exception as exc:  # noqa: BLE001
            _log_line("注入 Cookie 失败：%r" % exc)

    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    _log_line("浏览器已启动，标签页数=%d" % len(ctx.pages))

    login_page = page
    try:
        page.goto(PAGE_PLAN.format(su=su), wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)
    except Exception as exc:  # noqa: BLE001
        _log_line("初始跳转失败（不致命）：%r" % exc)

    if not looks_logged_out(page.content(), page.url):
        log("检测到已有登录态，跳过登录步骤。")
        _log_line("已有登录态")
        return ctx, page

    log("")
    log("=" * 66)
    log("请在弹出的浏览器窗口里登录浙大通行证。")
    log("密码只输入给浙江大学的登录页面，本程序不读取、不保存。")
    log("登录完成后无需任何操作，程序会自动继续（最多等待 %d 分钟）。" % max(1, login_timeout // 60))
    log("=" * 66)
    _log_line("进入等待登录循环")

    # 等待登录：只观察**当前这一个标签页**，绝不新建标签页、绝不导航它。
    #
    # 判定信号：登录表单的输入框消失。登录页有 #username / #password，
    # 登录成功后 CAS 会跳转到教务，这些元素自然消失。
    # 之前用"另开一个隐藏标签页去探测"的做法会多出一个 about:blank 标签，
    # 还会抢到前台，干扰用户登录——已废弃。
    deadline = time.monotonic() + login_timeout
    ok = False
    tick = 0
    while time.monotonic() < deadline:
        time.sleep(3)
        tick += 1
        try:
            if page.query_selector("#username") is None:
                # 再确认一次，避免页面切换瞬间误判
                time.sleep(1.5)
                if page.query_selector("#username") is None:
                    ok = True
                    break
            if tick % 20 == 0:
                _log_line("仍在等待登录（已 %d 秒）；URL=%s" % (tick * 3, _safe_url(page)))
        except Exception as exc:  # noqa: BLE001
            _log_line("浏览器已被关闭（检测于第 %d 轮，约 %d 秒）：%s"
                      % (tick, tick * 3, type(exc).__name__))
            raise SystemExit("浏览器窗口被关闭，已停止。")

    if not ok:
        _log_line("等待登录超时")
        raise SystemExit("等待登录超时（%d 分钟）。可加 --login-timeout 延长。" % max(1, login_timeout // 60))
    log("登录成功。")
    _log_line("登录成功")
    return ctx, login_page



def manual_login(su: str) -> dict:
    """用**原生**浏览器让用户登录，登录完成后自动继续（不需要按任何键）。

    为什么用原生浏览器：Playwright 启动的受控窗口带自动化参数，在某些机器上会
    干扰输入法/密码输入（现象是"密码输不完"）。这里用普通方式启动 Edge，
    登录体验与日常浏览器完全一致；若浏览器里已有有效登录态，点一下就直接进去。

    自动继续的机制：程序持续检测那个浏览器窗口是否已关闭。用户登录完成后关闭
    窗口，程序立刻接管并开始读取数据，**无需按回车、无需点任何东西**。

    不接触密码：程序只负责打开页面和等待。
    """
    import subprocess as _sp

    profile = PROFILE_DIR
    profile.mkdir(parents=True, exist_ok=True)
    # 直接打开统一身份认证登录页，让用户在这里输入学号与密码
    url = PAGE_CAS_LOGIN

    # 不再每次清空登录态。
    # 原先每次运行都清空，是为了"确保停在登录页"；但代价是每次都必须重新登录，
    # 而且同一台电脑换人使用时容易混淆。现在改为**保留会话**：
    #   - 已登录 → 直接复用，不打扰用户
    #   - 未登录 → 停在登录页让用户登录
    # 想强制重新登录时，用 --relogin。
    stale = _pids_with_profile(profile.name)
    if stale:
        _log_line("清理 %d 个残留浏览器进程" % len(stale))
        for pid in stale:
            _sp.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        time.sleep(2)
    profile.mkdir(parents=True, exist_ok=True)

    edge_candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    exe = next((p for p in edge_candidates if pathlib.Path(p).exists()), None)
    if exe is None:
        log("找不到 Edge，无法使用浏览器登录模式。")
        raise SystemExit(2)

    # 清掉残留进程，否则新窗口会"交接"给旧实例、看起来像没打开
    stale = _pids_with_profile(profile.name)
    if stale:
        _log_line("清理 %d 个残留浏览器进程" % len(stale))
        for pid in stale:
            _sp.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        time.sleep(2)

    log("")
    log("=" * 66)
    log("已在浏览器中打开浙大统一身份认证登录页。")
    log("")
    log("请在这个窗口里输入你的学号与密码完成登录。")
    log("密码只输入给浙江大学的页面，本程序不读取、不保存、不上传。")
    log("")
    log("【登录完成后，关闭那个浏览器窗口】程序会自动继续，无需按任何键。")
    log("=" * 66)

    argv = [
        exe,
        "--user-data-dir=%s" % profile,
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ]
    _log_line("启动原生 Edge，profile=%s" % profile)
    _sp.Popen(argv, close_fds=True)   # 不捕获 stdio，避免沙箱下管道被拒

    # 等待登录完成的信号：Edge 在 cookie 变化时会写 Cookies 数据库文件。
    # 监视它的修改时间即可判断"登录已发生"，**不需要用户关闭窗口**。
    cookie_file = profile / "Default" / "Network" / "Cookies"
    baseline = _mtime(cookie_file)
    _log_line("Cookies 基线时间=%s" % baseline)

    log("")
    log("等待你登录（浏览器窗口保持打开即可，不用关闭）...")
    deadline = time.monotonic() + 900
    waited = 0
    while time.monotonic() < deadline:
        time.sleep(3)
        waited += 3

        changed = _mtime(cookie_file) != baseline
        closed = not _pids_with_profile(profile.name)

        if changed or closed:
            time.sleep(3)          # 给 Edge 一点时间写完
            reason = "检测到登录信息已写入" if changed else "检测到浏览器已关闭"
            _log_line("%s（等待 %d 秒），继续" % (reason, waited))
            log("%s，继续。" % reason)
            # 把这份已经登录的配置复制给读取用的独立目录。
            # 必须复制：cookie 数据库在浏览器运行时被独占锁定，无法直接读取；
            # 而 Playwright 又不能复用这个目录（Edge 会答"你已在此会话中打开"）。
            _clone_profile_for_read(profile)
            return {}

        if waited % 60 == 0:
            _log_line("仍在等待登录（已 %d 秒）" % waited)
            log("  等待中（已 %d 秒）... 登录完成后程序会自动继续" % waited)

    _log_line("等待登录超时（%d 秒）" % waited)
    log("等待超时。将尝试直接继续（若已登录，这一步也能成功）。")
    return {}


def _mtime(path: pathlib.Path):
    """文件修改时间；不存在返回 None。"""
    try:
        return path.stat().st_mtime
    except Exception:  # noqa: BLE001
        return None


def _clone_profile_for_read(src: pathlib.Path, retries: int = 4) -> bool:
    """把已登录的配置目录复制给读取用目录（READ_PROFILE_DIR）。

    cookie 数据库在浏览器运行时被独占锁定，无法直接读取；而 Playwright 也不能
    复用同一个目录（Edge 会拒绝："你已在此会话中打开"）。所以复制一份。
    浏览器可能仍在写文件，失败就重试；实在不行就用现有内容。
    """
    import shutil

    dst = READ_PROFILE_DIR
    for attempt in range(1, retries + 1):
        try:
            if dst.exists():
                shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
                "*.lock", "LOCK", "Singleton*", "*.tmp", "Crashpad*", "ShaderCache",
                "GrShaderCache", "GPUCache", "Code Cache"))
            _log_line("已复制登录配置到读取目录（第 %d 次尝试成功）" % attempt)
            log("已接管登录状态。")
            return True
        except Exception as exc:  # noqa: BLE001
            _log_line("复制登录配置失败（第 %d 次）：%r" % (attempt, exc))
            time.sleep(2)
    _log_line("复制登录配置最终失败，改用原目录尝试")
    return False


def _pids_with_profile(profile_name: str) -> list[int]:
    """命令行里引用了指定 profile 目录的 msedge 进程号。"""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
          "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        ).stdout or ""
    except Exception:  # noqa: BLE001
        return []
    pids: list[int] = []
    for line in out.splitlines():
        if profile_name not in line:
            continue
        head = line.split("\t", 1)[0].strip()
        if head.isdigit():
            pids.append(int(head))
    return pids


def fetch_identity(page, su_hint: str = "") -> dict:
    """读取**当前登录者**的身份（学号 / 姓名 / 专业 / 年级 / 学院 / 班级）。

    这是支持"多人共用同一台电脑、各自登录"的关键：不再把学号写死在配置里，
    而是登录后从会话里读出当前是谁。

    依次尝试：
      1) zjuam 的 userinfo 接口（登录后返回 JSON）
      2) 教务学籍页（从页面标签取字段）
      3) zjuam 域下的 Cookie（JSESSIONID 之外可能带用户名）
    返回归一化后的字段 dict（取不到的为空字符串）。
    """
    fields: dict[str, str] = {k: "" for k in PROFILE_ALIASES}

    # 途径 1：zjuam userinfo
    for url in ("https://zjuam.zju.edu.cn/cas/v2/userinfo",
                "https://zjuam.zju.edu.cn/cas/userinfo"):
        try:
            data = page.evaluate(
                """
                async (url) => {
                  try {
                    const r = await fetch(url, {credentials:'include',
                      headers:{'Accept':'application/json'}});
                    const t = await r.text();
                    let j = null; try { j = JSON.parse(t); } catch(e) {}
                    return {status:r.status, json:j, head:t.slice(0,200)};
                  } catch(e) { return {status:0, json:null, head:String(e)}; }
                }
                """,
                url,
            )
            if isinstance(data, dict) and isinstance(data.get("json"), dict):
                for k, v in normalize_profile(data["json"]).items():
                    if v and not fields.get(k):
                        fields[k] = v
                _log_line("userinfo 命中：%s" % url)
        except Exception:  # noqa: BLE001
            pass

    # 途径 2：教务学籍页
    if not fields.get("student_id"):
        try:
            page.goto(PAGE_PROFILE.format(su=su_hint or ""),
                      wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3500)
            got = guess_from_html(page.content())
            for k, v in got.items():
                if v and not fields.get(k):
                    fields[k] = v
            _log_line("学籍页身份字段：%s" % json.dumps(got, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            _log_line("学籍页读取身份失败：%r" % exc)

    # 途径 3：Cookie 里的用户名
    if not fields.get("student_id"):
        try:
            cookies = page.context.cookies()
            for c in cookies:
                name = (c.get("name") or "").lower()
                val = c.get("value") or ""
                if name in ("username", "userid", "uid", "account") and val:
                    import re as _re
                    m = _re.search(r"\d{6,15}", val)
                    if m:
                        fields["student_id"] = m.group(0)
                        _log_line("从 Cookie %s 取得学号" % c.get("name"))
                        break
        except Exception:  # noqa: BLE001
            pass

    return fields


def _log_line(text: str) -> None:
    """把运行轨迹写进 var/guide-run.log，便于闪退后定位。"""
    try:
        (ROOT / "var").mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%H:%M:%S")
        with (ROOT / "var" / "guide-run.log").open("a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (stamp, text))
    except Exception:  # noqa: BLE001
        pass


def _safe_url(page) -> str:
    """安全读取页面 URL（页面可能已被关闭）。"""
    try:
        return page.url
    except Exception:  # noqa: BLE001
        return "(不可读)"


def _clear_profile(profile: pathlib.Path | None = None) -> None:
    """删除浏览器配置目录。只含登录态，不含用户资料。"""
    import shutil
    target = profile or PROFILE_DIR
    for _ in range(2):
        try:
            shutil.rmtree(target, ignore_errors=False)
            return
        except Exception:  # noqa: BLE001
            time.sleep(1.5)
    try:
        shutil.rmtree(target, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 步骤 2：从教务读取个人信息（办法二核心）
# ---------------------------------------------------------------------------

def _text(value) -> str:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return " ".join(str(value).split()).split("@@@")[-1].strip()
    if isinstance(value, list) and len(value) == 1:
        return _text(value[0])
    return ""


def normalize_profile(payload) -> dict[str, str]:
    """把任意形态的教务个人信息载荷归一化成六字段。

    递归遍历 dict，按别名表匹配键名；同时支持
    {"fieldCode": "zymc", "fieldValue": "数学与应用数学"} 这种定义-值成对结构。
    """
    out: dict[str, str] = {k: "" for k in PROFILE_ALIASES}

    def walk(node) -> None:
        if isinstance(node, dict):
            lower = {str(k).lower(): v for k, v in node.items()}
            # 定义-值成对：fieldCode/fieldName + fieldValue/actualValue
            code = ""
            for key in ("fieldcode", "fieldname", "code", "name", "zdm", "zdmc"):
                if key in lower and _text(lower[key]):
                    code = str(lower[key]).lower()
                    break
            value = ""
            for key in ("fieldvalue", "actualvalue", "value", "zhi", "nr", "sjz"):
                if key in lower and _text(lower[key]):
                    value = _text(lower[key])
                    break
            if code and value:
                for canon, aliases in PROFILE_ALIASES.items():
                    if code in aliases and not out[canon]:
                        out[canon] = value
            for key, val in lower.items():
                for canon, aliases in PROFILE_ALIASES.items():
                    if key in aliases and not out[canon]:
                        text = _text(val)
                        if text:
                            out[canon] = text
            for val in node.values():
                walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return out


_PAGE_FIELD_PATTERNS = {
    # 学籍页把标签和值排在一起（"类（专业） 统计学"），用这些模式取值
    "major": (re.compile(r"类\s*[（(]\s*专业\s*[)）]\s*([^\s]{2,30})"),
              re.compile(r"专业名称\s*[:：]?\s*([^\s]{2,30})"),
              re.compile(r"专业\s*[:：]\s*([^\s]{2,30})")),
    "college": (re.compile(r"学院\s*[:：]?\s*([^\s]{2,20}学院)"),
                re.compile(r"学院\s*[:：]?\s*([^\s]{2,20})")),
    "class_name": (re.compile(r"行政班\s*[:：]?\s*([^\s]{2,30})"),
                   re.compile(r"班级\s*[:：]\s*([^\s]{2,30})")),
    "student_id": (re.compile(r"学号\s*[:：]?\s*(\d{6,15})"),),
    "name": (re.compile(r"姓名\s*[:：]?\s*([\u4e00-\u9fff]{2,6})"),),
    "year": (re.compile(r"年级\s*[:：]?\s*(20\d{2})"),
             re.compile(r"(20\d{2})\s*级"),
             re.compile(r"级\s*[:：]?\s*(20\d{2})")),
}


def guess_from_html(html: str) -> dict[str, str]:
    """从页面可见文本里抽取字段。

    学籍页是"标签 值"平铺结构，因此按标签正则取值；取不到就不填。
    年份兜底：从行政班/班级名里的两位年号推出（如"统计学2501" → 2025）。
    """
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = " ".join(text.split())
    found: dict[str, str] = {}
    for key, patterns in _PAGE_FIELD_PATTERNS.items():
        for pattern in patterns:
            m = pattern.search(text)
            if m:
                found[key] = m.group(1).strip()
                break
    if not found.get("year"):
        klass = found.get("class_name") or ""
        m = re.search(r"(?:^|[^\d])(\d{2})\d{2}$", klass)
        if m:
            yy = int(m.group(1))
            found["year"] = str(2000 + yy)
    m2 = re.search(r"(20\d{2})\s*级", text)
    if m2 and not found.get("year"):
        found["year"] = m2.group(1)
    return found


def fetch_profile(page, su: str) -> tuple[dict[str, str], dict]:
    """读取教务个人信息。返回 (归一化字段, 诊断)。

    教务页面多为 JS 渲染：初始 HTML 是空壳，字段由 XHR 填入。
    因此这里做三件事：
      1) 监听 XHR，收集返回的 JSON（最可靠）
      2) 打开个人页后**轮询等待**内容真正出现，而不是只等固定秒数
      3) 依次尝试多种读取方式，任一命中即用
    """
    diag: dict = {"sources": [], "xhr": [], "waited_ms": 0}
    fields: dict[str, str] = {k: "" for k in PROFILE_ALIASES}

    captured: list = []
    xhr_records: list = []

    def _on_response(resp):
        try:
            ctype = resp.headers.get("content-type", "") or ""
            url = resp.url
            if ("json" in ctype) and ("zdbk.zju.edu.cn" in url):
                captured.append({"url": url, "status": resp.status})
                try:
                    data = resp.json()
                except Exception:  # noqa: BLE001
                    return
                # 落盘保存（截断超大响应），用于离线确定真实字段名与接口形状
                if len(xhr_records) < 30:
                    try:
                        snapshot = json.dumps(data, ensure_ascii=False)
                        xhr_records.append({
                            "url": url.split("?")[0],
                            "status": resp.status,
                            "bytes": len(snapshot),
                            "preview": snapshot[:4000],
                        })
                    except Exception:  # noqa: BLE001
                        pass
                for k, v in normalize_profile(data).items():
                    if v and not fields.get(k):
                        fields[k] = v
        except Exception:  # noqa: BLE001
            pass

    page.on("response", _on_response)

    # 1) 个人页
    try:
        page.goto(PAGE_PROFILE.format(su=su), wait_until="domcontentloaded", timeout=60000)
        deadline = time.monotonic() + 20
        # 轮询等待：文本变长或出现可识别字段就认为渲染完成
        prev_len = 0
        while time.monotonic() < deadline:
            page.wait_for_timeout(1000)
            diag["waited_ms"] += 1000
            try:
                html = page.content()
            except Exception:  # noqa: BLE001
                break
            text = re.sub(r"<[^>]+>", " ", html)
            if not fields.get("major"):
                for k, v in guess_from_html(html).items():
                    if v and not fields.get(k):
                        fields[k] = v
            if fields.get("major") and fields.get("year"):
                break
            if len(text) == prev_len and prev_len > 0:
                break            # 内容不再增长，视为渲染完成
            prev_len = len(text)

        diag["sources"].append("个人页 HTML")
        # 表单元素：name/value 与 data-value
        try:
            embedded = page.evaluate(
                """
                () => {
                  const out = [];
                  document.querySelectorAll('input,select,textarea').forEach(el => {
                    const v = el.value || el.getAttribute('data-value') || '';
                    if (el.name && v) out.push({fieldCode: el.name, fieldValue: v});
                  });
                  document.querySelectorAll('[data-field],[data-code],[data-name]').forEach(el => {
                    const code = el.getAttribute('data-field') || el.getAttribute('data-code') || el.getAttribute('data-name');
                    const v = (el.textContent || '').trim();
                    if (code && v) out.push({fieldCode: code, fieldValue: v});
                  });
                  return out;
                }
                """
            )
            if embedded:
                diag["sources"].append("个人页表单/数据属性")
                for k, v in normalize_profile(embedded).items():
                    if v and not fields.get(k):
                        fields[k] = v
        except Exception as exc:  # noqa: BLE001
            diag["form_error"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        diag["profile_page_error"] = str(exc)

    # 成绩接口已不再使用（本工具不再读取成绩）。

    try:
        page.remove_listener("response", _on_response)
    except Exception:  # noqa: BLE001
        pass

    diag["xhr"] = captured[:30]
    diag["fields_found"] = {k: bool(v) for k, v in fields.items()}
    # 把 XHR 响应落盘：用于离线校准字段别名与接口地址（不含凭据）
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "教务接口-采样.json").write_text(
            json.dumps({"captured": captured, "records": xhr_records},
                       ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return fields, diag


def select_my_plan(plans: dict, ident: dict) -> dict:
    """按**学籍信息**从全部方案中定位"本人的培养方案"。

    现实情况比"专业+年级"复杂：
      - 培养方案**逐年修订**（明年会有新的 20XX 级版本）
      - 用户可能**转专业**，此时学籍专业会变，但入学年份不变，
        新专业未必有他入学那年的方案
    因此匹配策略是"**以专业为主、年份为辅**"，并把过程如实记录下来：

        1) 专业 + 学院 + 入学年份  → 最理想
        2) 专业 + 学院（不限年份，取入学年份及之前最近的一版）  → 转专业常见
        3) 专业（不限学院/年份）  → 院系名写法差异
        4) 专业名包含匹配        → 最后手段，建议人工确认

    返回 {"entry", "candidates", "matched_by", "notes"}。
    其中 candidates 是**该专业的所有版本**，notes 说明年份差异，
    便于上层（以及 AI 助手）判断"这一版是否适用"。
    """
    by_year = plans.get("by_year") or {}
    year = str(ident.get("year") or "").strip()
    major = (ident.get("major") or "").strip()
    college = (ident.get("college") or "").strip()
    notes: list[str] = []

    # 该专业在**所有年份**里的全部版本（按年级倒序）
    all_major: list[dict] = []
    if major:
        for y in sorted(by_year, reverse=True):
            for it in by_year[y]:
                all_major.append(it)

    def norm(s: str) -> str:
        return re.sub(r"[\s（）()·\-—]", "", s or "")

    def same_major(it: dict) -> bool:
        return bool(major) and norm(it.get("zymc")) == norm(major)

    def same_college(it: dict) -> bool:
        return (not college) or norm(it.get("xymc")) == norm(college)

    same = [it for it in all_major if same_major(it)]
    if not same:
        loose = [it for it in all_major
                 if major and (major in (it.get("zymc") or "")
                               or (it.get("zymc") or "") in major)]
        if loose:
            return {"entry": loose[0], "candidates": loose,
                    "matched_by": "专业名包含匹配——建议人工确认",
                    "notes": ["专业名与学籍记录不完全一致，可能写法不同"]}
        return {"entry": None, "candidates": [],
                "matched_by": "未匹配到",
                "notes": ["学籍专业「%s」在方案列表中找不到" % (major or "（空）")]}

    # 1) 同年份 + 同学院
    same_year = [it for it in same if str(it.get("synj") or "").strip() == year]
    exact = [it for it in same_year if same_college(it)]
    if exact:
        return {"entry": exact[0], "candidates": same,
                "matched_by": "专业+学院+入学年份（%s 级）精确匹配" % year,
                "notes": _plan_year_notes(same, year, notes)}

    # 2) 同学院，年份不限：取 <= 入学年份里最近的一版（转专业常见）
    same_col = [it for it in same if same_college(it)]
    if same_col:
        older = [it for it in same_col
                 if str(it.get("synj") or "").strip().isdigit()
                 and int(str(it.get("synj"))) <= int(year or 0)]
        pick = (older[0] if older else same_col[0])
        reason = ("专业+学院匹配；学籍入学年份 %s 无对应版本，"
                  "已取 %s 级（转专业或新专业时常见）"
                  % (year or "?", pick.get("synj"))) if older else (
                  "专业+学院匹配；该专业无 %s 级及更早版本，已取 %s 级"
                  % (year or "?", pick.get("synj")))
        return {"entry": pick, "candidates": same, "matched_by": reason,
                "notes": _plan_year_notes(same, year, notes)}

    # 3) 专业对、学院名对不上
    pick = same_year[0] if same_year else same[0]
    return {"entry": pick, "candidates": same,
            "matched_by": "专业匹配（学院名未对上，已放宽）",
            "notes": _plan_year_notes(same, year, notes + [
                "学籍学院「%s」与方案中的开课学院名称可能写法不同" % (college or "（空）")])}


def _plan_year_notes(same: list[dict], year: str, notes: list[str]) -> list[str]:
    """补充"该专业有多个年份版本"这类提示，便于判断适用性。"""
    years = sorted({str(it.get("synj") or "").strip() for it in same
                    if str(it.get("synj") or "").strip().isdigit()}, reverse=True)
    if len(years) > 1:
        notes.append("该专业存在多个年份版本：%s；已选用与学籍入学年份相符或最近的一版"
                     % "、".join("%s 级" % y for y in years))
    if year and years and year not in years:
        notes.append("注意：学籍入学年份 %s 不在该专业的版本年份中（%s），"
                     "可能因转专业或方案修订，请结合学籍确认适用版本"
                     % (year, "、".join(years)))
    return notes


def fetch_plan_pdf(page, su: str, *, major: str, enroll_year: int,
                   college: str = "") -> dict:
    """按学籍里的专业自动下载**完整培养方案 PDF**。

    教务的培养方案详情是 PDF，不是 JSON 表格：
        /jwglxt/pyfagl/pyfayl_cxPyfaylPdf.html?id=<方案ID>&doType=query&gnmkdm=N153020
    其中 id 由教学计划号 jxjhh 推导（首位 2→0：20250613 → 020250613）。

    流程：
      1) 查方案列表，用 synj=入学年份（+学院）过滤，找到 zymc 与本专业一致的条目
      2) 由 jxjhh 推导 id，下载 PDF
      3) 落盘 var/academics/培养方案-<专业>.pdf，并交给 PDF 解析器提取模块与课程

    返回 dict：list_entry / pdf_path / pdf_bytes / parse（解析结果）/ tried。
    """
    result: dict = {"major": major, "enroll_year": enroll_year, "list_entry": None,
                    "pdf_path": None, "pdf_bytes": 0, "parse": None, "error": None}

    def query(url: str, payload: dict) -> dict:
        return page.evaluate(
            """
            async ([url, payload]) => {
              const res = await fetch(url, {method:'POST', credentials:'include',
                headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8',
                         'X-Requested-With':'XMLHttpRequest'},
                body: new URLSearchParams(payload).toString()});
              const text = await res.text();
              let j = null; try { j = JSON.parse(text); } catch(e) {}
              return {status: res.status, len: text.length, json: j, head: text.slice(0, 160)};
            }
            """,
            [url, payload],
        )

    # 1) 定位"本人方案"：按学籍信息从**全部方案**里匹配（专业 + 年级 + 学院）。
    #    先取全量列表（含 synj 年级字段），再做三层匹配，而不是按单一 synj 查一次。
    try:
        plans = fetch_all_plans(page, su)
    except Exception as exc:  # noqa: BLE001
        result["error"] = "方案列表查询失败：%r" % exc
        return result

    ident = {"year": str(enroll_year or ""), "major": major, "college": college}
    picked = select_my_plan(plans, ident)
    entry = picked.get("entry")
    result["list_entry"] = entry
    result["matched_by"] = picked.get("matched_by")
    result["notes"] = picked.get("notes") or []
    result["plan_years"] = plans.get("years") or []
    result["same_major_candidates"] = picked.get("candidates") or []
    result["same_major_count"] = len(picked.get("candidates") or [])
    if not entry:
        result["error"] = "未能在方案列表中匹配到学籍专业「%s」（%s 级，%s）" % (
            major, enroll_year, college)
        return result
    _log_line("方案匹配：%s（%s）" % (entry.get("zymc"), picked.get("matched_by")))
    for n in result["notes"]:
        _log_line("方案匹配提示：%s" % n)

    jxjhh = str(entry.get("jxjhh") or "")
    # id 推导：首位 2 换成 0（观测值 20250613 → 020250613）
    candidates = []
    if jxjhh:
        if jxjhh.startswith("2"):
            candidates.append("0" + jxjhh[1:])
        candidates.append(jxjhh)
    for extra in ("pyfaxx_id", "kcxddt", "dljxjhh"):
        v = str(entry.get(extra) or "")
        if v and v not in candidates:
            candidates.append(v)
    result["ids_tried"] = candidates

    # 2) 下载 PDF
    raw = OUT_DIR / ("培养方案-%s.pdf" % re.sub(r"[\\/:*?\"<>|]", "_", major or "plan"))
    for pid in candidates:
        url = (BASE + "pyfagl/pyfayl_cxPyfaylPdf.html?id=%s&doType=query"
                       "&gnmkdm=N153020&su=%s" % (pid, su))
        try:
            data = page.evaluate(
                """
                async (url) => {
                  const res = await fetch(url, {credentials:'include'});
                  const buf = await res.arrayBuffer();
                  const bytes = new Uint8Array(buf);
                  // 判断是否为 PDF（%PDF-）
                  let isPdf = bytes.length > 4 && bytes[0]===0x25 && bytes[1]===0x50
                              && bytes[2]===0x44 && bytes[3]===0x46;
                  let bin = '';
                  const chunk = 0x8000;
                  for (let i = 0; i < bytes.length; i += chunk) {
                    bin += String.fromCharCode.apply(null, bytes.subarray(i, i+chunk));
                  }
                  return {status: res.status, len: bytes.length, isPdf: isPdf, b64: btoa(bin)};
                }
                """,
                url,
            )
        except Exception as exc:  # noqa: BLE001
            _log_line("PDF id=%s 取回异常：%r" % (pid, exc))
            continue
        if isinstance(data, dict) and data.get("isPdf"):
            import base64
            blob = base64.b64decode(data["b64"])
            raw.write_bytes(blob)
            result["pdf_path"] = str(raw)
            result["pdf_bytes"] = len(blob)
            result["pdf_id"] = pid
            _log_line("培养方案 PDF 已保存：%s（%d 字节，id=%s）" % (raw.name, len(blob), pid))
            break
        else:
            _log_line("PDF id=%s 不是 PDF（status=%s len=%s）"
                      % (pid, (data or {}).get("status"), (data or {}).get("len")))

    if not result["pdf_path"]:
        result["error"] = "未能下载 PDF（试过 id：%s）" % ", ".join(candidates)
        return result

    # 3) 到此为止：**不做解析**。
    # 培养方案 PDF 原样交给 App 里的大模型阅读（它能直接读 PDF）。
    # 这里不再调用 plan_pdf 的规则解析器——那套规则既复杂又容易出错
    # （课号被切断、水印污染、模块归类错位等），而助手读原文更可靠。
    _log_line("培养方案 PDF 下载完成：%s（%d 字节，id=%s）"
              % (raw.name, result["pdf_bytes"], result.get("pdf_id")))
    return result


def guess_from_plan_title(page, su: str) -> dict[str, str]:
    """从培养方案页标题交叉校验：'2025级数学与应用数学专业培养方案'。"""
    try:
        page.goto(PAGE_PLAN.format(su=su), wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)
        text = " ".join(re.sub(r"<[^>]+>", " ", page.content()).split())
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, str] = {}
    m = re.search(r"(20\d{2})\s*级\s*([^\s，,。]{2,30}?)\s*(?:专业)?培养方案", text)
    if m:
        out["year"] = m.group(1)
        out["major"] = m.group(2)
    return out


def resolve_identity(args, page, su: str) -> tuple[str, int]:
    """办法二：先读教务 → 与培养方案标题交叉校验 → 让用户确认。"""
    log("")
    log("正在读取教务个人信息（专业 / 年级 / 学院 / 班级）...")
    fields, diag = fetch_profile(page, su)
    for k, v in fields.items():
        if v:
            log("   %-12s %s" % (k, v))
    if not any(fields.values()):
        log("   未能从教务读到可识别字段，将改用培养方案页标题与人工确认。")

    title_info = guess_from_plan_title(page, su)
    if title_info:
        log("   培养方案标题显示：%s级 %s" % (title_info.get("year", "?"),
                                              title_info.get("major", "?")))

    # 合并：个人页优先，标题兜底；冲突时以标题为准并提示（标题直接来自你要用的方案）
    major = fields.get("major") or title_info.get("major") or ""
    year_raw = fields.get("year") or title_info.get("year") or ""
    if fields.get("major") and title_info.get("major") and fields["major"] != title_info["major"]:
        log("")
        log("   注意：个人信息中的专业(%s)与培养方案标题(%s)不一致。"
            % (fields["major"], title_info["major"]))
        log("   培养方案才是学分要求的依据，默认采用标题中的专业。")
        major = title_info["major"]
    if year_raw and not str(year_raw).isdigit():
        m = re.search(r"(20\d{2})", str(year_raw))
        year_raw = m.group(1) if m else ""
    enroll_year = int(year_raw) if str(year_raw).isdigit() and len(str(year_raw)) == 4 else 0
    if enroll_year and enroll_year < 100:
        enroll_year = 2000 + enroll_year

    (OUT_DIR).mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "个人信息-诊断.json").write_text(
        json.dumps({"fields": fields, "plan_title": title_info, "diagnostics": diag},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    # 已有命令行参数则不再询问
    if args.major and args.enroll_year:
        return args.major, int(args.enroll_year)

    # 从学籍读到就直接用：不再让用户确认（避免被 input() 阻塞）
    if major and enroll_year:
        log("")
        log("已从学籍确定：专业=%s，入学年份=%d（无需确认，直接继续）" % (major, enroll_year))
        return major, int(enroll_year)

    log("")
    log("-" * 66)
    log("请补充信息（直接回车采用默认值，或输入新值）：")
    try:
        raw = input("  专业 [%s]： " % (major or "未知")).strip()
        major = raw or major
    except EOFError:
        pass
    try:
        raw = input("  入学年份 [%s]： " % (enroll_year or "未知")).strip()
        if raw.isdigit():
            enroll_year = int(raw)
    except EOFError:
        pass

    if not major or not enroll_year:
        log("   提醒：专业或入学年份缺失，学分要求与年份一致性检查会受限。")
    return major, int(enroll_year or 0)


# ---------------------------------------------------------------------------
# 步骤 3：抓取
# ---------------------------------------------------------------------------

def fetch_page(page, url: str, *, wait: int = 2500) -> str | None:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(wait)
        html = page.content()
    except Exception as exc:  # noqa: BLE001
        log("    失败：%s" % exc)
        return None
    if looks_logged_out(html, page.url):
        log("    登录态失效，跳过。")
        return None
    return html


def fetch_api_json(page, url: str, payload: dict | None = None):
    script = """
    async ([url, payload]) => {
      const res = await fetch(url, {method: 'POST', credentials: 'include',
        headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                  'X-Requested-With': 'XMLHttpRequest'},
        body: new URLSearchParams(payload).toString()});
      const text = await res.text();
      let json = null;
      try { json = JSON.parse(text); } catch (e) { json = null; }
      return {status: res.status, len: text.length, json: json, head: text.slice(0, 300)};
    }
    """
    try:
        return page.evaluate(script, [url, payload or {}])
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def fetch_plan_detail(page, su: str, *, major: str, enroll_year: int,
                      college: str = "") -> dict:
    """按学籍里的专业/年级自动查培养方案明细（零手工参数）。

    流程：
      1) 调 API_PLAN_LIST，用 synj=入学年份 过滤（可再按学院代码收窄）
      2) 在结果里找 zymc 与学籍专业完全一致的那条
      3) 从该条取出 jxjhh(教学计划号) / zydm(专业代码) / xydm(学院代码)
      4) 用这些参数再查明细接口，取模块学分与课程清单

    返回 dict，含 list_entry / detail / tried（各明细候选的响应摘要），
    全部落盘，便于后续离线校准。
    """
    result: dict = {"major": major, "enroll_year": enroll_year,
                    "list_entry": None, "detail": [], "detail_fields": [],
                    "tried": [], "error": None}
    quoted = json.dumps

    def query(url: str, payload: dict) -> dict:
        return page.evaluate(
            """
            async ([url, payload]) => {
              const res = await fetch(url, {method:'POST', credentials:'include',
                headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8',
                         'X-Requested-With':'XMLHttpRequest'},
                body: new URLSearchParams(payload).toString()});
              const text = await res.text();
              let j = null; try { j = JSON.parse(text); } catch(e) {}
              return {status: res.status, len: text.length, json: j, head: text.slice(0, 200)};
            }
            """,
            [url, payload],
        )

    # 1) 找本人在方案列表里的那一条
    list_payload = {
        "synj": str(enroll_year), "xydm": "", "zydm": "", "dlbs": "", "jxjhh": "",
        "_search": "false", "nd": "1",
        "queryModel.showCount": "200", "queryModel.currentPage": "1",
        "queryModel.sortName": "synj desc, xymc, jxjhh",
        "queryModel.sortOrder": "asc", "time": "0",
    }
    try:
        r = query(API_PLAN_LIST.format(su=su), list_payload)
        items = (r.get("json") or {}).get("items") or []
        entry = None
        for it in items:
            if (it.get("zymc") or "").strip() == (major or "").strip():
                if college and (it.get("xymc") or "").strip() not in ("", college):
                    continue
                entry = it
                break
        if entry is None and items:
            # 名称不完全一致时，退化为包含匹配，并在结果里标注
            for it in items:
                name = (it.get("zymc") or "").strip()
                if major and (major in name or name in major):
                    entry = it
                    break
        result["list_entry"] = entry
        result["list_total"] = len(items)
    except Exception as exc:  # noqa: BLE001
        result["error"] = "列表查询失败：%r" % exc
        return result

    if not entry:
        result["error"] = "方案列表中未找到专业「%s」（共 %d 条）" % (major, result.get("list_total", 0))
        return result

    # 2) 用条目里的标识查明细（逐个候选试，记录每次响应）
    ident = {
        "jxjhh": entry.get("jxjhh") or "",
        "zydm": entry.get("zydm") or "",
        "xydm": entry.get("xydm") or "",
        "pyfaxx_id": entry.get("pyfaxx_id") or "",
        "njdm_id": str(enroll_year),
    }
    result["identifiers"] = ident

    candidates = [
        ("课程明细", BASE + "pyfagl/pyfaxxcx_cxPyfaxxKcxx.html?doType=query&gnmkdm=N153020&su=" + su),
        ("模块明细", BASE + "pyfagl/pyfaxxcx_cxPyfaxxMkxx.html?doType=query&gnmkdm=N153020&su=" + su),
        ("计划明细", BASE + "pyfagl/pyfaxxcx_cxPyfaxxJhxx.html?doType=query&gnmkdm=N153020&su=" + su),
        ("课程(简)", BASE + "pyfagl/pyfaxxcx_cxPyfaxxKc.html?doType=query&gnmkdm=N153020&su=" + su),
    ]
    for label, url in candidates:
        payload = dict(ident)
        payload.update({"_search": "false", "nd": "1",
                        "queryModel.showCount": "500",
                        "queryModel.currentPage": "1", "time": "0"})
        try:
            rr = query(url, payload)
        except Exception as exc:  # noqa: BLE001
            result["tried"].append({"label": label, "error": repr(exc)})
            continue
        got = rr.get("json") or {}
        rows = got.get("items") or got.get("rows") or []
        result["tried"].append({
            "label": label, "status": rr.get("status"), "bytes": rr.get("len"),
            "rows": len(rows),
            "keys": list(rows[0].keys())[:12] if rows else list(got.keys())[:12],
        })
        if rows:
            result["detail"] = rows
            result["detail_fields"] = list(rows[0].keys())
            result["detail_source"] = label
            break
    return result


def probe_select_api(page, su: str) -> dict:
    """探测选课目录的数据接口（当前未知，记录候选供后续定死）。"""
    seen: list[dict] = []
    diag: dict = {"candidates": [], "note": "选课目录接口待校准"}

    def on_response(resp):
        try:
            ctype = resp.headers.get("content-type", "") or ""
            if "json" in ctype or "list" in resp.url or "cx" in resp.url:
                seen.append({"url": resp.url, "status": resp.status, "type": ctype})
        except Exception:  # noqa: BLE001
            pass

    try:
        page.on("response", on_response)
    except Exception:  # noqa: BLE001
        pass

    try:
        page.goto(PAGE_SELECT.format(su=su), wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
    except Exception as exc:  # noqa: BLE001
        diag["error"] = str(exc)
        return diag

    if looks_logged_out(page.content(), page.url):
        diag["error"] = "未登录"
        return diag

    diag["candidates"] = [s for s in seen if "zdbk.zju.edu.cn" in s["url"]][:30]
    text = " ".join(re.sub(r"<[^>]+>", " ", page.content()).split())
    diag["page_hint"] = text[:200]
    return diag


def fetch_plan_pdf_batch(page, su: str, majors: list[dict], *, limit: int = 0) -> dict:
    """批量探测/下载多个专业的培养方案 PDF。

    majors: [{major, college, jxjhh, zymc, synj}, ...]
    返回 {ok: [...], failed: [...]}，只提取模块要求与课程数（控体积），
    完整数据仍按专业分别落盘。

    用途：验证"其他专业的方案是否也能抓"。
    """
    report: dict = {"ok": [], "failed": [], "total": len(majors)}

    for idx, m in enumerate(majors, 1):
        if limit and idx > limit:
            break
        major = m.get("major") or m.get("zymc") or ""
        jxjhh = str(m.get("jxjhh") or "")
        if not jxjhh:
            report["failed"].append({"major": major, "reason": "无 jxjhh"})
            continue

        # id 推导：首位数字变 0（20250613 → 020250613）
        candidates = []
        if jxjhh.startswith("2"):
            candidates.append("0" + jxjhh[1:])
        candidates.append(jxjhh)

        got = None
        for pid in candidates:
            url = (BASE + "pyfagl/pyfayl_cxPyfaylPdf.html?id=%s&doType=query"
                           "&gnmkdm=N153020&su=%s" % (pid, su))
            try:
                data = page.evaluate(
                    """
                    async (url) => {
                      const res = await fetch(url, {credentials:'include'});
                      const buf = await res.arrayBuffer();
                      const bytes = new Uint8Array(buf);
                      const isPdf = bytes.length > 4 && bytes[0]===0x25 && bytes[1]===0x50
                                    && bytes[2]===0x44 && bytes[3]===0x46;
                      return {status: res.status, len: bytes.length, isPdf: isPdf};
                    }
                    """,
                    url,
                )
            except Exception as exc:  # noqa: BLE001
                got = {"error": repr(exc)}
                continue
            if isinstance(data, dict) and data.get("isPdf"):
                got = {"id": pid, "bytes": data["len"]}
                break
            got = data or {}

        if got and got.get("id"):
            report["ok"].append({"major": major, "college": m.get("college") or m.get("xymc"),
                                 "jxjhh": jxjhh, "id": got["id"], "bytes": got["bytes"]})
            _log_line("批量方案 OK：%s（%s，%d 字节）" % (major, got["id"], got["bytes"]))
            log("    [%d/%d] %-24s OK  (%d KB)"
                % (idx, len(majors), major, (got["bytes"] or 0) // 1024))
        else:
            reason = "非 PDF/无权限 (status=%s)" % (got or {}).get("status")
            report["failed"].append({"major": major, "jxjhh": jxjhh, "reason": reason})
            _log_line("批量方案 失败：%s（%s）" % (major, reason))
            log("    [%d/%d] %-24s 失败：%s" % (idx, len(majors), major, reason))

    return report


def write_agent_package(su: str, ident: dict, major: str, enroll_year: int,
                        collected: dict) -> pathlib.Path:
    """把抓到的材料整理成"给 AI 助手直接读"的目录。

    设计前提：宿主 App 已接入大模型（DeepSeek v4.1 等），能直接读 PDF/JSON/HTML。
    因此本程序**不做解析、不算学分**——只负责把原始材料抓下来、摆好，
    并写一份 manifest.json 说明每个文件的来源与用途，让助手知道能拿它们做什么。

    产出目录：
        var/agent-input/
            README.txt            给人看的使用说明
            manifest.json         材料清单（来源/时间/用途）
            prompt.md             建议交给助手的任务提示词
            01-学籍信息.json        当前登录者的身份
            02-培养方案.pdf         本人的完整培养方案（核心材料）
            02-培养方案列表.json     全校各专业的方案概要（可跨专业比较）
            03-课表.json            本学期课表
            04-选课-课程列表.json    选课系统里的课程池（若可得）
    """
    pkg = OUT_DIR.parent / "agent-input"
    pkg.mkdir(parents=True, exist_ok=True)

    # 清掉上一次的旧材料，避免换人后混入上一个用户的数据
    for old in pkg.iterdir():
        if old.is_file():
            try:
                old.unlink()
            except Exception:  # noqa: BLE001
                pass

    files: list[dict] = []

    def put(src: pathlib.Path | None, name: str, desc: str, kind: str) -> None:
        if not src or not src.exists():
            return
        dst = pkg / name
        try:
            dst.write_bytes(src.read_bytes())
        except Exception as exc:  # noqa: BLE001
            _log_line("复制材料失败 %s：%r" % (name, exc))
            return
        files.append({
            "file": name,
            "type": kind,
            "bytes": dst.stat().st_size,
            "description": desc,
            "source": str(src),
        })

    # 学籍信息（写成自己的一份，便于助手直接读）
    info_path = OUT_DIR / "学籍信息.json"
    if ident:
        info_path.write_text(json.dumps(ident, ensure_ascii=False, indent=2), encoding="utf-8")
    put(info_path, "01-学籍信息.json",
        "当前登录者的身份：学号、姓名、专业、年级、学院、班级", "json")

    # 培养方案 PDF（核心材料，助手可直接读）
    plan_pdf = None
    if major:
        cand = OUT_DIR / ("培养方案-%s.pdf" % re.sub(r"[\\/:*?\"<>|]", "_", major))
        if cand.exists():
            plan_pdf = cand
    if plan_pdf is None:
        for p in sorted(OUT_DIR.glob("培养方案-*.pdf")):
            plan_pdf = p
            break
    put(plan_pdf, "02-培养方案.pdf",
        "本人专业的完整培养方案（教务导出 PDF，含模块学分要求与课程清单）", "pdf")

    put(OUT_DIR / "培养方案-全部.json", "02-培养方案列表-全部专业.json",
        "全校各专业 × 各年级的培养方案列表（含专业名、学院、最低毕业学分、"
        "核心课程名、教学计划号等，可据此跨专业比较）", "json")

    put(OUT_DIR / "课表.json", "03-课表.json",
        "本人本学期的课表数据（教学班、时间、地点）", "json")

    put(OUT_DIR / "选课-全部课程.json", "04-选课-全部课程.json",
        "选课系统的全部课程（已遍历所有分组并去重）：课程号、课程名、学分、"
        "类别、开课学院等。注意 selections 里含各分组条数统计", "json")
    put(OUT_DIR / "选课-课程列表.json", "04-选课-课程池.json",
        "选课系统单次查询返回的课程池（作为上一条的补充）", "json")
    put(OUT_DIR / "选课页-可见文本.txt", "04-选课页-可见文本.txt",
        "选课页渲染后的可见文本（含教学班详情：教师、上课时间、地点、余量/容量）", "txt")

    # 方案匹配信息：优先用本次采集的结果；--no-fetch 重新打包时从落盘文件回读
    detail = collected.get("培养方案明细") or {}
    if not detail:
        f = OUT_DIR / "培养方案明细.json"
        if f.exists():
            try:
                detail = json.loads(f.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                detail = {}
    entry = detail.get("list_entry") or {}
    plan_meta = {
        "matched_by": detail.get("matched_by"),
        "notes": detail.get("notes") or [],
        "plan_year": entry.get("synj"),
        "major_on_plan": entry.get("zymc"),
        "college_on_plan": entry.get("xymc"),
        "major_code": entry.get("zydm"),
        "jxjhh": entry.get("jxjhh"),
        "total_required_credits": entry.get("zdbyxf"),
        "pdf_id": detail.get("pdf_id"),
        "same_major_versions": [
            {"year": it.get("synj"), "jxjhh": it.get("jxjhh"),
             "xymc": it.get("xymc"), "zdbyxf": it.get("zdbyxf")}
            for it in (detail.get("same_major_candidates") or [])[:10]
        ],
    }

    manifest = {
        "_comment": "本目录是给 AI 助手的输入材料。程序只负责抓取与整理，不解析、不下结论。",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "freshness": {
            "note": "每次登录都会清空旧材料并完整重抓——课表与选课目录每学期变化，"
                    "培养方案逐年修订，因此不要沿用历史结果。",
            "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "identity": {
            "student_id": su,
            "name": ident.get("name"),
            "major": major,
            "enroll_year": enroll_year,
            "college": ident.get("college"),
            "class_name": ident.get("class_name"),
        },
        "matched_plan": plan_meta,
        "files": files,
        "suggested_tasks": [
            "阅读 02-培养方案.pdf，整理出本专业的模块学分要求与全部课程清单",
            "对照 01-学籍信息.json 的年级，说明培养方案的适用性",
            "说明各模块还需修读的学分构成（方案要求层面，不含个人已修数据）",
            "从 04-选课-课程列表.json 中筛选出属于培养方案内、建议下学期修读的课程",
            "指出课程的先修关系与可能的学期冲突",
        ],
        "limits": [
            "本程序不读取成绩，因此无法计算个人已修学分与剩余学分",
            "培养方案 PDF 由教务导出，可能与网页版存在版本差异",
            "选课课程池按选课轮次分批开放，未必包含全部课程",
        ],
    }
    (pkg / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    prompt = "\n".join([
        "# 任务背景",
        "",
        "下表中的材料来自浙江大学教务系统，由本程序在用户本人登录后抓取。",
        "用户是 **%s**（学号 %s，%s 级，%s）。" % (
            ident.get("name") or "（姓名未知）", su, enroll_year or "?", major or "（专业未知）"),
        "",
        "# 可用材料",
        "",
    ] + ["- `%s`：%s" % (f["file"], f["description"]) for f in files] + [
        "",
        "# 请完成",
        "",
        "1. 读 `02-培养方案.pdf`，抽出**全部**模块学分要求与课程清单"
        "（课程号、课程名、学分、建议学期、必修/选修）。",
        "2. 按模块归类列出课程，并给出每个模块的学分合计。",
        "3. **确认方案适用性**：`manifest.json` 的 `matched_plan` 记录了这份方案是按"
        "什么规则匹配出来的、该专业还有哪些年份的版本。请核对它是否与用户学籍"
        "（专业 / 入学年份 / 学院）相符；若不一致，说明可能原因（转专业、方案修订、"
        "同名专业）并提示人工确认。",
        "4. 结合 `04-选课-全部课程.json`，指出培养方案内、适合下一学期修读的课程，"
        "并说明理由（先修关系、建议学期）。",
        "5. 明确标注材料中**没有**的信息（例如个人已修成绩、绩点）。",
        "",
        "# 关于时效性",
        "",
        "本目录材料是**本次登录时重新抓取**的（见 manifest.json 的 `freshness`）。",
        "课表与选课目录每学期都会变化；培养方案逐年修订，同一专业可能存在多个年份"
        "的版本。分析时请以 `matched_plan` 的匹配结果与提示为准。",
        "",
        "# 输出要求",
        "",
        "- 所有数字都要能追溯到具体文件；找不到依据就写“材料中未提供”。",
        "- 不要把推测写成事实。",
    ])
    (pkg / "prompt.md").write_text(prompt, encoding="utf-8")

    readme = "\n".join([
        "这个目录是给 AI 助手的输入材料。",
        "",
        "程序只做一件事：在你本人登录后，把教务系统里的材料原样抓下来、摆整齐。",
        "它不解析 PDF、不算学分、不下结论——这些都交给 App 里的大模型完成。",
        "",
        "顺序建议：",
        "  1. manifest.json   先看这个，了解每份文件是什么、能做什么",
        "  2. prompt.md       建议交给助手的任务描述",
        "  3. 其余文件         具体材料",
        "",
        "说明：",
        "  - 本程序不读取成绩，因此无法计算个人已修/剩余学分",
        "  - 培养方案 PDF 由教务导出，可能与网页版存在版本差异",
        "  - 换人登录后重新运行，本目录会被整体替换（不会残留上一个用户的数据）",
    ])
    (pkg / "README.txt").write_text(readme, encoding="utf-8")

    _log_line("已生成助手材料包：%s（%d 个文件）" % (pkg, len(files)))
    return pkg


def fetch_select_courses(page, su: str) -> dict:
    """抓取选课系统里的**全部**课程（遍历所有分组 + 翻页）。

    选课页把课程按分组（dl 参数）划分，且每组的取数接口是：
        POST /xsxk/zzxkghb_cxZzxkGhbKcList.html
    重要参数：dl(分组) lx(类型) nj(年级) xn(学年) xq(学期) zydm(专业)
             kspage/jspage(起止记录序号)

    做法：
      1) 打开选课页，从 DOM 里读出真实的搜索上下文（学年/学期/年级/专业）
         以及页面上声明的全部分组（data-dl）
      2) 对每个分组翻页拉取，直到取满或没有新数据
      3) 按课程号去重后落盘

    只读：仅调用查询接口，不提交任何选课操作。
    """
    result: dict = {"context": {}, "groups": [], "total_unique": 0, "courses": [],
                    "per_group": {}, "errors": []}

    # 1) 读取页面上下文与分组
    try:
        page.goto(PAGE_SELECT.format(su=su), wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(6000)
    except Exception as exc:  # noqa: BLE001
        result["errors"].append("打开选课页失败：%r" % exc)
        return result

    if looks_logged_out(page.content(), page.url):
        result["errors"].append("选课页要求登录（会话已失效）")
        return result

    try:
        ctx_info = page.evaluate(
            """
            () => {
              const v = (s) => { const e = document.querySelector(s); return e ? (e.value||'') : ''; };
              const groups = [];
              document.querySelectorAll('[data-dl]').forEach(e => {
                groups.push({dl: e.getAttribute('data-dl') || '',
                             lx: e.getAttribute('data-lx') || '',
                             label: (e.innerText||'').trim().slice(0,24)});
              });
              return {xn: v('#xn'), xq: v('#xq'), nj: v('#nj'), zydm: v('#zydm'),
                      jxjhh: v('#jxjhh'), dl: v('#current_dl'), lx: v('#current_lx'),
                      groups: groups};
            }
            """
        )
    except Exception as exc:  # noqa: BLE001
        result["errors"].append("读取选课页上下文失败：%r" % exc)
        return result

    xn = ctx_info.get("xn") or ""
    xq = ctx_info.get("xq") or ""
    nj = ctx_info.get("nj") or ""
    zydm = ctx_info.get("zydm") or ""
    jxjhh = ctx_info.get("jxjhh") or ""
    result["context"] = {"xn": xn, "xq": xq, "nj": nj, "zydm": zydm, "jxjhh": jxjhh}

    groups = ctx_info.get("groups") or []
    # 去重并按 dl 排序，保证结果稳定
    uniq: dict[tuple, dict] = {}
    for g in groups:
        key = (g.get("dl") or "", g.get("lx") or "")
        if key[0] and key not in uniq:
            uniq[key] = g
    groups = list(uniq.values())
    result["groups"] = [{"dl": g["dl"], "lx": g["lx"], "label": g.get("label")}
                        for g in groups]

    JS_POST = """
    async ([url, payload]) => {
      try {
        const r = await fetch(url, {method:'POST', credentials:'include',
          headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8',
                   'X-Requested-With':'XMLHttpRequest'},
          body: new URLSearchParams(payload).toString()});
        const t = await r.text();
        let j = null; try { j = JSON.parse(t); } catch(e) {}
        return {status:r.status, len:t.length, json:j};
      } catch(e) { return {status:0, json:null, err:String(e)}; }
    }
    """
    URL = BASE + "xsxk/zzxkghb_cxZzxkGhbKcList.html"
    page_size = 100
    all_courses: dict[str, dict] = {}

    for g in groups:
        dl, lx = g["dl"], g["lx"]
        got_rows: list[dict] = []
        start = 1
        for _round in range(12):                     # 最多 12 页，防死循环
            payload = {
                "dl": dl, "lx": lx, "xkmc": "", "kcbs": "",
                "nj": nj, "xn": xn, "xq": xq, "zydm": zydm, "jxjhh": jxjhh,
                "xnxq": "(%s-%s)-" % (xn, xq),
                "kspage": start, "jspage": start + page_size - 1,
            }
            try:
                res = page.evaluate(JS_POST, [URL, payload])
            except Exception as exc:  # noqa: BLE001
                result["errors"].append("分组 %s 请求失败：%r" % (dl, exc))
                break
            data = (res or {}).get("json")
            rows = data if isinstance(data, list) else ((data or {}).get("items") or [])
            if not rows:
                break
            got_rows.extend(rows)
            for r in rows:
                code = str(r.get("kcdm") or "").strip()
                if code and code not in all_courses:
                    all_courses[code] = r
            if len(rows) < page_size:
                break
            start += page_size

        result["per_group"][dl] = len(got_rows)
        log("    分组 %-12s %-14s %d 条" % (dl, g.get("label") or "", len(got_rows)))

    result["courses"] = list(all_courses.values())
    result["total_unique"] = len(all_courses)
    _log_line("选课课程抓取：%d 个分组，去重后 %d 门课程"
              % (len(groups), result["total_unique"]))
    return result


def fetch_all_plans(page, su: str, years: tuple[str, ...] | None = None) -> dict:
    """抓取**所有专业 × 所有年级**的培养方案列表（翻页取全）。

    年级范围**不写死**：先探测当前有哪些年级（用一份空 synj 的查询拿到年份清单），
    再逐年翻页取全。这样即使将来出现 2027 级方案，也能自动抓到。
    """
    JS_POST = """
    async ([url, payload]) => {
      try {
        const r = await fetch(url, {method:'POST', credentials:'include',
          headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8',
                   'X-Requested-With':'XMLHttpRequest'},
          body: new URLSearchParams(payload).toString()});
        const t = await r.text();
        let j = null; try { j = JSON.parse(t); } catch(e) {}
        return {status:r.status, json:j};
      } catch(e) { return {status:0, json:null}; }
    }
    """
    out: dict = {"by_year": {}, "total": 0, "years": [], "errors": []}
    page_size = 100

    def query(year: str, pageno: int) -> dict:
        payload = {
            "synj": year, "xydm": "", "zydm": "", "dlbs": "", "jxjhh": "",
            "_search": "false", "nd": str(pageno),
            "queryModel.showCount": str(page_size),
            "queryModel.currentPage": str(pageno),
            "queryModel.sortName": "synj desc, xymc, jxjhh",
            "queryModel.sortOrder": "asc", "time": "0",
        }
        res = page.evaluate(JS_POST, [API_PLAN_LIST.format(su=su), payload])
        return (res or {}).get("json") or {}

    # 1) 探测有哪些年级：不传 synj 取一批，从结果里收集 synj 取值
    if not years:
        found: set[str] = set()
        try:
            data = query("", 1)
            for it in (data.get("items") or []):
                y = str(it.get("synj") or "").strip()
                if y.isdigit() and len(y) == 4:
                    found.add(y)
            # 再按最新年份探一次，防止首页只覆盖部分年份
            if found:
                newest = max(found)
                data2 = query(str(int(newest) + 1), 1)
                for it in (data2.get("items") or []):
                    y = str(it.get("synj") or "").strip()
                    if y.isdigit() and len(y) == 4:
                        found.add(y)
        except Exception as exc:  # noqa: BLE001
            out["errors"].append("探测年级失败：%r" % exc)
        # 兜底：至少覆盖最近若干年（含未来一年，避免新方案漏抓）
        this_year = int(time.strftime("%Y"))
        for y in range(this_year - 6, this_year + 2):
            found.add(str(y))
        years = tuple(sorted(found, reverse=True))

    out["years"] = list(years)
    for year in years:
        items: list[dict] = []
        total = None
        for pageno in range(1, 8):                   # 最多 7 页
            try:
                data = query(year, pageno)
            except Exception as exc:  # noqa: BLE001
                out["errors"].append("%s 级第 %d 页失败：%r" % (year, pageno, exc))
                break
            rows = data.get("items") or []
            if total is None:
                total = data.get("totalResult")
            if not rows:
                break
            items.extend(rows)
            if total is not None and len(items) >= total:
                break
        if items:
            out["by_year"][year] = items
            out["total"] += len(items)
            log("    %s 级：%d 条" % (year, len(items)))
    return out


def fetch_plan_pdf_for_entry(page, su: str, entry: dict, dst: pathlib.Path) -> bool:
    """按方案条目下载对应的 PDF（供批量下载使用）。成功返回 True。"""
    jxjhh = str(entry.get("jxjhh") or "")
    if not jxjhh:
        return False
    ids = []
    if jxjhh.startswith("2"):
        ids.append("0" + jxjhh[1:])      # 观测到的写法
    ids.append(jxjhh)
    for pid in ids:
        url = (BASE + "pyfagl/pyfayl_cxPyfaylPdf.html?id=%s&doType=query"
                       "&gnmkdm=N153020&su=%s" % (pid, su))
        try:
            data = page.evaluate(
                """
                async (url) => {
                  const res = await fetch(url, {credentials:'include'});
                  const b = new Uint8Array(await res.arrayBuffer());
                  const isPdf = b.length>4 && b[0]===0x25&&b[1]===0x50&&b[2]===0x44&&b[3]===0x46;
                  let s=''; const C=0x8000;
                  for (let i=0;i<b.length;i+=C) s+=String.fromCharCode.apply(null,b.subarray(i,i+C));
                  return {status:res.status, len:b.length, isPdf:isPdf, b64:btoa(s)};
                }
                """,
                url,
            )
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict) and data.get("isPdf"):
            import base64
            try:
                dst.write_bytes(base64.b64decode(data["b64"]))
                return True
            except Exception:  # noqa: BLE001
                continue
    return False


def collect_all(page, su: str, *, skip_select: bool,
                major: str = "", enroll_year: int = 0, college: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 每次运行都**清空历史材料**，全部重新抓取。
    #
    # 为什么这样做：选课目录与课表**每个学期都会变**，培养方案的适用版本也可能调整。
    # 若沿用上一次的文件，会出现"用旧学期的课表算新学期"这类错误，而且换人登录后
    # 还可能混入上一个用户的数据。所以每次登录都完整重读一遍。
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.iterdir():
        if old.is_file():
            try:
                old.unlink()
            except Exception:  # noqa: BLE001
                pass
        elif old.is_dir():
            import shutil as _sh
            _sh.rmtree(old, ignore_errors=True)
    _log_line("已清空 var/academics，开始完整重新抓取")

    # 学籍信息（专业/年级/学院/班级）——自动读取，供后续步骤使用
    log("  读取学籍信息 ...")
    try:
        page.goto(PAGE_PROFILE.format(su=su), wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        profile = guess_from_html(page.content())
        (OUT_DIR / "学籍信息.json").write_text(
            json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
        out["学籍信息"] = profile
        log("    %s" % json.dumps(profile, ensure_ascii=False))
        major = major or profile.get("major") or ""
        college = college or profile.get("college") or ""
        if not enroll_year:
            try:
                enroll_year = int(profile.get("year") or 0)
            except ValueError:
                enroll_year = 0
    except Exception as exc:  # noqa: BLE001
        log("    读取失败：%r" % exc)

    # 每个接口都带上分页参数，一次取全。
    # 不带参数时服务端会按默认每页 10 条返回（成绩曾因此只拿到 10/22 条）。
    def _page_payload(year: int = 0) -> dict:
        p = {"_search": "false", "nd": "1",
             "queryModel.showCount": "500", "queryModel.currentPage": "1", "time": "0"}
        if year:
            p["synj"] = str(year)
            p["queryModel.sortName"] = "synj desc, xymc, jxjhh"
            p["queryModel.sortOrder"] = "asc"
        return p

    # 课表（当前用户的）
    log("  抓取 课表 ...")
    try:
        html = fetch_page(page, PAGE_TIMETABLE.format(su=su))
        if html:
            (OUT_DIR / "课表.html").write_text(html, encoding="utf-8")
            out["课表"] = html
        data = fetch_api_json(page, API_TIMETABLE.format(su=su), _page_payload())
        if isinstance(data, dict) and data.get("json") is not None:
            (OUT_DIR / "课表.json").write_text(
                json.dumps(data["json"], ensure_ascii=False, indent=2), encoding="utf-8")
            out["课表.json"] = data["json"]
            rows = (data["json"] or {}).get("items") or []
            log("    课表 %d 条" % len(rows))
    except Exception as exc:  # noqa: BLE001
        log("    课表抓取失败：%r" % exc)

    # 培养方案列表：**所有专业 × 所有年级**（翻页取全）
    log("  抓取 培养方案列表（全部专业/年级）...")
    try:
        plans = fetch_all_plans(page, su)
        (OUT_DIR / "培养方案-全部.json").write_text(
            json.dumps(plans, ensure_ascii=False, indent=2), encoding="utf-8")
        out["培养方案-全部"] = plans
        log("    合计 %d 条（%s）" % (
            plans.get("total"),
            "、".join("%s级 %d 条" % (y, len(v)) for y, v in (plans.get("by_year") or {}).items())))
    except Exception as exc:  # noqa: BLE001
        log("    培养方案列表抓取失败：%r" % exc)

    # 本人专业的完整培养方案 PDF（核心材料）
    if major:
        log("  下载 本人培养方案 PDF（专业=%s，年级=%s）..." % (major, enroll_year or "?"))
        try:
            detail = fetch_plan_pdf(page, su, major=major,
                                    enroll_year=enroll_year or 0, college=college)
            (OUT_DIR / "培养方案明细.json").write_text(
                json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")
            out["培养方案明细"] = detail
            if detail.get("pdf_path"):
                log("    PDF 已保存：%s（%d 字节，方案 id=%s）"
                    % (pathlib.Path(detail["pdf_path"]).name, detail["pdf_bytes"],
                       detail.get("pdf_id")))
            else:
                log("    %s" % (detail.get("error") or "未取得 PDF"))
        except Exception as exc:  # noqa: BLE001
            log("    培养方案下载异常：%r" % exc)

    # 选课系统：**全部课程**（遍历所有分组 + 翻页）
    if not skip_select:
        log("  抓取 选课系统全部课程 ...")
        try:
            sel = fetch_select_courses(page, su)
            (OUT_DIR / "选课-全部课程.json").write_text(
                json.dumps(sel, ensure_ascii=False, indent=2), encoding="utf-8")
            out["选课-全部课程"] = sel
            log("    共 %d 门课程（%d 个分组）" % (sel.get("total_unique"),
                                                  len(sel.get("groups") or [])))
            for e in (sel.get("errors") or [])[:3]:
                log("    注意：%s" % e)
        except Exception as exc:  # noqa: BLE001
            log("    选课课程抓取失败：%r" % exc)
    return out

    # 完整培养方案：自动定位并**下载 PDF**。
    return out


# ---------------------------------------------------------------------------
# 步骤 4：输出
# ---------------------------------------------------------------------------

def pick_saved(name: str) -> pathlib.Path | None:
    for ext in (".pdf", ".html", ".htm", ".json"):
        p = OUT_DIR / (name + ext)
        if p.exists():
            return p
    # 也接受自动识别到的任意文件名
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("fa", PROG_DIR / "fetch_academics.py")
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            found = mod.discover_saved_files()
            return found.get(name)
    except Exception:  # noqa: BLE001
        pass
    return None


def repackage_existing() -> int:
    """不联网，仅把 var/academics 里已有的材料重新整理成助手材料包。

    对应 --no-fetch：换了 App 的助手、或想重新生成 prompt 时用得上。
    """
    ident = {}
    info = OUT_DIR / "学籍信息.json"
    if info.exists():
        try:
            ident = json.loads(info.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            ident = {}
    cfg = load_config()
    major = cfg.get("major") or ident.get("major") or ""
    enroll_year = cfg.get("enroll_year") or 0
    try:
        enroll_year = int(ident.get("year") or enroll_year or 0)
    except Exception:  # noqa: BLE001
        pass

    if not any(OUT_DIR.glob("培养方案-*.pdf")):
        log("var\\academics 里没有培养方案 PDF，无法打包。")
        log("请先运行不带 --no-fetch 的命令完成抓取。")
        return 1

    pkg = write_agent_package(cfg.get("su") or ident.get("student_id") or "",
                              ident, major, enroll_year, {})
    log("助手材料已重新生成：%s" % pkg)
    try:
        os.startfile(str(pkg))
    except Exception:  # noqa: BLE001
        pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ZJU 学业数据采集向导（你自己登录；专业/年级优先从教务读取）")
    parser.add_argument("--su", default=None, help="学号（用于页面 su 参数；默认读配置）")
    parser.add_argument("--major", default=None, help="专业（给了就跳过确认）")
    parser.add_argument("--enroll-year", type=int, default=None, help="入学年份")
    parser.add_argument("--target-term", default=None, help="目标学期，如 5")
    parser.add_argument("--credit-cap", type=float, default=None, help="下学期学分上限")
    parser.add_argument("--interests", nargs="*", default=[], help="兴趣关键词")
    parser.add_argument("--skip-select", action="store_true", help="跳过选课目录抓取")
    parser.add_argument("--no-fetch", action="store_true", help="跳过抓取，用已保存文件出报告")
    parser.add_argument("--probe-majors", type=int, default=0, metavar="N",
                        help="登录后批量探测 N 个不同专业的培养方案能否下载（验证跨专业权限）")
    parser.add_argument("--native-login", action="store_true",
                        help="改用电脑上的普通浏览器登录（会在读完数据前要求你关闭该窗口）")
    parser.add_argument("--auto-login", action="store_true",
                        help="(已废弃，默认即为受控窗口登录，保留此参数仅为兼容)")
    parser.add_argument("--relogin", action="store_true",
                        help="强制重新登录（清空已保存的登录态）")
    parser.add_argument("--login-timeout", type=int, default=600,
                        help="等待登录的秒数，默认 600（10 分钟）")
    parser.add_argument("--debug", action="store_true",
                        help="异常时不等待按键，直接把堆栈写入 var/guide-error.log")
    args = parser.parse_args()

    # 统一兜底：任何异常都打印完整堆栈并留档，避免"窗口一闪就没"查不到原因
    try:
        return _run(args)
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        import traceback
        detail = traceback.format_exc()
        log("")
        log("=" * 66)
        log("程序异常退出，完整堆栈如下：")
        log(detail)
        log("=" * 66)
        try:
            (ROOT / "var").mkdir(parents=True, exist_ok=True)
            (ROOT / "var" / "guide-error.log").write_text(detail, encoding="utf-8")
            log("已写入：%s" % (ROOT / "var" / "guide-error.log"))
        except Exception:  # noqa: BLE001
            pass
        if not args.debug:
            try:
                input("按回车关闭...")
            except EOFError:
                pass
        return 1


def _run(args) -> int:
    cfg = load_config()
    # 学号处理：**不再要求用户输入、也不写死**。
    # 下一步会先登录，再从页面读出"当前登录者是谁"，所以这里的 su 只是
    # 可选的地址提示（某些教务页面需要 su 参数）。留空即可正常登录。
    su = args.su or cfg.get("su") or ""

    if args.no_fetch:
        # 不联网：把已抓到的材料重新整理成助手材料包
        return repackage_existing()

    from playwright.sync_api import sync_playwright

    # 用 Playwright 自己的窗口登录。
    #
    # 为什么不再用"原生浏览器 + 事后取 cookie"：cookie 只存在浏览器的加密数据库里，
    # 而该数据库在浏览器运行期间被独占锁定（实测 Copy-Item / .NET Copy / sqlite 全部
    # 失败）。于是只剩两条路：让用户关窗以释放数据库，或改用同一个配置目录——后者
    # 会与开着的浏览器争用目录，Playwright 必然启动失败（Edge 报"你已在此会话中"）。
    # 两条路都不好，所以直接用受控窗口：会话天然在进程内共享，无需 cookie、无需关窗。
    # 加 --native-login 才会走原生浏览器那条路。
    use_native_login = bool(getattr(args, "native_login", False))
    login_state: dict = {}
    if getattr(args, "relogin", False):
        _log_line("--relogin：清空登录态，强制重新登录")
        _clear_profile(PROFILE_DIR)
        if (STATE_PATH).exists():
            try:
                STATE_PATH.unlink()
            except Exception:  # noqa: BLE001
                pass
    if use_native_login:
        login_state = manual_login(su) or {}

    with sync_playwright() as pw:
        ctx, page = start_browser_and_wait_login(pw, su, login_timeout=args.login_timeout,
                                                 already_logged_in=bool(login_state),
                                                 storage_state=login_state)
        try:
            # 关键：先读出**当前登录者是谁**，学号一律以这里读到的为准。
            # 这样换人登录（同学用同一台电脑）时，程序访问的就是他自己的数据，
            # 不会像以前那样仍然用配置里写死的旧学号。
            log("")
            log("正在确认当前登录者身份...")
            ident = fetch_identity(page, su_hint=su)
            real_su = ident.get("student_id") or ""
            if real_su and real_su != su:
                log("  检测到当前登录者学号为 %s（与配置不同，已切换）" % real_su)
                _log_line("身份切换：配置 su=%s -> 实际 su=%s" % (su, real_su))
                su = real_su
            elif real_su:
                log("  当前登录者学号：%s" % real_su)
            else:
                log("  未能读出学号，将沿用地址参数 %s" % (su or "（空）"))
            for k in ("name", "major", "college", "class_name", "year"):
                if ident.get(k):
                    log("  %-10s %s" % (k, ident[k]))
            cfg.update({"su": su})
            save_config(cfg)

            major, enroll_year = resolve_identity(args, page, su)
            cfg.update({"su": su, "major": major, "enroll_year": enroll_year})
            save_config(cfg)

            # 批量探测模式：验证其他专业的培养方案能不能抓
            if args.probe_majors:
                log("")
                log("-" * 66)
                log("批量探测 %d 个专业的培养方案（验证跨专业权限）" % args.probe_majors)
                pr = page.evaluate(
                    """
                    async ([url, payload]) => {
                      const res = await fetch(url, {method:'POST', credentials:'include',
                        headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8',
                                 'X-Requested-With':'XMLHttpRequest'},
                        body: new URLSearchParams(payload).toString()});
                      const text = await res.text();
                      let j=null; try{ j=JSON.parse(text); }catch(e){}
                      return {status:res.status, json:j, head:text.slice(0,120)};
                    }
                    """,
                    [API_PLAN_LIST.format(su=su),
                     {"synj": str(enroll_year or 2025), "xydm": "", "zydm": "", "dlbs": "",
                      "jxjhh": "", "_search": "false", "nd": "1",
                      "queryModel.showCount": "300", "queryModel.currentPage": "1",
                      "queryModel.sortName": "synj desc, xymc, jxjhh",
                      "queryModel.sortOrder": "asc", "time": "0"}],
                )
                items = ((pr or {}).get("json") or {}).get("items") or []
                # 挑选分布在不同学院的专业，覆盖面更广
                picked, seen_colleges = [], set()
                for it in items:
                    col = (it.get("xymc") or "").strip()
                    if not col or col in seen_colleges:
                        continue
                    seen_colleges.add(col)
                    picked.append({
                        "major": (it.get("zymc") or "").strip(),
                        "college": col,
                        "jxjhh": it.get("jxjhh"),
                    })
                    if len(picked) >= args.probe_majors:
                        break
                log("   已选 %d 个不同学院的专业：%s"
                    % (len(picked), "、".join(p["major"] for p in picked)))
                rep = fetch_plan_pdf_batch(page, su, picked, limit=args.probe_majors)
                (OUT_DIR / "跨专业探测.json").write_text(
                    json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
                log("")
                log("   成功 %d 个，失败 %d 个"
                    % (len(rep["ok"]), len(rep["failed"])))
                for f in rep["failed"]:
                    log("     失败：%s —— %s" % (f.get("major"), f.get("reason")))
                log("   详情见 var\\academics\\跨专业探测.json")
                return 0

            log("")
            log("-" * 66)
            log("开始采集（只读，不触发任何选课或报名）")
            collected = collect_all(page, su, skip_select=args.skip_select,
                                    major=major, enroll_year=enroll_year)
            log("采集完成：%s" % ("、".join(sorted(collected)) or "（无）"))

            # 整理成"给 AI 助手直接读"的材料包（程序不解析、不下结论）
            pkg = write_agent_package(su, ident, major, enroll_year, collected)
            log("")
            log("=" * 66)
            log("助手材料已就绪：%s" % pkg)
            log("  manifest.json  材料清单（来源与用途）")
            log("  prompt.md      建议交给助手的任务描述")
            log("  README.txt     使用说明")
            for f in sorted(pkg.iterdir()):
                if f.is_file() and f.name not in ("manifest.json", "prompt.md", "README.txt"):
                    log("  %-28s %6.1f KB" % (f.name, f.stat().st_size / 1024))
            log("=" * 66)
            log("")
            log("说明：PDF 等材料原样交给 App 里的大模型阅读，本程序不做解析。")
            # 采集结束后立刻把登录态落盘，避免下次还要重新登录
            _save_session(ctx)
        finally:
            try:
                _save_session(ctx)        # 正常退出前再存一次
            except Exception:  # noqa: BLE001
                pass
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass

    log("")
    log("原始数据目录：%s" % OUT_DIR)
    try:
        os.startfile(str(pkg))            # 打开材料目录，方便直接交给助手
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
