# Campus Agent 核心试用版进度

日期：2026-10-13。首轮 M0–M2 已实现并完成逐项证据核对；Goal 完成状态由运行时最终设置。

唯一实施目录：C:/Users/mingxiang/.codex/worktrees/campus-agent-core/campus-agent/workbench；父分支 codex/agent-core，基线3a853560。源码未提交/推送。

完整验收和限制：agent-acceptance.md。历史尝试与中间测试见 agent-progress-history.md，历史的“未完成/运行中”不代表当前状态。

## 已验证

- 原源码包408项SHA与包哈希匹配，原工作55文件无缺失；53字节一致，两份授权更新的设计文档保留旧版备份。
- 官方Harness0.2.0-rc.2、18项显式插件、16项严格校园工具；实际领域引擎与受控模型链路，第二轮/取消/服务重启恢复。
- 对话主入口、来源/日历工作区、草稿与课程上下文；具体方案接受、临时约束、应用/撤销、任务生成/反馈与真实job取消。
- 同一实际浏览器会话查询/原文/本周三晚间/方案/日历/取消/反馈/撤销，桌面和390px窄屏，刷新恢复；截图与JSON见assets/agent-browser-full.json及此前m1/m2证据。
- 最终API整套272通过，前端19文件125通过；错误UI专项9通过；干净目录最终构建通过。
- 干净目录D:/AI/campus-agent-rebuild-c72uzzk5安装、迁移、API/web启动smoke及41次真实Harness模型调用通过，最新证据var/agent-integration-icq_4a_j/evidence.json。所有验证进程均已正常退出，无未回收运行句柄。

## 当前限制

真实校园账号/付费模型质量/学生试点未验证；本机单人/单API/单worker；检索为子串，资料覆盖与日期未知保留。固定上游依赖审计13条涉及两类根因，未启用相关MCP/Office插件，漏洞未修补；详见验收报告。后续M3–M4、公开部署和学校写入不在首轮范围内。

## 首轮八项门槛

| 条件 | 状态 | 证据 |
| --- | --- | --- |
| 1 干净安装构建启动 | 已验证 | agent-clean-rebuild.json |
| 2 真实Harness插件/工具 | 已验证 | runtime-version及集成runtime/model_visible_tools |
| 3 同入口三场景 | 已验证 | browser-full/m2 JSON及集成plan_turns |
| 4 成功空结果非法越权版本重复失败取消 | 已验证 | 各agent测试与最终272API检查 |
| 5 约束及事实精度 | 已验证 | constraints/plan_actions/PD3/feedback测试 |
| 6 桌面窄屏和上下文 | 已验证 | assets/agent-browser-full.json及截图 |
| 7 基线和新增回归 | 已验证 | API272、前端125、干净构建 |
| 8 文档/限制/报告 | 已验证 | agent-acceptance及相关运行/工具文档 |

下一阶段建议：先处理固定上游依赖升级，再单独授权真实模型与学校账户试点；如需M3–M4使用设计目录中下一轮Goal合同。首轮没有待实施工作，不自行扩大范围。
