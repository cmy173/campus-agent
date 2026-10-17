# 真实模型三项任务结果摘要（已记录 3 次会话）

- 证据集标签：`t2_retest_after_fix`
- 关联：first_9runs → `docs/audit/harness-next-round/live-model.json`
- 关联：t2_retest_before_fix → `docs/audit/harness-next-round/live-model-retests.json`
- 生成时间：2026-10-15T09:36:56.302446+00:00
- 证据 JSON：`docs/audit/harness-next-round/live-model-t2-retest.json`
- 原始轨迹：`docs/audit/harness-next-round/traces-live/<run_id>.json`
- 产物副本：`docs/audit/harness-next-round/artifacts-live/<run_id>/`
- 状态：`partial`；金额：`unknown`（无账单依据）
- Provider：`{'id': 'prv_c68d2878', 'name': 'deepseek', 'model': 'deepseek-flash', 'protocol': 'openai_chat_completions', 'base_url_host': 'api.deepseek.com'}`

## 逐项达标

| 任务 | 会话数 | 达标 |
|---|---|---|
| t1 附件→PDF/XLSX | 0 | 0 |
| t2 课程材料→复习卷→导出 | 3 | 0 |
| t3 缺资料/授权不足 | 0 | 0 |

## 汇总

- 达标会话：0/3
- 9 次门槛（≥8 且每项 ≥2/3）：未满足
- 自动失败项：无
- 主 Agent token 合计：{'inputTokens': 16456, 'outputTokens': 6196, 'cacheReadTokens': 656128, 'cacheWriteTokens': 0, 'totalTokens': 678780}


## 逐次会话

| run | 任务 | 状态 | 达标 | 模型调用 | 工具调用 | 秒 | 最终产物 | 失败项 |
|---|---|---|---|---|---|---|---|---|
| t2-1-aae78a85 | t2 | completed | False | 12 | 13 | 34.0 | - | worker_succeeded, paper_read, export_published, export_content |
| t2-2-81bc1cc5 | t2 | completed | False | 12 | 13 | 31.9 | - | paper_read, export_published, export_content |
| t2-3-77552470 | t2 | completed | False | 12 | 14 | 35.9 | - | worker_succeeded, paper_read, export_published, export_content |

## 预算耗尽而中止的会话（模型调用达到 12 次上限）

| run | 任务 | 模型调用 | 工具调用 | 已受理 job | job 真实终态（turn 结束后的核验窗口） |
|---|---|---|---|---|---|
| t2-1-aae78a85 | t2 | 12 | 13 | job_45d88e53 | {'job_45d88e53': 'succeeded'} |
| t2-2-81bc1cc5 | t2 | 12 | 13 | job_16a3f58d | {'job_16a3f58d': 'succeeded'} |
| t2-3-77552470 | t2 | 12 | 14 | job_989b7c1f | {'job_989b7c1f': 'succeeded'} |

说明：以上会话的模型调用刚好用满 12 次上限，后台 review_generate 仍在运行；本脚本在 turn 结束后用剩余核验窗口读取 job 真实终态（不延长模型/工具预算）。预算是固定上限，未因失败放宽。


## T2 明细（受理 / 同调用等待 / 真实 worker / 试卷）

| run | 预检 | job | wait_seconds 请求 | 本调用内观测到终态 | jobs.read 状态序列 | job 真实终态 | 卷版本 | 题数 |
|---|---|---|---|---|---|---|---|---|
| t2-1-aae78a85 | True | job_45d88e53 | [6] | False | ['running', 'running', 'running', 'running'] | {'job_45d88e53': 'succeeded'} | v1 | 3 |
| t2-2-81bc1cc5 | True | job_16a3f58d | [6] | False | ['running', 'running', 'running', 'succeeded'] | {'job_16a3f58d': 'succeeded'} | v1 | 3 |
| t2-3-77552470 | True | job_989b7c1f | [6] | False | ['running', 'running', 'running', 'running'] | {'job_989b7c1f': 'succeeded'} | v1 | 3 |

本摘要只是本次记录的 3 次真实模型小样本执行记录，不构成最终验收结论，也不声称确定性或生产成功率。
