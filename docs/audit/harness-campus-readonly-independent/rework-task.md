# 交给 DeepSeek Harness：校园只读轮次返工

请先读同目录 `acceptance.md`。当前独立结论是需修改，不要重新开发已通过的缓存纯读、课程资料和计划能力。
基于当前未提交工作继续，保留他人修改及所有原始证据，不 reset/clean，不自动 commit/push。

本轮只处理以下四项：

1. **可信恢复探针禁止启动认证**：`Runtime.probe_authenticated_read()` 映射到普通 `campus.sync` 后可能执行 `auto_relogin_locked()` 或私有浏览器 Cookie 采纳。
   将可信只读探针与用户主动显式同步的认证恢复策略区分。目标会话尚未就绪就返回结构化错误并保留检查点；用户显式 sync 的有界、已授权恢复继续可用。
   不绕过身份/授权复核和撤销规则，不以改 docstring 掩盖行为。独立探针已复现认证入口调用一次。
2. **补真正的 T4 自动恢复**：在测试启动/学校协议边界注入已认证合成会话，使用实际 API、安装的 Harness、持久检查点与可信认证通知，由服务端产生续轮；不能把人工发消息算自动恢复。
   先跑受控模型，无需真人账号。不增加生产公开 Cookie/权限注入入口，不 mock 最终业务成功。保留其他平台认证/目标未认证的负面对照；补重启、撤销和窄 scope 核对。
3. **修复两处判分误报**：通知“没有…全校性通知”不能判为全校肯定断言；`connections.sync(wait_seconds>0)` 已返回同一任务 succeeded 时可作为终态证据，不强制额外 jobs.read。
   对 queued/running、错 job ID、failed/partial_failed、只读摘要、真正肯定性全校断言添加负面对照，不能只放宽断言。
   已校验完整正文从 campus.query 返回同样有效；不要求 notice.read 是唯一入口，不为评测重复调用工具。
4. **增加离线重判并更新证据口径**：独立抽测 T2 的记录在 `var/campus-readonly-independent/model-t2.json`。
   交付两轮原 T1/T2/T3/T4 与独立 T2 的非破坏式重判：保留原哈希/原判定、输出新 judge 版本和理由。
   修复 judge 不需要重跑全部收费模型。受控 T4 通过后才按既有预算补一次真实模型 T4；预算/配置无法核实时标待验证。

独立可复跑探针：

```powershell
apps/api/.venv/Scripts/python.exe -X utf8 tools/audit_regression.py docs/audit/harness-campus-readonly-independent/test_additional_boundaries.py -q -p no:cacheprovider
```

当前结果是 2 passed / 3 failed。其中两个判分探针需要上述独立 T2 证据文件；不要删测试、改原始结果或移除负面断言换通过。
产品修复新增的回归请进入正常测试套件；验收方探针文件不要直接修改，存在不同判断先提供反例证据。

本轮完整回归已有 API 713/5 skip、连接器/启动器 606+87 subtests、Node 85、受控集成 83 次调用的通过基线。
先跑受影响测试与受控 T4，再跑必要完整回归。前端未改不必重复构建，不重跑与本返工无关的上一轮付费导出评测。

请输出 `docs/audit/harness-campus-readonly-rework-result.md`，原始证据放新的 `var/harness-campus-readonly-rework-<UUID>/`。
列每项前后证据、准确文件/哈希、命令/exit code/耗时、调用轨迹、判分变更及未验证平台；不得自行宣布最终验收通过。
