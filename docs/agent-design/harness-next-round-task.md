# 可直接交给 DeepSeek Harness 的开发提示词

你正在开发 Campus Agent，工作目录为 `D:\AI\campus-agent`。本轮目标是补齐已经证实的 Agent 能力缺口，并提供可独立验收的运行证据。请检查当前源码、实际修改、运行测试并交付代码，不要只给方案或修改系统提示词。

成果将交回当前 Codex 对话，由另一位验收者独立检查 diff、运行回归、打开产物并审阅工具轨迹。你的完成声明不是验收结论。

## 0. 执行环境及工作区保护

先核实实际拥有：仓库文件读取、任务范围内源码写入、受控运行 Python/Node/npm/pytest 的权限。若你运行在 Campus Agent 的普通校园助手 profile 中，Shell/workflow/PTC 当前被明确禁用，普通文件工作区也不自动授予整个仓库的开发权限；提示词不能改变这些权限。

缺少开发工具时，提交 `blocked-environment.md`，列出缺失能力、被拒工具、结构化错误和最小所需配置。由可信启动端提供**只针对本任务工作目录的开发会话与受控命令权限**后再实施。不要修改产品 profile 来给自己提权，不要谎称已经运行测试。

仓库有大量既有未提交及未跟踪修改：

- 读取适用 `AGENTS.md`、git status 和目标文件当前 diff；保留现有工作。
- 不 reset/clean、整目录覆盖、stash他人改动，不自动commit/push。
- 基于当前文件记录源码哈希及修改前基线；只记录必要源码/测试，不归档个人数据或凭据。
- 若发现其他写入者同时修改目标文件，重新读取并合并最小改动，无法可靠合并时报告冲突，不能用旧副本覆盖。
- 所有测试DB、资料、会话、浏览器profile落独立UUID测试目录；不修改 `var/personal.db`、个人校园缓存、凭据或普通浏览器profile。
- 不关闭TLS、SSRF、账户scope、Origin/runtime token检查或外部提交确认；不修改OS代理、DNS、路由。

## 1. 必须先读并核实现状

诊断文件：

1. `docs/audit/architecture-transition-2026-10-15.md`
2. `docs/audit/transition-tool-audit.md`
3. `docs/audit/transition-summary.json`
4. `docs/audit/harness-next-round-acceptance.md`
5. `docs/agent-design/goal-v2-personal-assistant.md`

相关实现：

- `apps/api/app/agent_bridge/{tools,model,job_actions,files,runtime,store}.py`
- `apps/api/app/routers/{review_papers,resources,imports,exports,jobs,agent}.py`
- `apps/api/app/{review_engine,review_export,schemas}.py`
- `apps/agent-runtime/{campus-plugin,prepare-profile,capability-policy,runner,goal-lifecycle}.mjs`
- `apps/agent-runtime/skills/document-artifacts/`，特别是 `scripts/export_documents.py`
- `apps/api/tests/test_document_artifacts.py` 及现有review、scope、job、Agent测试
- `tools/{audit_regression,verify_agent_runtime,audit_live_transition}.py`

先给出简短实施计划、实际支持与缺失清单，然后继续实施；日常命名、文件拆分和测试选择自行决定，无须反复确认。若当前代码已实现某项，验证后复用，不重复开发。

## 2. 本轮范围与优先级

本轮必须完成M0—M3，顺序实施。不要同时迁移所有工作台接口。

### M0：冻结基线并保护已经修复的链路

先运行相关回归，记录命令、exit code、耗时、passed/failed/skipped和源码哈希。2026-10-15曾记录API616通过/5跳过、Node85通过，它们是历史参考值，不是当前代码必然达到的数字，也不是伪造结果的模板。

必须保留：

- sync job在返回前提交，独立worker和下一HTTP请求可见。
- import后仅该会话获得服务端验证的新课程scope；其他会话不扩权。
- 分页游标、partial、空结果、跨平台失败隔离；不把当前页当全量。
- 窄scope不能同步/复用/读取/取消宽范围job。
- 后台failed/partial_failed不返回虚假的业务成功。
- 浏览器长操作不阻塞无关本地工具，停止仍等待原调用收尾。
- 自定义模型桥的限流/超时可进入官方有限重试；认证错误不重试。
- 匿名网页地址防护保留；精确已登记的教务只读通知路径不误判提交。

