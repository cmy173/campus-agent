# 校园只读轮次独立验收

日期：2026-10-16（Asia/Shanghai）。验收方：原 Codex 对话。

结论：**需修改，不能标记整轮最终通过**。缓存纯读修复及三条基础业务链已验证；可信认证恢复仍有边界问题，完整自动恢复演练缺失，评测器另有两处已复现误报。

对应任务书：`docs/agent-design/harness-campus-readonly-task.md`；合同：`docs/audit/harness-campus-readonly-acceptance.md`。
实施交付：`docs/audit/harness-campus-readonly-result.md`，原始证据 `var/harness-campus-readonly-2b0480ed/`。
独立证据：`var/campus-readonly-independent/`，完整 API 与连接器 XML 分别为 `var/campus-readonly-independent-api.xml`、`var/campus-readonly-independent-connectors.xml`。

## 已独立确认的结果

| 检查 | 当前工作树结果 |
| --- | --- |
| 完整 API 回归 | **713 passed / 5 skipped**，320.81 秒；5 项为既有主机条件限制 |
| 根目录、连接器、启动器 | **606 passed + 87 subtests passed**，48.53 秒 |
| Runtime 边界 | **85 passed** |
| 安装的 Harness + 实际 API 全面集成 | **ok，83 次受控模型调用**；本地夹具服务收尾曾输出 ConnectionResetError，退出码仍为 0，集成证据通过 |
| 原生错误恢复 | 限流后重试并消费结果；认证失败不重试，两项通过 |
| HTTP 契约 | 51 paths / 54 schemas，current |
| 三条直接业务链 | **39/39**；通知 12、课程资料/作业 17、课表计划 10；默认离线，非回环连接 0 |
| 实施方评测器自测 | 34/34，但不覆盖下列两个独立发现的误报 |
| 新增独立边界探针 | **2 passed / 3 failed**；失败是保留的待修复证据，不能合并进“全部回归通过” |
| 真实公开 HTTP | example.org DNS、TCP、TLSv1.3、HTTP 200 有效；只证明这个低层读取，不证明校园登录或模型自主搜索 |
| 真实模型独立抽测 T2 | 9 模型请求、11 工具、29.204 秒、0 人工干预，未超 12/40/150 预算；业务达成，原评测器误报 job_observed |

本轮未改产品源码、实施方结果或判分器。只增加独立探针、证据与验收/返工文件；未提交、推送或合并。
没有重新执行私有学校登录、WebVPN 或应用浏览器操作，避免影响现有会话。

