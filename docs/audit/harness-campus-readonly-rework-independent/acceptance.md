# 校园只读返工：第二次独立验收

日期：2026-10-16（Asia/Shanghai）。结论：**需修改**。

上轮指出的外层 CAS 恢复边界及两处判分误报已修复，受控 T4 已有有效正面证据。
仍有两个认证恢复路径遗漏、一个证据保护缺陷，真实模型 T4 也未完成；不能将局部修复写成整轮最终通过。

交付：`docs/audit/harness-campus-readonly-rework-result.md`。
独立证据目录：`var/campus-readonly-rework-independent/`。
验收基线仍为 HEAD `650a9348e859ee24544c85fce1a3f397ae3e2345` 加本轮未提交修改。
验收方未修改产品源码、实施方脚本及旧证据，只新增独立测试和报告；未 commit/push/合并。

## 独立验证结果

| 检查 | 结果 |
| --- | --- |
| 完整 API | **714 passed / 5 skipped**，308.63 秒；skip 为既有主机限制 |
| 根目录/连接器/启动器 | **606 passed + 87 subtests passed**，50.18 秒 |
| 上次 5 项独立探针 | **5/5 passed**，未改探针文件 |
| 新受控 T4 | **49/49 passed**；5 次回环模型请求，非回环连接 0，隔离树已删除 |
| 三条直接业务链 | **39/39 passed**；离线，无学校网络请求 |
| 完整实际 API → 安装的 Harness 集成 | **ok，83 次受控调用**；本地夹具服务结束时有一次连接重置输出，最终 exit 0、集成结果通过 |
| 评测器 judge v2 | **42/42 自测通过** |
| 非破坏式重判的默认路径 | 三份、**7 条**会话已重判，三个来源文件 SHA256 均保持不变；不拼成统一成功率 |
| HTTP 契约 | 51 Agent paths / 54 schemas，current |
| 本次新增边界测试 | **1 passed / 3 failed**，已保留失败记录 |
| 交付源码哈希 | 报告列出的 7 个文件全部匹配 |

本次未新增付费模型请求，未访问真实校园/门户，未使用真实校园凭据。
未改前端与 Node 实现，沿用上一轮相应测试结论，不把历史测试冒充本轮重跑。

## 已接受的修复

1. 恢复 callback 将服务端私有的 `allow_auth_recovery=False` 传到 workspace；外层 CAS 重登和浏览器采纳在该路径被禁止。
   普通用户显式 sync 保留默认的已授权恢复，不新增模型可指定的身份/凭据字段。
2. 通知否定句不再按“全校”前六个字符机械判定；真正全校肯定句的原有对照仍被拒绝。
3. 显式等待同步返回同一个持久 job 的终态，且 worker 行一致，可计为已观察终态，不强制冗余 jobs.read。
4. 受控 T4 确实由服务端入队续轮：持久目标/窄 scope 保留，写操作被拒；不是驱动人工发送第二条消息。

受控 T4 正面路径是**安装目标合成会话后重启 API**。修改隔离 journal 的 next_probe_at 用于模拟冷却窗口流逝，
这一替换已明确记录，不按真实等待计时。它证明启动恢复有效，但不能证明下述正常账号设置登录成功事件已接通。

## N1 — P1：禁止认证的策略没有到达 WebVPN 传输层

代码位置：`connectors/campus/campus_network.py:225–231`、`connectors/campus/webvpn_tunnel.py:151–167,201–219`。

当前标志仅在 workspace 得到 AUTH 错误后，阻止 `_resync_result` 进入外层恢复。
但是第一次 service.sync 已经会使用 CampusTransport：当目标教务会话在内存有效、通道被选为 WebVPN、门户会话已过期或未建立时，
`_open_mapped → tunnel.request → open → ensure_session → adapter.login` 会在更低层启动门户登录。

独立测试使用真实 CampusClient、真实 CampusTransport、真实 WebVPNTunnel 和真实恢复 callback，
只把门户 adapter 的最终 login 换成计数/返回验证码错误，并禁止真实 socket.connect：

```text
allow_auth_recovery=False
CAS 会话有效 / 教务连接有效
WebVPN 凭据为合成值 / 门户会话不存在
结果：portal-password-submit 进入 1 次（期望 0）
```

