# 校园认证 / 学在浙大会话 / WebVPN 增量复核

审计：2026-10-15（Asia/Shanghai）。只读源码与合成测试，没有读取个人 secrets、真实浏览器 profile、Cookie 数据库或业务缓存；没有学生凭据、学校认证请求或 SSO/MFA 绕过。

## 基线与范围

开始时工作树已有用户未提交修改，全部保留，未 reset/checkout：

- `connectors/browser/controlled_login.py` +102 行：Windows 浏览器 temp/profile 可写目录与 fallback。
- `connectors/browser/session_runner.py` +2 行：使用 writable_profile_dir。
- `connectors/tests/test_controlled_browser.py` +73 行：目录逻辑测试。

API/runtime/前端已有修改归其他成员。本成员对实际代码增量优化，未改 API。校园 transport 原有默认 direct、固定 HTTPS host/port allowlist、no-redirect、issuer-bound Cookie、有限 CAS/ETA 跳转均保留。没有关闭 TLS 或新增密码保存/自动密码重试。

## 实际修改

| 修复 | 位置与行为 |
|---|---|
| API 跟随认证重定向/发送 token 的风险 | `adapters/transport.py:RequestsTransport.request` 设置 allow_redirects=False；相对路径护栏拒绝协议/authority、控制字符、反斜杠与 fragment，且在 header/token callback 前拒绝。`adapters/xzzd.py:_request/check_connection` 与 `adapters/zykt.py:_request/_request_text/_check_state` 将 301/302/303/307/308 映射 auth-required/AUTH_EXPIRED，恢复 action 不丢失。 |
| 任意 JSON 冒充有效会话 | `xzzd.py:check_connection` 必须有对象内 courses/list/data 数组（空数组允许）；JSON code401/419 是 auth-required，success=false/未知结构不成功。`browser/controlled_login.py:_READ_JS` 同样要求已知数组证据。classify_read_result 优先识别重定向JSON及401/419。 |
| 陈旧JWTUser免验证/强制登录入口 | `controlled_login.py:run_verify` 有无标记均先到平台首页复用官网/学校SSO，之后只读探针，不提交凭据。`_wait_for_session` 在成功/超时/异常后关闭自己的后台探针tab。 |
| Cookie 属性丢失/跨平台复用 | 新增 `browser/session_cookies.py:_requests_session`，仅当前 courses/classroom 域与适用父域，父域收窄到当前API host；排除CAS master iPlanetDirectoryPro、其他平台、未登记域与过期异常条目，保留path/Secure/expiry/HttpOnly。BrowserSession和A17探针共用此逻辑，trust_env=False 避免隐式代理/.netrc，仅显式STUDY_PLATFORM_PROXY，TLS保持默认。 |
| 停止后旧内存状态继续可用 | `session_runner.py:BrowserSession.pump_once/cookies_snapshot` 防御性复制；stop清snapshot，不删除profile。`campus/campus_network.py:CampusTransport.close` 清jar及authenticated/passport_authenticated/eta_authenticated；旧client不再声称已认证。 |
| 原始异常内容进入响应/证据文件 | `controlled_login.py:run_verify/run_logout_test/run_reset_session` 和 `session_runner.py:handle/run_sync/run_fetch` 的意外异常仅输出类型，不回显ticket/proxy/cookie异常文本。用户原有可写目录错误汇总逻辑未删除。 |
| WebVPN假恢复、失败未撤销、虚构24h保证 | `campus/webvpn_adapter.py:restore_cookies/probe/is_session_valid` 原子替换jar且须实际probe；拒绝空/过期/异常域pathexpiry，未知或负面JSON/登录重定向/过量响应/异常均fail-closed。取消未经确认24h保证，仅显式success=true接受。 |
| WebVPN静默直通/目标被改写 | `_parts/proxy_url/open` 未认证或未登记不direct fallback；拒绝userinfo、非法scheme、非默认端口、控制字符、反斜杠、fragment；保留path/query，仅已有两项host token，不猜令牌。默认采用campus direct/system policy与no-redirect，不循环。 |
| WebVPN身份和Cookie边界 | `_WebVPNCookiePolicy/login/close` portal issuer-bound、jar每实例独立、账号切换前清原身份，未知服务端/网络错误不回显。兼容get_cookies_dict仅内存传递接口，明确禁止日志/持久化，未新增存储调用。 |

## 三套会话与生产入口边界

1. CampusManager按owner管理CAS/ZDBK/ETA内存jar。**2026-10-15 更新**：`connectors/campus/webvpn_tunnel.py` 已把 WebVPN 接入生产 `CampusTransport`——按主机选择通道（默认 `CAMPUS_WEBVPN_MODE=auto`：仅在已保存 WebVPN 凭据且主机 TCP 探测不可达时切换），portal 会话仅存内存、失败一次进 300s 冷却、过期只重认证一次；未登记主机（eta）与未保存凭据的情况仍然只走直连，`check_url` 依旧拒绝 portal URL 作为目标。WebVPN 账号与校园通行证在凭据保险箱里是两个独立槽位。真实 portal 登录与真实离校路由仍未实测（无真实账号/离校网络），不能称生产校园VPN全链打通。
2. 旧学在浙大/智云BrowserSession使用独立controlled-browser持久profile，API snapshot仅内存。check失效最多一次官网/SSO刷新及接口复核；sync/fetch/list_courses本轮没有新增全量自动恢复重放。
3. 助手BrowserHost由Node `apps/agent-runtime/browser-host.mjs` 使用CAMPUS_BROWSER_HOME/profile，与旧profile路径/实例隔离，不能因校园disconnect静默删除。

