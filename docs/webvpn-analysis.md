# WebVPN 登录与代理协议分析

分析日期：2026-10-12。来源：登录页内联脚本原文、已认证会话中的真实跳转 URL、门户页面 DOM。
所有结论标注了证据类型；未做真实账号的程序化登录验证（见文末）。

## 账号体系（双层认证）

- **WebVPN 账号**：上网账号（同 VPN/RVPN 账号），只解决"内网通道"。
- **通行证账号（CAS/zjuam）**：进入 CAS 类站点（教务、ETA 等）还需要第二层通行证验证。
- 两者多数同学相同（学号），但**可能不同**，因此凭据表分开保存两套账号密码。
- 实测确认：通过 WebVPN 打开教务系统时落到 zjuam 的 CAS 登录页，需要再次通行证认证。

## 登录协议

- 登录页：`https://webvpn.zju.edu.cn/login`（jQuery 老页面，内联脚本）
- 提交：`$.post('/do-login', <form serializeArray>)` → **form-urlencoded**，字段：
  - `user_name`（DOM input name 实测）
  - `password`（下方加密后的 hex 串）
- 响应 JSON：成功 `{success: true, url: <跳转地址>}`；失败 `{success: false, error: <code>}`。

### 密码加密（登录页内联脚本原文）

```js
var textRightAppend = function (text, mode) {
    var segmentByteSize = mode === 'utf8' ? 16 : 32
    if (text.length % segmentByteSize === 0) { return text }
    var appendLength = segmentByteSize - text.length % segmentByteSize
    var i = 0
    while (i++ < appendLength) { text += '0' }   // 补字符 '0'
    return text
}
var encrypt = function (text, key, iv) {
    var textLength = text.length
    text = textRightAppend(text, 'utf8')
    var keyBytes = utf8.toBytes(key)
    var ivBytes = utf8.toBytes(iv)
    var textBytes = utf8.toBytes(text)
    var aesCfb = new AesCfb(keyBytes, ivBytes, 16)
    var encryptBytes = aesCfb.encrypt(textBytes)
    return hex.fromBytes(ivBytes) + hex.fromBytes(encryptBytes).slice(0, textLength * 2)
}
// 调用点：data[i].value = encrypt(data[i].value, "wrdvpnisawesome!", "wrdvpnisawesome!")
```

即：AES-128-CFB，key = iv = `wrdvpnisawesome!`，补 `'0'` 到 16 的倍数，
输出 = `hex(iv) + hex(密文)[:len(原文)*2]`（hex，按原文长度截断）。
本项目实现：`campus_agent/campus_crypto.py::webvpn_encrypt_password`，
并用独立重实现做了对拍测试（tests/test_webvpn_credentials.py）。

## 代理 URL 格式（重要）

**不是透明代理**。早期"直接访问校内域名即走 WebVPN"的观察是错的：
那台机器本身可达校园网，浏览器是直连。真实代理格式为 URL 重写：

```
https://webvpn.zju.edu.cn/https/<host-token>/<原路径>?<原查询>
```

`host-token` 样例（已认证会话实测）：

| 主机 | token |
|---|---|
| zjuam.zju.edu.cn | `77726476706e69737468656265737421eafd549d2a7e725a6b468ca88d1b203b` |
| zdbk.zju.edu.cn | `77726476706e69737468656265737421eaf34397692a6245300d8db9d6562d` |

token 结构 = `hex("wrdvpnisawesome!") + hex(主机名密文)[:len(主机名)*2]`，
与密码加密同构，但其主机名密文**未能用任何标准 AES 配置离线复现**
（ECB/CBC/CFB/OFB/CTR/CFB8 × 多种 IV/填充 × 加解密原语全部排除；
密钥流分析确认是流式结构但 ks1 不等于 E/D(key/0)）。
推测门户使用了未公开混淆的自定义 AesCfb 变体或另一组密钥材料。

**因此本项目采用"已知主机令牌表 + 运行时校验"**：
`campus_agent/webvpn_adapter.py::HOST_TOKENS` 登记上表主机；
代理请求若返回 WebVPN 错误页（如 HOST_NOT_FOUND）会暴露为清晰的连接错误。
新增主机的登记方法：浏览器登录 WebVPN 后，在门户地址框输入主机回车，
从地址栏复制 `/https/` 后的 token 段加入表内（token 对同一主机是确定值）。

被代理页面还会调用 `/wengine-vpn/cookie?method=get&host=...&scheme=...&path=...`
做原域 Cookie 桥接；程序化客户端不需要模拟该桥接（Cookie 由 WebVPN 域统一管理）。

## Session 与心跳

- 登录后 WebVPN 域 Cookie：`route`（会话）、`heartbeat`、`show_*` 等。
- 会话有效期未实测确认；适配器默认按 24 小时估计，并用 `/user/info` 探针
  （`WebVPNAdapter.probe()`）做服务端真实性校验，过期即用存储凭据重新登录。
- 同一 WebVPN 账号在新地点登录会踢掉旧会话（实测：浏览器会话被用户手动顶掉后，
  旧 Cookie 立即失效）——多端共用一个上网账号时自动重登会互相踢，属预期行为。

## 验证状态

- ✅ 登录页脚本原文、DOM 字段名、响应结构：真实页面取证。
- ✅ 代理 URL 格式与两个主机 token：真实已认证会话取证。
- ✅ 加密/解密、令牌 URL 构造、凭据 API：自动化测试（11 项）。
- ⚠️ **程序化真实登录未验证**：分析会话被顶掉前未用真实账号跑通
  `WebVPNAdapter.login()`；首次在 UI 保存真实凭据时即完成该验证，
  若失败会显示映射后的错误文案（账号密码错误/锁定/限流等）。

## 参考

- WebVPN 使用说明：https://itc.zju.edu.cn/_t2014/2021/0615/c49798a2395843/page.psp
- 故障反馈：xwmaster@zju.edu.cn
