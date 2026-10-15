# 按账号学院匹配通知正文

> **实现位置已迁移。** 学院通知协议现位于 `connectors/campus`（`connectors/college_notices.py`
> 与 `campus_college_sources.py`）；旧 `campus_agent.server` 与 `scripts/verify_college_notices.py`
> 已退役，前端旧页面已由 `/chat` 工作区取代。

更新：2026-10-12。已实现 `ckc_public / college_notices`，试点为竺可桢学院。只读取公开通知，不需要校园 Cookie。

## 使用

1. 登录后在“我的学业”获取本人基本信息。已有缓存时无需重新登录校园站点。
2. 打开“待办与截止”，学院通知区域依据当前账号缓存中的 `college` 字段匹配来源，默认只读本地通知缓存。
3. 点击“同步学院通知”，读取列表首页前 3 条通知的正文和附件链接。
4. 展开正文或查看官方原文，按需要点击“保存到资料库并整理”。浏览器只发送 `record_id`；资料内容、官方来源、平台记录 ID、发布日期、正文版本与结构化附件清单全部由服务端按当前账号缓存重新校验后写入，附件不并入正文。保存后自动执行离线规则提取，需要事件摘要时在“通知整理”中显式同意并使用模型。候选事项仍需人工核对。

同一份内容重复保存由原资料 SHA 去重，重复导入不重新生成候选事项。官方正文变更后再保存会形成新资料与新正文版本，旧资料、旧整理结果和已核对待办保留，用户需核对新旧版本。通知图片和附件仅保留名称与官方链接，标记为未解析，没有下载、OCR 或解析；正文中的缺年日期、相对日期、缺少时间等情况不会自动补造截止时间。学院匹配不是通知资格判定，“全部通知”也可能包括教师、不同年级及不同项目的事项。整理事件、时间与待核对问题的契约和限制见 [通知整理](notice-extraction.md)。

## 来源与真实验证

