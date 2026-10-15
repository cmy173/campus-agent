# 本地 REST API

v0.2 仅允许回环地址访问，无 CORS。写请求需 `X-Campus-Request: 1`；已登录写请求还需 `X-CSRF-Token`，从登录或 `/api/me` 获取。会话 cookie 为 HttpOnly、SameSite=Strict，12 小时失效。

| 方法 / 路径 | 功能 |
|---|---|
| GET /api/health | 本机健康状态 |
| POST /api/auth/login | JSON student_id、password；用校园通行证（浙大统一身份认证）登录，首次成功登录自动建立账号 |
| POST /api/auth/logout | 撤销当前会话 |
| GET /api/me | 学号与 CSRF token |
| GET /api/model | 模型配置状态，不返回密钥 |
| POST /api/documents | 原始字节；X-Filename 为 URL 编码名称，X-Document-Kind 为 notice/material |
| GET /api/documents | 当前账号资料与状态 |
| GET /api/documents/{id} | 资料详情与带位置的 chunks |
| GET /api/documents/{id}/download | 下载原件 |
| DELETE /api/documents/{id} | 级联删除原件、片段、待办 |
| GET /api/tasks | 当前账号全部待办 |
| PATCH /api/tasks/{id} | JSON state:pending/done、due:带时区 ISO 日期或 null；标记已核对 |
| POST /api/chat | JSON query、可选 use_model；返回 answer/citations/steps/mode 及可选 warning/tasks |
| GET /api/activities?target=2 | 目标 0–30 的虚构推荐 |
| GET /api/audit | 最近 50 条本账号操作记录 |
| DELETE /api/data | 清除本账号资料、派生内容和审计，保留账号 |

上传首次返回 202，同账号相同内容返回 200 和 duplicate=true。状态 queued → processing → ready/failed。重启时未完成任务标为失败，可删除重传。相同内容去重不修改类型，选错资料类型时删除后重传。

每用户最多 100 份资料，全局最多 10 个排队/解析任务；两条线程调度解析进程，单进程超时 30 秒。原件存于 SQLite BLOB，派生内容通过外键关联。

身份来自服务端会话，不信任请求中的 owner_id。无权对象返回 404。文件内指令不会触发工具或网络动作。错误返回 detail 字符串。

登录时学号与统一身份认证密码只在本机后端用于向 `zjuam.zju.edu.cn` 的 CAS 换取一次性票据；密码不写库、不写盘、不写日志，认证失败只尝试一次（避免触发风控锁号）。因此登录需要能访问统一身份认证（一般在校园网 / RVPN 下）。账号即学号，资料按学号隔离。

当前 cookie 在本机 HTTP 下不设置 Secure，不能直接照搬公网；数据库不加密，解析子进程不是操作系统沙箱。详见 security.md。
