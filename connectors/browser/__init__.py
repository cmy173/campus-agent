"""受控浏览器组件（A17 原型包）。

设计依据：`connectors/docs/A5-跳转式登录设计.md` v0.2 §4（受控浏览器 + 持久化 profile）。
实现形态：`controlled_login.py` 最小可跑原型（Playwright + 系统 Edge/Chrome channel）。
红线：无凭据入参；cookie 仅内存持有；日志经 A8 脱敏；独立 profile 目录（不碰用户主浏览器）。
"""
