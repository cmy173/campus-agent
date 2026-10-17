# 真实模型三项任务结果摘要（已记录 3 次会话）

- 证据集标签：`t2_final_after_bounded_wait_fix`
- 关联：first_9runs → `docs/audit/harness-next-round/live-model.json`
- 关联：t2_retest_after_first_fix → `docs/audit/harness-next-round/live-model-t2-retest.json`
- 生成时间：2026-10-15T09:37:23.596938+00:00
- 证据 JSON：`docs/audit/harness-next-round/live-model-t2-final.json`
- 原始轨迹：`docs/audit/harness-next-round/traces-live/<run_id>.json`
- 产物副本：`docs/audit/harness-next-round/artifacts-live/<run_id>/`
- 状态：`partial`；金额：`unknown`（无账单依据）
- Provider：`{'id': 'prv_c68d2878', 'name': 'deepseek', 'model': 'deepseek-flash', 'protocol': 'openai_chat_completions', 'base_url_host': 'api.deepseek.com'}`

## 逐项达标

| 任务 | 会话数 | 达标 |
|---|---|---|
| t1 附件→PDF/XLSX | 0 | 0 |
| t2 课程材料→复习卷→导出 | 3 | 1 |
| t3 缺资料/授权不足 | 0 | 0 |

## 汇总

- 达标会话：1/3
- 9 次门槛（≥8 且每项 ≥2/3）：未满足
- 自动失败项：无
- 主 Agent token 合计：{'inputTokens': 19202, 'outputTokens': 5815, 'cacheReadTokens': 665728, 'cacheWriteTokens': 0, 'totalTokens': 690745}


## 逐次会话

| run | 任务 | 状态 | 达标 | 模型调用 | 工具调用 | 秒 | 最终产物 | 失败项 |
|---|---|---|---|---|---|---|---|---|
| t2-1-bfe45cde | t2 | completed | False | 12 | 14 | 33.4 | - | export_published, export_content |
| t2-2-1086b7d2 | t2 | completed | False | 12 | 13 | 38.9 | - | worker_succeeded, paper_read, export_published, export_content |
| t2-3-7feafb3e | t2 | completed | True | 12 | 13 | 29.8 | 合成实验课程 复习卷-完整卷.md | - |

## 预算耗尽而中止的会话（模型调用达到 12 次上限）

| run | 任务 | 模型调用 | 工具调用 | 已受理 job | job 真实终态（turn 结束后的核验窗口） |
|---|---|---|---|---|---|
| t2-1-bfe45cde | t2 | 12 | 14 | job_8980ed1c | {'job_8980ed1c': 'succeeded'} |
| t2-2-1086b7d2 | t2 | 12 | 13 | job_779ef507, job_b64b8034 | {'job_779ef507': 'succeeded', 'job_b64b8034': 'cancelled'} |
| t2-3-7feafb3e | t2 | 12 | 13 | job_194a1d2b | {'job_194a1d2b': 'succeeded'} |

说明：以上会话在模型调用达到 12 次上限时被运行时中止；后台 review_generate 的真实终态见上表（本脚本在 turn 结束后用剩余核验窗口读取，不延长模型/工具预算）。预算是固定上限，未因失败放宽。


## T2 明细（受理 / 同调用等待 / 真实 worker / 试卷）

| run | 预检 | job | wait_seconds 请求 | 本调用内观测到终态 | jobs.read 状态序列 | job 真实终态 | 卷版本 | 题数 |
|---|---|---|---|---|---|---|---|---|
| t2-1-bfe45cde | True | job_8980ed1c | [6] | False | ['running', 'running', 'running', 'running', 'succeeded'] | {'job_8980ed1c': 'succeeded'} | v1 | 3 |
| t2-2-1086b7d2 | True | job_b64b8034 | [6, 6] | False | ['running', 'running', 'running'] | {'job_779ef507': 'succeeded', 'job_b64b8034': 'cancelled'} | v1 | 3 |
| t2-3-7feafb3e | True | job_194a1d2b | [6] | False | ['running', 'succeeded'] | {'job_194a1d2b': 'succeeded'} | v1 | 3 |

本摘要只是本次记录的 3 次真实模型小样本执行记录，不构成最终验收结论，也不声称确定性或生产成功率。
