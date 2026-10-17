# 真实模型三项任务结果摘要（已记录 3 次会话）

- 证据集标签：`independent_acceptance`
- 生成时间：2026-10-15T11:10:50.504516+00:00
- 证据 JSON：`docs/audit/harness-independent/live-model.json`
- 原始轨迹：`docs/audit/harness-independent/traces-live/<run_id>.json`
- 产物副本：`docs/audit/harness-independent/artifacts-live/<run_id>/`
- 状态：`partial`；金额：`unknown`（无账单依据）
- Provider：`{'id': 'prv_c68d2878', 'name': 'deepseek', 'model': 'deepseek-flash', 'protocol': 'openai_chat_completions', 'base_url_host': 'api.deepseek.com'}`

## 逐项达标

| 任务 | 会话数 | 达标 |
|---|---|---|
| t1 附件→PDF/XLSX | 1 | 1 |
| t2 课程材料→复习卷→导出 | 1 | 0 |
| t3 缺资料/授权不足 | 1 | 1 |

## 汇总

- 达标会话：2/3
- 本文件只含 3 次复测会话；9 次门槛只适用于 first_9runs，本文件不参与该判定
- 自动失败项：无
- 主 Agent token 合计：{'inputTokens': 16056, 'outputTokens': 7093, 'cacheReadTokens': 355840, 'cacheWriteTokens': 0, 'totalTokens': 378989}


## 逐次会话

| run | 任务 | 状态 | 达标 | 模型调用 | 工具调用 | 秒 | 最终产物 | 失败项 |
|---|---|---|---|---|---|---|---|---|
| t1-1-3cb5b3e1 | t1 | completed | True | 3 | 3 | 29.6 | synthetic-tensile-test-report.pdf, synthetic-tensile-test-report.xlsx | - |
| t2-1-ff0459e9 | t2 | completed | False | 12 | 17 | 56.1 | 合成实验课程复习卷-完整卷.md | worker_succeeded |
| t3-1-9f9ffac1 | t3 | completed | True | 5 | 9 | 27.5 | - | - |

## 预算耗尽而中止的会话（模型调用达到 12 次上限）

| run | 任务 | 模型调用 | 工具调用 | 已受理 job | job 真实终态（turn 结束后的核验窗口） |
|---|---|---|---|---|---|
| t2-1-ff0459e9 | t2 | 12 | 17 | job_556e41c3, job_eff9e725 | {'job_556e41c3': 'succeeded', 'job_eff9e725': 'cancelled'} |

说明：以上会话在模型调用达到 12 次上限时被运行时中止；后台 review_generate 的真实终态见上表（本脚本在 turn 结束后用剩余核验窗口读取，不延长模型/工具预算）。预算是固定上限，未因失败放宽。


## T2 明细（受理 / 同调用等待 / 真实 worker / 试卷）

| run | 预检 | job | wait_seconds 请求 | 本调用内观测到终态 | jobs.read 状态序列 | job 真实终态 | 卷版本 | 题数 |
|---|---|---|---|---|---|---|---|---|
| t2-1-ff0459e9 | True | job_eff9e725 | [14, 14] | False | ['running', 'running', 'running', 'succeeded', 'running'] | {'job_556e41c3': 'succeeded', 'job_eff9e725': 'cancelled'} | v1 | 3 |

本摘要只是本次记录的 3 次真实模型小样本执行记录，不构成最终验收结论，也不声称确定性或生产成功率。
