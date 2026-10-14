# 首轮（pre-V2）历史文档

这里保存第一轮工作台/旧服务器时期的说明。对应的可执行实现已在 V2 退役：

- `campus_agent`（旧 FastAPI 应用、静态界面、旧启动链路、`notice_*` 抽取管线、旧 `llm.py` 转发、旧 `storage.py`）已删除；
- 校园协议实现已单份迁入 `connectors/campus`，产品入口为根目录 `tools/dev.py` + `apps/web`（`/chat`）。

文档保留用于追溯当时的验证记录与决策，其中的启动命令、路径与接口不再可执行。
当前状态、迁移台账与验收证据见 [../agent-v2-progress.md](../agent-v2-progress.md)、
[../agent-v2-migration.md](../agent-v2-migration.md) 与 [../agent-design/](../agent-design/)。
