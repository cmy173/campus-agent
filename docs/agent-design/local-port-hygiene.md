# 本地端口卫生（验证后必须把机器交还）

## 症状

工作台启动器报 `端口 8000 已被占用。请关闭旧服务或调整启动参数；启动器不会终止其他进程`，
于是看不到应用窗口。原因通常不是"端口没释放"，而是**上一次验证留下的进程还活着**：
test bench、审计脚本或一次被 `Start-Process` 分离出去的 `tools/dev.py`。

## 项目占用的端口

| 端口 | 用途 | 谁启动 |
|---|---|---|
| 5173 | 网页（Vite） | `tools/dev.py` |
| 8000 | API（uvicorn） | `tools/dev.py` |
| 8765 / 8766 | 学在浙大 / 智云课堂连接器 | `tools/dev.py --platforms` |
| 15173 / 18080 / 19191 | 合成验收 test bench（网页 / API / 受控模型） | `tools/round2_testbench.py` |
| 3080 | DeepSeek Harness Web GUI | 不属于本项目，**不要动** |

## 规则

1. **验证进程必须自终止。** 用 `tools/dev.py --ttl-seconds N`（或 `--stop-file`、`--smoke-test`）
   启动，用 `tools/round2_testbench.py --ttl-seconds N`（默认 3600 秒）启动 test bench。
   两者到点或看到 stop-file 都会关闭自己并释放端口。
2. **不要用分离进程长期占用 5173/8000。** 需要给用户留窗口时，让用户从工作台启动器启动；
   自己只在有 TTL/stop-file 的前提下启动。
3. **每个测试会话结束前跑一次**：

   ```powershell
   apps/api/.venv/Scripts/python.exe tools/release_ports.py --all
   ```

   它只处理**命令行走本仓库/本仓库 venv** 的进程（也会顺着父进程链识别 Windows venv
   重新执行基础解释器后那个真正绑定端口的子进程），把整棵树停掉并逐个端口复核；
   外来的监听进程只报告不处理。`--dry-run` 先看会停什么，`--keep-launcher` 保留正在运行的
   `tools/dev.py`。

## 本文件对应的那次清理（2026-10-15）

13:44 的一次审计 + 12:41 启动的 test bench 一直占着 15173/18080/19191，加上我自己分离启动的
`tools/dev.py` 占着 5173/8000/8765，启动器因此无法启动。用
`tools/release_ports.py --all` 停掉两棵树后，七个端口全部复核为空；
随后用 `tools/dev.py --ttl-seconds 45` 做了启动→健康→自动关闭的验证。