发现基线新失败，先定位与本任务的关系。原有失败可以记录，但不得删测试、放宽断言或增加skip来获得通过。

### M1：恢复可信文档导出能力

新增一个固定业务能力工具，建议名 `artifacts.export`，最终名称可调整，但必须在注册表、模型schema、服务和测试中一致。

功能：把已获得的结构化内容渲染为真正的PDF/XLSX，可同时支持既有Markdown，完成后返回本会话可下载文件ID与元数据。复用现有 `export_documents.py` 及已有reportlab/openpyxl；不为渲染再实现通用代码执行器。

要求：

1. 输入采用明确且有大小上限的类型：标题、来源、段落/表格、有限sheet/row/cell与格式。渲染器现有validate不等于已具备适合Agent的严格输入契约。
2. 不接受Python/JS/Shell代码、命令、可执行表达式、任意模块、任意输出目录/字体路径或远端URL作为渲染参数。
3. 若使用输入文件，只能接受服务端验证的本会话文件ID/任务工作区内的受限输入；身份/根目录由服务端确定。权限检查也覆盖symlink、路径穿越与其他会话文件。
4. 可以固定可信模块调用，也可以固定解释器+固定脚本的受控子进程；模型不能选择执行程序。设超时、输入/输出/行列大小限制，失败清理仅限本次临时产物。
5. 结果通过现有会话文件存储交付，产生真实发布回执。渲染成功但发布失败必须明确区分；同一可信call ID重放不重复创建文件，参数变化拒绝。
6. PDF中文可读、表格不裁切；XLSX数字/日期/普通文本类型正确。不把以`=`开头的普通字符串自动变公式；不接收任意公式/外链/DDE。最小版本可只支持文字、数字、日期与布尔值；不能声称公式已计算。
7. 未知事实保留未知，不由渲染器猜日期。没有来源时明确缺失；不声称程序能够证明模型填写的事实都来自原文。
8. 模型看到成功前，产物必须确实存在并可从会话下载。失败返回结构化错误，不能仅靠后缀伪造PDF/XLSX。
9. 接通工具后再更新document-artifacts skill和persona中的“当前只能文本”说明；权限策略继续禁止任意Shell。

验收闭环A：

```text
用户上传合成资料
 → 模型实际read
 → 模型构造结构化内容
 → artifacts.export渲染
 → 发布到本会话文件区
 → API下载
 → PDF/XLSX解析及视觉/内容核验
```

### M2：接通复习卷业务，不复制原引擎

为既有review服务提供最小Agent适配，建议能力为 `reviews.preflight`、`reviews.generate`、`reviews.read`、`reviews.export`；名称可按现有约定调整，不强制一函数一工具，但必须覆盖整个业务流程。

要求：

1. 复用原有材料选择、预检、冻结快照、版本、题目配额/约束、已提交作业排除和费用配置。不要在tool文件中重新造出卷引擎。
2. 只允许选择本会话授权且当前可用的课程/材料版本。模型不能指定owner、任意provider地址、密钥或扩大scope。
3. 预检返回缺失资料与可继续条件。只有元数据/未解析资料时不假装读过；旧预检、过期版本与删除材料需拒绝或明确重新预检。
4. 生成返回持久job ID。随后 `jobs.read` 必须能读到该会话发起的review job；补齐job归属，不接受“只要知道job_id就能读”。
5. 受理、运行、部分失败、失败、完成、取消分别返回；请求结束、重复call、服务重启不丢已受理任务，不重复生成扣费任务。
6. 原后台review_generate需要模型调用时，沿用已配置服务端provider与现行资料发送授权及预算，不绕过配置，也不把密钥送入工具。
7. 读卷与导出都验证paper/revision/snapshot归属。最小版本必须交付原有可靠的Markdown导出；PDF可接M1，须保留题目、答案版本和证据。
8. 用户明确要求生成复习卷后，正常读取、预检、已授权生成和本地导出不要求逐步点击UI。只有实际需要登录/新增资料授权/缺失必要信息时才进入needs_user。
9. 如果原业务藏在router中，可将本轮必需的业务方法移到小型应用服务供HTTP/tool复用；保持原REST和工作台页面兼容。不整体搬迁全部router。

验收闭环B：

