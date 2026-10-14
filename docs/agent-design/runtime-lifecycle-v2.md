# V2 运行与停止语义

日期：2026-10-14。当前覆盖 Windows 前台轮次及应用管理的进程；持久目标、调度和跨轮子任务仍在建设。

## 状态合同

轮次增加 stopping：停止请求立即撤销运行时授权，阻止新的模型/工具调用；收尾期间不允许该会话启动下一轮。SDK、浏览器及在途 API 操作结束后才进入 cancelled 或原超时原因对应的 failed。迟到的成功/失败不能覆盖 stopping。

SDK和浏览器并行停止，原有业务调用锁作为收尾屏障。失败保留 stopping/stop_incomplete，界面可重试；新的 stop_requested 对应新尝试。重启时尚在 stopping 的轮次标 interrupted，不冒称已取消。前端轮询包括 stopping，工具不提前标中断；取消响应更新前取消旧查询，避免旧 running 覆盖新停止状态。

## 进程归属

Windows Job Object 管理自有 Node 及普通派生进程，仅设置 KILL_ON_JOB_CLOSE，不增加文件、网络、CPU或内存限制。Node入口等待任务输入，API在提供授权/输入前绑定归属；正常根进程退出也清理留下的子进程。终止后查询实际活动进程数至零，再结束收尾。

仅管理本应用创建的进程，不按名称终止用户其他进程。真实双组验证：结束A根进程后其detached子进程未写延迟标记，B仍写出结果。其他系统服务、远端任务及经系统代理创建的独立进程不因此自动可逆，已有提交也不因停止变为未提交。

接口依据：[Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)、[Job accounting](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information)。目标环境为当前Windows，不用这些证据宣称其他系统已验收。

## 浏览器脚本

旧实现关闭页面/客户端约0.34秒后返回BROWSER_STOPPED，但延迟JavaScript仍发请求。现在保留脚本能力，停止时正常关页面后结束含脚本的自有进程组；CPU循环无法响应时由API强制收尾。

签名活动帧标识当前调用，普通stdout/无签名JSON不改变控制状态。停止控制最多等待2秒，随后按自有归属结束无法响应的进程；异常重启清理旧句柄。强制终止需要重新打开浏览器，最新未落盘的页面状态不保证保留。

共享浏览器仍按请求串行，和其他轮次竞争时保持停止中等待；它不是每个子Agent的独立浏览器。跨轮continuable、子Agent浏览器调用独立归属、持久Goal和调度仍待实施，不能用root取消替代。

## 证据

- 修复前var/v2-browser-stop-townctm_/evidence.json：停止后真实延迟操作执行，验证失败。
- 延迟脚本var/v2-browser-stop-qc445dvm/evidence.json：约0.344秒、进程归零、无延迟操作。
- CPU循环及普通/无签名stdout：var/v2-browser-stop-paap5jde/evidence.json，约2.063秒、进程归零。
- detached与兄弟独立：var/v2-owned-process-o2_s_igs/evidence.json。
- API/Harness：var/agent-integration-wc8y93uf/evidence.json，71次受控模型调用，实际脚本取消、stopping阻止新轮次、结束后恢复。
- UI：var/agent-integration-1023p1_9/root-stop-ui-evidence.json，停止中/禁用按钮DOM、最终截图、刷新恢复、宽375≤390；stop_fixture.late=false。过渡截图绘制滞后于DOM，不用它证明视觉过渡。

API294、前端25文件142及构建通过。模型受控，未执行付费试用。