基线API校园disconnect调用CampusManager.disconnect，pop owner state并client.close；clear_cache=true另删snapshot/capability缓存。不等于学校服务端logout，不清两套浏览器持久profile。本成员仅加强close flags。

**lead协作范围**：正在新增trusted-owner connect-browser桥：明确授权后私有内存采纳助手浏览器快照、connect_zdbk真实probe与identity核对；授权仅进程内、只读query auth错误才retry1、失败冷却、disconnect revoke。这是授权快照传递，不是三个profile自动共用，其实现/测试由lead报告。本成员不声明WebVPN跨桥或ETA全任务恢复支持。

## Mock可核实请求次数

新增 `connectors/tests/test_auth_vpn_followup.py` 共64条参数化Mock测试。测试不仅检查state，也明确断言：

| 合成场景 | API请求 | 浏览器refresh | connector程序化登录 | 结果 |
|---|---:|---:|---:|---|
| 无snapshot，Mock官网刷新后出现可用snapshot | 1 | 1 | 0 | ok |
| 已有有效snapshot | 1 | 0 | 0 | ok |
| 陈旧平台snapshot，Mock学校SSO刷新恢复 | 2 | 1 | 0 | ok |
| 刷新后仍失效 | 2 | 1 | 0 | auth_required，不循环 |

login=0仅证明连接器没有提交凭据/重复程序化登录，**不是首次真实学生认证零交互**；首次无校园会话仍须用户在官方页面完成密码/验证码/MFA等认证。

Standalone WebVPN：显式login一次=GET登录页+POST do-login共2请求；probe+业务请求后累计4，login POST仍1；失效probe累计5，之后open本地拒绝，无第6请求/自动重登。这不是学生实测。

## 实际测试结果

根目录 `.venv` Python3.12.10缺pytest（No module named pytest）；系统Python3.12.10/pytest9.0.3先跑通过205 tests+38 subtests，显式排除了17条临时目录相关用例。直接跑含下载套件曾9 failed,114 passed,7 subtests failed，均为Windows临时目录WinError5；TEMP/TMP改仓库专用目录仍失败。一个新增Mock替身cookies不是list的测试错误也曾修正，未隐瞒中间失败。

**最终使用lead的隔离runner完成含下载的完整相关套件，无排除：**

```powershell
.\apps\api\.venv\Scripts\python.exe tools/audit_regression.py connectors/tests/test_auth_vpn_followup.py connectors/tests/test_controlled_browser.py connectors/tests/test_session_reuse.py connectors/tests/test_network_routing.py connectors/tests/test_session_runner.py connectors/tests/test_xzzd.py connectors/tests/test_zykt.py connectors/tests/test_transport.py tests/test_campus_network.py tests/test_passport_recovery.py tests/test_campus.py tests/test_campus_profiles.py -q -p no:cacheprovider --tb=short
```

**306 passed, 67 subtests passed in 22.31s**。包含TestFetchResource、TestServiceSharedCache、实际loopback下载原子写入/大小护栏测试。runner只在本次UUID合成树兼容Windows mkdir(mode700)，不改生产ACL，DB/files/secrets/campus目录均隔离。不读取真实data。

新增测试全部Mock，不启动真实浏览器/访问学校。现有部分测试用本机loopback服务；校园网络测试在HTTPSHandler.do_open前中止，验证直连策略而不触学校网络。用户原有目录fallback测试及CAS Cookie issuer/CSRF/有限跳转旧测试均在通过范围。

`python -m compileall -q` 本轮修改模块：通过。`git diff --check -- connectors`：通过。

## 限制与后续验收

- 无学生凭据；未确认WebVPN当前/user/info完整schema、实际登录、Cookie服务端寿命、离校直连/VPN实际效果或server host token变化。probe宁可拒绝未知schema，需明确授权后的官方只读实测，不能拿历史注释代替验收。
- RequestsTransport.download signed-media/CDN重定向沿用旧行为，本轮no-redirect仅API request；下载护栏回归已通过，不等于CDN目标安全策略审计完成。
- 持久浏览器本身会保存会话；不能承诺浏览器Cookie绝不落盘。未导出Cookie值/保存密码。用户原有var/controlled-browser fallback属敏感目录，应排除提交上传；推荐可用时指定仓库外STUDY_BROWSER_PROFILE_DIR。目录fallback用户修改完整保留。
- 自定义WebVPN opener是调用方明确提供的transport，可改变默认policy；生产传入自定义opener需保持no-redirect/issuer隔离。兼容Cookie内存导出若新增调用方，必须遵守不日志/不持久化。
- disconnect只撤销本地校园连接，不替代学校logout或静默清另一profile；独立浏览器退出必须用户确认，UI应明确语义。
- ETA复认证后整段任务/成绩课程全量批次续跑、生产WebVPN自动选择，本成员未证明，不能宣布全链完成。