对应 `test_transport_auth_boundary.py` 失败。没有真实提交密码或访问学校，证明的是底层认证入口可达。
本轮受控 T4 替换了 CampusClient.request，所以恰好绕过了这层，49/49 无法排除该问题。

要求：无认证恢复策略必须按调用传到传输/隧道；无有效门户会话或门户过期时返回结构化需要认证状态并保留检查点，不能隐式门户重登。
已经有效的门户会话仍可读取；普通显式 sync 的已授权通道恢复仍可用。不全局禁用 WebVPN/自动登录，也不切通道重投密码。

## N2 — P1：正常账号设置的登录成功未唤醒恢复

代码位置：`apps/api/app/routers/campus.py:112–124`。

用户在账号设置调用 `POST /api/v1/agent/campus/credentials/test-login {scope:passport}`。
真实处理器在 `auto_relogin_locked()` 返回 `ok=true/AUTO_LOGIN_OK` 后直接返回结果，没有调用 `resume_authenticated_tasks()`。
相邻的 connect-browser 路由有该通知，xzzd check 也有；此实际登录入口遗漏。

独立 HTTP 路由测试仅替换学校认证结果与状态，观察真实 runtime 的恢复通知方法：

```text
成功登录：HTTP 200 / ok=true / connected=true；通知数 0（期望 1） → 失败
失败登录：HTTP 200 / ok=false；通知数 0（期望 0） → 通过
```

所以 waiting_auth 的任务不会由这个成功动作重新武装探针，可能一直等到别的通知或进程重启。
当前受控 T4 的重启路径掩盖了这个用户流程缺口。

要求：经服务端验证的 passport 登录成功后、释放认证锁之后通知恢复；失败/验证码/只保存凭据不能当作认证成功。
增加**不重启 API**的正面集成：原任务等待 → 目标登录成功的正常可信入口 → 服务端产生续轮 → 回答原目标；保留失败和无关平台负面对照。
不应依赖前端另发人工续轮或让模型重新登录。

## N3 — P2：rejudge 输入输出重合时覆盖原始证据

代码位置：`tools/verify_campus_readonly_live.py:2861–2866`。

默认命令保持原件不变，这部分通过。但参数允许 `--rejudge-input X --out X`。
程序读取 X 后把重判报告写回同一个 X，原始会话/原判定被替换，违反命令承诺的“never rewrites the originals”。

独立测试只对临时合成 JSON 执行该命令，观察到输入字节改变，已保存失败；既有真实记录未被用于破坏性测试。

要求：写入前拒绝输出与任一输入的同文件关系；包括相对/绝对别名、符号链接/硬链接等实际同文件关系。
拒绝时原字节不变、非零退出。允许向单独报告文件正常输出，保留输入 SHA256 和原判定。

## 尚未完成：真实模型 T4

实施方明确承认预算和 provider 配置可核实，但未把合成已认证会话接缝移植到真实模型驱动，因剩余工作预算停止。
这是**实施未完成**，不是缺账号/网络/权限的外部阻塞。

先修 N1/N2 并通过同一夹具的受控演练，再接同一学校边界到真实模型 T4；保持每回合 12 模型请求/40 工具/150 秒，最多两回合，分别记录 usage。
不需要再次购买 T1/T2/T3，不能由受控模型成绩替代真实模型对恢复结果的消费和继续执行。

本次不再重判已经接受的通知/课程/计划目标，不要求新增平台或重写 Harness。
学在/智云真人账号、WebVPN 真人登录、浏览器通路仍按此前范围单独标未验证。

## 复跑与下一步

```powershell
apps/api/.venv/Scripts/python.exe -X utf8 tools/audit_regression.py docs/audit/harness-campus-readonly-independent/test_additional_boundaries.py -q -p no:cacheprovider
apps/api/.venv/Scripts/python.exe -X utf8 tools/audit_regression.py docs/audit/harness-campus-readonly-rework-independent -q -p no:cacheprovider
apps/api/.venv/Scripts/python.exe -X utf8 tools/verify_campus_auth_resume.py --out var/<新UUID>/controlled-t4.json
```

新测试当前合计 1 passed/3 failed，是待修复证据，不能并入“全部测试通过”。
具体返工任务见同目录 `rework-task.md`。只重跑受影响项和必要回归，保留首次失败。
