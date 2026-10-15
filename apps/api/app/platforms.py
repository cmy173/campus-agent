"""平台常量（登录入口等）。

CR001 裁定（`contracts/reviews/2026-10-09-CR001-A12裁定.md` DECISION-3）：
本期无回跳，`entry_url` 为服务端权威入口常量（打开即 302 到平台登录页），
不回传任何回跳/配对参数；平台登录后由用户回到工作台触发连接检查衔接。
"""
from __future__ import annotations

ENTRY_URLS: dict[str, str] = {
    "xzzd": "https://course.zju.edu.cn/ua/login",
    "zykt": "https://classroom.zju.edu.cn/",
}


def entry_url_for(platform: str) -> str | None:
    return ENTRY_URLS.get(platform)