- [竺可桢学院办公网官方 WebPlus 站点](https://webplus.zju.edu.cn/_s597/main.psp)，页面标题为“竺可桢学院办公网”。教务公开通知也链接此站点，例如 [竺院荣誉证书通知](https://zdbk.zju.edu.cn/jwglxt/xtgl/xwck_ckLoginNews.html?doType=save&xwbh=4F9F1F32F7D28E8BE06329B3CA0A7D51)。
- [全部通知](https://webplus.zju.edu.cn/_s597/qbtz/list.psp)：实际 HTML `ul.news_list`，每项含 `news_title`、`news_meta`。
- 正文固定在 `/_s597/YYYY/MMDD/c<栏目号>a<文章号>/page.psp`，读取 `h1.arti_title`、`arti_update` 与 `wp_articlecontent`。以站点和原始文章号生成稳定 ID；栏目号不同不重复生成文章。
- 附件只接受该官方主机 `/_upload/article/files/…` 下已登记的文件后缀；不请求附件、页面脚本、图片、正文中的外站链接或跳转地址。

2026-10-12 在跟随系统策略下，用无 Cookie 请求读取了 3 条真实通知正文、7 个附件链接；离线、断开与重启后的缓存读取通过。完整元数据见 [验证记录](college-notices-validation-2026-10-12.json)。验证用临时数据库和合成的本地账号/学院字段，不是新的真实校园登录验证。自动化测试使用合成 HTML 和模拟传输。

本机直连 WebPlus 超时，跟随系统成功；原 `ckc.zju.edu.cn`、`office.ckc.zju.edu.cn` HTTPS 入口在本机出现 TLS 失败。没有关闭证书验证或自动换通道，采用已由官方页面确认的 WebPlus HTTPS 来源。该结果不代表其他用户的网络必然可达。

## 账号匹配与接口

前端和模型仅能提供 `limit`（整数 1–5，默认 3）。`owner_id`、学院、URL、Cookie 都不是查询参数。

```python
from campus_agent.connector_contracts import UserContext, CollegeNoticesQuery

context = UserContext(authenticated_account['owner'])
service = app.state.campus_service
cached = service.query('college_notices', CollegeNoticesQuery(), context)
updated = service.sync('college_notices', CollegeNoticesQuery(), context)
```

HTTP 入口复用 `GET /api/campus/query/college_notices` 和 `POST /api/campus/sync/college_notices`，后者请求体为 `{"query":{"limit":3}}`，使用现有登录和 CSRF 校验。

结果除统一时间、完整性和错误字段外，新增可选 `selection`：账号学院、匹配状态、学院字段的获取时间和是否过期。学院依据来自本人的教务/ETA 服务端缓存，按字段时间选择更新证据；更新为空或同时间冲突时停止匹配。只清理空白字符并匹配登记的学院名/别名，不根据专业、姓名或班级猜测学院，不在用户请求中传入院系地址。

当前匹配“竺可桢学院”及“浙江大学竺可桢学院”。其他学院、缺失学院、歧义分别返回 `unsupported`、`missing`、`ambiguous` 的选择状态和标准 `UNSUPPORTED` 错误，不回退竺院、不访问网络，也不返回该账号以前其他学院的通知缓存。过期学院信息可以读取已有公开来源，但界面说明匹配依据已过期并提供更新入口。

通知记录包含稳定 ID、学院、标题、发布日期、纯文本正文、正文 SHA-256、官方来源、受限附件链接以及未读图片/附件提示。HTML 不进入可执行 DOM，存储的正文没有脚本内容；通知中的文字也不会作为工具指令执行。

## 同步和网络边界

缓存键含账号、平台、能力、匹配学院和 `limit`；选择来源与同步在同一账号锁内完成。当前是首页有限窗口，始终返回 `completeness=partial`，不提供历史分页或增量游标。

同步逐篇读取，最多 1 个列表请求和 5 个正文请求。中途超时、限流或协议变化时立即停止，保留已成功的新正文和旧缓存，并同时返回结构化错误；下一次显式同步从首页重新读取，通过原始文章号去重。空列表不会清空缓存，也不会标记获取成功。缩小 `limit` 使用独立缓存范围。

新公开页面传输集中在 `campus_network.PublicPageTransport`，配置 `CAMPUS_PUBLIC_NETWORK_POLICY=system|direct`，默认 `system`。它只影响本次新增的公开学院通知读取；CAS、教务、原公开教务摘要继续使用 `CAMPUS_NETWORK_POLICY`。没有 CookieJar、重定向跟随、自动重试或自动通道切换。超时 10 秒/请求、响应上限 2 MB、正文上限 100000 字符，复用按主机的请求限额。

设置只对新启动的进程生效，`.env.example` 不会自动载入。直连仍依赖系统路由，不能保证绕过 TUN；浏览器 WebVPN 会话不被继承。

## 扩展与尚未验证

新增学院需先核实其官方列表、正文和访问条件，在 `campus_college_sources.py` 登记严格来源范围，新增适配器并在注册表注册。适配器的 `ConnectorSpec.college_names` 声明准确名称/别名，应用服务根据此声明匹配，前端没有按学院写判断分支。不要仅因院系名称已知就注册一个没有真实协议的连接器。

尚未完成：其他院系、全量历史、个性化权限通知、自动订阅、后台定时同步、附件与图片内容、通知资格自动判定，以及真实标注语料上的待办日期准确率评估。真实账号学院字段刷新和校内认证流程本次没有重新提交密码验证。

复现公开验证：`python scripts/verify_college_notices.py --output <报告路径>`。通知整理与人工核对字段清单的复现：`python scripts/verify_notice_extraction.py --keyword <关键词> --output <报告路径>`，取得授权密钥后加 `--use-model` 记录一次真实模型调用。两者只运行公开请求，读取临时数据库与合成学院字段，不读取用户实际资料库；不要把这两个脚本放入自动化测试。