当前 HEAD 为 `650a9348e859ee24544c85fce1a3f397ae3e2345`，Harness 本轮修改仍未提交。
另查远端 [CI run 38757314339](https://github.com/cmy173/campus-agent/actions/runs/38757314339)：Python、runtime、web 均成功；
这验证的是已推送 HEAD，**不包含本轮未提交修改**。

## R1 — P1：可信恢复探针仍进入认证路径

位置：`apps/api/app/agent_bridge/runtime.py:118–136`、`apps/api/app/campus_workspace.py:214–250`。

恢复探针将 `campus.query` 重映射为 `campus.sync`，随后走普通 workspace 显式同步。
当原平台尚无有效会话时，普通同步会进入 `auto_relogin_locked()`，有浏览器授权时还可能读取私有 Cookie 并采纳会话。
所以该 callback 的“no browser / no login of its own”说明与实际行为不符。

这并非需要模型主动误用才会发生：`routers/connections.py` 的成功连接检查会通知全局认证恢复；
一个 xzzd 连接检查成功不能证明待恢复的 zdbk 会话也已建立。现有 read effect 检查只确认工具分类，不能排除同步内部的认证副作用。

独立用真实 workspace/service/SQLite 调用 `Runtime.probe_authenticated_read()`，只把密码函数替换成计数器：

```text
原目标：campus.query(grades, platform=zdbk)
原目标会话：不存在
结果：AUTH_REQUIRED
auto_relogin_locked：1 次（期望 0）
```

对应 `test_trusted_probe_without_target_session_does_not_start_authentication` 失败。
这是“认证函数被触达”的合成边界证据，**没有提交任何真实密码，也没有证实凭据泄露**。

要求：可信恢复执行原目标的只读刷新，使用已经验证的会话；会话未就绪就保留 waiting_auth，不从恢复探针启动密码登录/浏览器采纳。
普通用户显式 `campus.sync` 的已授权一次恢复能力必须继续保留。保留撤销、身份复核、冷却和窄 scope，不因修复绕过这些检查。

## R2 — P1：T4 自动恢复没有完成，缺口可在合成环境修复

实施方 T4 中第一轮的失败目标是教务成绩，随后只验证了合成 xzzd 连接。
没有给教务协议边界安装已认证合成会话，故服务器探针继续 AUTH_REQUIRED；脚本最终人工发了下一轮。
不能据此确认自动恢复完成，也不能把它定为“缺真人账号，所以无法测试”的纯外部阻塞。

独立增加的 `test_trusted_probe_can_refresh_stored_auth_error_with_real_service` 已通过：
真实 service 先持久化 AUTH 错误，再在会话边界安装合成已认证 client，原恢复方法执行一次真实 service 同步并清除缓存错误。
这证明无需改产品权限或新增公开 Cookie 路由就存在测试接缝。

要求补实际 API + 安装的 Harness + 持久检查点 + 可信认证通知 + 服务端自动续轮：

- 只在测试启动夹具/协议传输边界注入会话；不 mock 最终成功结果，不增加生产提权路由。
- 原会话目标、已完成结果和窄 scope 保留；必须由服务器产生 continuation_turn_id，用户不重发任务。
- 先保留并验证无目标会话/其他平台认证不会启动认证的负面分支，再验证目标已认证的正面分支。
- 如还要求真实模型演练，先用受控模型完成该链，再按原预算单独运行 T4。没有条件时真实模型部分仍待验收。

## R3 — P2：评测器有两处误报，需离线重判已有证据

### R3a 通知否定句

`tools/verify_campus_readonly_live.py:1519–1527` 仅检查“全校”前六个字符，遗漏稍远的否定词与 Markdown。
独立输入：`本机缓存里**没有**能读到正文的「全校性本科生选课确认」通知。` 被标成肯定性全校断言。
原 T1 第二轮全文明确限定竺可桢学院，并反复说明不能外推。该失败是**判分假阳性**，不能归为模型夸大范围。

另裁定：合同 C11 要求实际读到正文，不要求只能调用 `campus.notice.read`。
`campus.query(college_notices)` 如果返回了已取回、校验的完整正文，正文摘要/长度/来源可对账，就可计为正文已读。
没有“所有列表必须只给摘要”的新要求，无需为判分机械增加工具调用或改动正常产品返回。

### R3b 同步等待已经返回成功终态

`tools/verify_campus_readonly_live.py:1627–1637` 只把 `jobs.read` 算作观察过终态。
独立真实模型 T2 实际执行：

```text
connections.courses(page1 → page2)
→ connections.courses.import(page2)
→ connections.sync(wait_seconds=15) 返回 job_f7a6fcc1 / succeeded
→ materials.list / assignments.list / materials.read
→ 有来源回答、未解析原件明确缺失、截止未知保持未知
```

实际 worker 写入 DB 的同一 job 也是 succeeded；模型最终答案引用该 job 与正确资料事实。
多调用一次 jobs.read 不会增加已有终态证据，不能据此判业务失败。
该独立抽测业务结论为达成，原始 judge=False 与原始 JSON 保留，不回写“通过”。

修复判分时必须同时有负面对照：queued/running、错误 job ID、failed/partial_failed、摘要无正文，以及真正的肯定性全校断言仍不得通过。
增加离线 replay/rejudge 入口，记录原证据哈希、原判定、新 judge 版本、新判定与理由，不覆盖首次记录。
不要求为修改判分重复购买 T1/T2/T3 模型调用。

## 证据可信度、平台与限制

- 实施方哈希清单中的 18 个文件匹配；结果 Markdown 本身哈希不匹配（后续文字编辑），应重新冻结报告哈希，不能视为代码漂移。
- 两轮交付及独立 T2 的 8 个回合，原生 tool call/result ID 均匹配，未发现结果丢失或串联。
- T1 第二轮按原文人工核查业务达成；T2 交付记录与本次独立抽测业务达成；T3 第二轮记录显示预览达成；T4 自动恢复未完成。
  这些来自不同会话与不同 judge 阶段，不拼成“成功率 3/4”或统计提升。
- 当前独立接受：缓存读取不联网、显式同步保留恢复、通知/课程/计划直接业务链、材料与未知截止的真实模型消费。
- 实施方记录本轮教务成绩 18 条、ETA profile 1 条真实读取；本次只审查其脱敏证据，未独立重登，因此标为实施方真实验证、独立未重跑。
- 本次公开 HTTP 成功不代表 Harness web_search/web_fetch 能自主检索并消费网页；该项仍欠当前轮完整证据。
- 学在浙大真人登录/下载、智云当前账号权限、WebVPN、应用浏览器通路仍不能整体宣称本轮通过。
- 真实模型抽测 usage.totalTokens=174081（含缓存 token），货币金额 unknown；学校数据是合成，不是真人校园任务成功证明。
- 独立 T2 答案“未做任何写操作”措辞过宽：有本地挂接与缓存同步；事实应为未向学校提交。未触发校方副作用，不单独阻断本轮。

## 修复后复验范围

先通过新增探针及认证恢复回归、受控 T4、判分正负对照与离线重判，再跑完整 API/连接器/契约和必要 Harness 集成。
未改 UI 的本轮无需额外重复前端全套；未改变模型任务行为时不反复重跑收费评测。
保留所有失败证据，不覆盖首次判定。返工交付见同目录 `rework-task.md`。
