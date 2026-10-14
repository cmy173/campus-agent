# WebVPN 长期登录与自动同步使用指南

## 功能

- 在"隐私与设置 → 校园自动登录（WebVPN）"保存上网账号密码（通行证账号密码不同时可分别填写）。
- 密码以 AES-256-GCM 加密存于本机数据库；服务在后台自动登录 WebVPN、会话被顶掉或过期后自动重登。
- 后台按间隔（默认 30 分钟，可调 5-1440 分钟）同步学院通知；同步失败保留上次缓存。
- 课表与成绩仍按原流程在你登录通行证时更新；经 WebVPN 刷新课表需要后续的 CAS-over-WebVPN 支持，当前未实现。

## 使用步骤

1. 启动服务并登录工作台。
2. 打开"隐私与设置"，在"校园自动登录（WebVPN）"填写：
   - WebVPN 账号 / 密码（上网账号，同 VPN/RVPN）；
   - 通行证账号 / 密码：**与 WebVPN 账号不同时才填**；相同则留空（系统默认复用 WebVPN 账号）。
3. 勾选"自动同步学院通知"（默认勾选），点击"保存并启用自动登录"。
4. 状态行会显示已保存账号、自动同步间隔、上次同步时间与结果；"立即同步"可手动触发，"清除凭据"删除全部保存内容。

## 接口（均为本机会话 + CSRF 保护）

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/api/campus/webvpn/credentials` | 保存加密凭据（webvpn_username/webvpn_password 必填，zjuam_* 可选，auto_sync 可选） |
| GET | `/api/campus/webvpn/status` | 查询凭据与同步状态（不含任何密钥） |
| PATCH | `/api/campus/webvpn/sync-settings` | 修改 auto_sync / sync_interval_minutes |
| POST | `/api/campus/webvpn/sync` | 手动触发一次同步（后台线程执行） |
| DELETE | `/api/campus/webvpn/credentials` | 清除凭据、会话与同步状态 |

## 行为与边界

- 自动同步只做两件事：保持/重建 WebVPN 会话；通过公开连接器拉取学院通知（不需要内网与 CAS）。
- 同一上网账号在别处登录会顶掉本服务会话；后台会在下个周期自动重登（两边会互相顶，属 WebVPN 机制）。
- WebVPN 代理仅登记了 `zjuam.zju.edu.cn`、`zdbk.zju.edu.cn` 两个主机令牌；新增主机需按
  [协议分析](webvpn-analysis.md) 的一次性方法登记。
- 程序化真实登录尚未用真实账号验证；首次保存真实凭据即完成验证，失败会显示映射后的中文错误。

## 安全说明

- 明文密码只存在于服务进程内存（解密瞬间），不落盘、不进日志、不发往模型或第三方。
- 加密密钥由本机 `data/.device_salt`（权限 600）与账号 ID 派生；换机器或删盐值后旧密文不可解，需重填。
- 该功能面向个人设备；共享设备请勿保存凭据。
