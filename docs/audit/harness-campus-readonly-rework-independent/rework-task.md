# 交给 Harness：校园只读第二次返工

请先读同目录 `acceptance.md`。上轮 CAS 外层修复、两项 judge 修复、默认离线重判和受控 T4 已独立接受，不重写它们。
继续当前工作树，保留已有修改与旧证据，默认不 commit/push。验收方测试不要修改。

只完成四项：

1. **N1：无认证恢复策略贯穿 WebVPN**。
   现在 allow_auth_recovery=False 只阻止外层 CAS/浏览器，底层 CampusTransport→WebVPNTunnel.ensure_session 仍会登录门户。
   按调用传递禁止认证策略：目标会话有效但门户未认证/已过期时不提交 WebVPN 密码，返回结构化认证状态、保留检查点。
   有效门户会话可复用；用户显式 sync 已授权恢复继续可用。不能全局禁用、串到其他线程/用户，也不能自动换通道重投密码。
   用真实 transport + 合成 portal adapter 测试，不把 CampusClient.request 整层替换掉来规避该分支；补冷却、过期和并发隔离回归。
2. **N2：接通正常登录成功通知**。
   passport `credentials/test-login` 的 ok=true 必须在校方身份验证完成、释放认证锁后通知恢复。
   失败/验证码/仅保存配置不通知。通过正常已有路由演练，无需新增提权路由：waiting_auth → 同目标认证成功 → 不重启 API → 服务器自动续轮。
   保留撤销/无关平台/窄 scope/不重放副作用的负面对照。受控 T4 的重启恢复路径仍保留，不能拿它替代不重启路径。
3. **N3：保护重判原始输入**。
   --rejudge 的输出与任一输入为同一实际文件时，在写入前拒绝并非零退出，原字节不变；覆盖相对/绝对、可用平台上的 symlink/hardlink 别名。
   独立报告文件正常输出，记录原哈希与原判定。不要改旧证据来换测试通过。
4. **补一次真实模型 T4**。
   先让前两项在实际 API+安装的 Harness+受控模型通过，再把同一个合成学校会话/正常可信通知接缝复用到真实模型驱动。
   单次 T4，最多两回合，每回合 12 模型/40 工具/150 秒；分别计量，服务器生成 continuation_turn_id，用户不重发任务，最终答案消费真实探针结果。
   不索取提示词密码、不访问真实校园、不增加生产 Cookie 注入接口。不重复 T1/T2/T3 的收费评测。
   若确有外部条件缺失写清条件；“驱动尚未移植/剩余实现时间不足”属于未完成，不能标为外部阻塞或整轮通过。

独立复现入口：

```powershell
apps/api/.venv/Scripts/python.exe -X utf8 tools/audit_regression.py docs/audit/harness-campus-readonly-rework-independent -q -p no:cacheprovider
```

当前 1 passed / 3 failed。请在正常测试套件补产品回归，同时使独立探针通过；不要删除断言或修改验收方探针。

本次已独立确认基线：API 714 passed/5 既有 skip；根/连接器/启动器 606+87 subtests；旧探针 5/5；受控 T4 49/49；业务链39/39；全面 Harness 集成83次；judge42/42。
修复后先跑受影响项、无重启 T4 与重判保护，再跑必要完整 API/连接器/契约和 Harness 回归。未改 UI 不重跑整套前端。

交付 `docs/audit/harness-campus-readonly-rework2-result.md`；证据放新的 `var/harness-campus-readonly-rework2-<UUID>/`。
列源码哈希、修改文件、前后同输入对照、命令/exit code/耗时、受控及真实模型轨迹、预算/usage和未验证平台，不自行宣布最终验收通过。