```text
用户指定已授权课程
 → 模型发现/读取真实合成材料
 → preflight
 → generate（冻结版本、持久入队）
 → jobs.read（真实worker完成或明确失败）
 → read指定卷版本
 → export并发布下载
 → 验证文件内容与冻结材料一致
```

原引擎某种mode缺题库/模型/依赖时应保留真实标准错误；选择项目实际支持的最小mode完成闭环，不伪造生成结果。

### M3：建立可复跑的验收证据

新增自动化场景和一个运行入口，保存到独立测试目录。不要覆盖上一轮诊断的JSON/XML，也不要把测试资料写入个人库。

至少覆盖验收文件列出的12类场景。必须分开报告：

- 直接工具/业务层测试，不经过模型。
- 实际FastAPI+Harness+受控模型+合成业务数据。
- 真实配置模型+合成课程/附件+实际工具执行。
- 真人校园授权验证；没有条件就明确未执行。

真实模型至少测“附件→PDF/XLSX”“课程材料→复习卷→导出”“缺资料时准确报告限制”三项，每项3个独立会话，共9次。固定同一provider/model、相同输入数据、工具暴露策略、推理参数、预算与判分规则。API不支持固定seed时记为未控制因素，不声称确定性。

每次最多12个模型请求、40个工具调用、150秒；不得提高预算来掩盖回归。实际设置受项目已有更严格限制约束。用已保存且启用的模型配置，不索取用户在提示词中填写key；命令必须显式`--live`并记录实际调用用量。无法使用模型时交付可复跑入口并标记未验收，不能用受控模型替代真实成绩。

记录：真实可见工具、选择/参数、call ID、工具执行与结果、错误与恢复、最终产物ID/哈希、任务目标判分、人工介入、耗时、模型/工具调用数和usage。没有账单依据的金额写unknown；搜索/业务生成的额外模型调用用量单独记录，不混成主Agent用量。

通过标准见验收文件；不要以 `turn=succeeded` 或“所有预期工具都被调用”代替业务达成。

## 3. 本轮不做

- 不重写Harness loop，不新增第二个planner、关键词任务路由或无证据的多Agent框架。
- 不扩大校园适配平台，不承诺WebVPN认证或真人学在资料已验收。
- 不迁移全部一般资料导入、长期偏好修改、JSON/ICS导出；列为后续待办。
- 不顺带更换模型、升级上游依赖、重做UI、重命名现有27工具。
- 不把自定义模型桥的contextWindow/usage改造混入本轮，除非阻塞上述验收且有复现；独立说明最小修复。
- 不用前端隐藏按钮、system prompt或MCP readOnlyHint代替服务端权限。

## 4. 回归命令与交付

优先使用项目已有隔离测试入口：

```powershell
# 仓库根
apps/api/.venv/Scripts/python.exe tools/audit_regression.py apps/api/tests -q -p no:cacheprovider
apps/api/.venv/Scripts/python.exe tools/audit_regression.py tests connectors/tests -q -p no:cacheprovider
apps/api/.venv/Scripts/python.exe tools/verify_agent_runtime.py
node apps/agent-runtime/verify-transition-recovery.mjs
node apps/web/node_modules/typescript/bin/tsc -b apps/web
apps/api/.venv/Scripts/python.exe tools/export_agent_contract.py --check

# apps/agent-runtime目录
npm run test:audit
```

运行前核对当前环境；可调整命令适配现有测试入口，但必须解释原因。若改HTTP契约，同步导出并检查兼容性。修复导致完整演练工具目录变化时，更新准确清单，保留对未授权工具不可见/不可执行的断言。

将本轮交付说明写到 `docs/audit/harness-next-round-result.md`，机器证据写到独立 `docs/audit/harness-next-round/`：

1. M0—M3逐项完成/失败/受阻情况。
2. 本轮修改文件和每项对应的复现、修复、回归、回滚方式。
3. 实际Tool Schema与端到端调用轨迹。
4. 12类确定性场景和9次真实模型任务的明细，错误与失败尝试同样保留。
5. PDF/XLSX/复习卷样例、授权下载方法、文件哈希及核验结果。
6. 完整回归命令、exit code、耗时、skip原因。
7. 仍缺的权限、服务、账号和未完成能力。

完成后把结果文件路径与简短摘要交回原对话，由Codex独立验收。不得在交付中自行写“已通过最终验收”。
