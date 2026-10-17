# 真实模型三项任务结果摘要（已记录 3 次会话）

- 证据集标签：`t2_after_wait_window_14s`
- 关联：first_9runs → `docs/audit/harness-next-round/live-model.json`
- 关联：t2_retest_after_first_fix → `docs/audit/harness-next-round/live-model-t2-retest.json`
- 关联：t2_final_after_bounded_wait_fix → `docs/audit/harness-next-round/live-model-t2-final.json`
- 生成时间：2026-10-15T09:40:58.915681+00:00
- 证据 JSON：`docs/audit/harness-next-round/live-model-t2-final2.json`
- 原始轨迹：`docs/audit/harness-next-round/traces-live/<run_id>.json`
- 产物副本：`docs/audit/harness-next-round/artifacts-live/<run_id>/`
- 状态：`partial`；金额：`unknown`（无账单依据）
- Provider：`{'id': 'prv_c68d2878', 'name': 'deepseek', 'model': 'deepseek-flash', 'protocol': 'openai_chat_completions', 'base_url_host': 'api.deepseek.com'}`

## 逐项达标

| 任务 | 会话数 | 达标 |
|---|---|---|
| t1 附件→PDF/XLSX | 0 | 0 |
| t2 课程材料→复习卷→导出 | 3 | 2 |
| t3 缺资料/授权不足 | 0 | 0 |

## 汇总

- 达标会话：2/3
- 9 次门槛（≥8 且每项 ≥2/3）：未满足
- 自动失败项：无
- 主 Agent token 合计：{'inputTokens': 27625, 'outputTokens': 5829, 'cacheReadTokens': 631168, 'cacheWriteTokens': 0, 'totalTokens': 664622}


## 逐次会话

| run | 任务 | 状态 | 达标 | 模型调用 | 工具调用 | 秒 | 最终产物 | 失败项 |
|---|---|---|---|---|---|---|---|---|
| t2-1-f73871b7 | t2 | completed | True | 12 | 15 | 44.6 | 《合成实验课程》复习卷-完整卷.md | - |
| t2-2-5583e89e | t2 | completed | False | 12 | 14 | 38.5 | - | worker_succeeded, paper_read, export_published, export_content |
| t2-3-fc27eaeb | t2 | completed | True | 10 | 11 | 47.2 | 合成实验课程 复习卷-完整卷.md | - |

## 预算耗尽而中止的会话（模型调用达到 12 次上限）

| run | 任务 | 模型调用 | 工具调用 | 已受理 job | job 真实终态（turn 结束后的核验窗口） |
|---|---|---|---|---|---|
| t2-1-f73871b7 | t2 | 12 | 15 | job_bea60d0b | {'job_bea60d0b': 'succeeded'} |
| t2-2-5583e89e | t2 | 12 | 14 | job_c81e8067 | {'job_c81e8067': 'succeeded'} |

说明：以上会话在模型调用达到 12 次上限时被运行时中止；后台 review_generate 的真实终态见上表（本脚本在 turn 结束后用剩余核验窗口读取，不延长模型/工具预算）。预算是固定上限，未因失败放宽。


## T2 明细（受理 / 同调用等待 / 真实 worker / 试卷）

| run | 预检 | job | wait_seconds 请求 | 本调用内观测到终态 | jobs.read 状态序列 | job 真实终态 | 卷版本 | 题数 |
|---|---|---|---|---|---|---|---|---|
| t2-1-f73871b7 | True | job_bea60d0b | [14] | False | ['running', 'succeeded'] | {'job_bea60d0b': 'succeeded'} | v1 | 3 |
| t2-2-5583e89e | True | job_c81e8067 | [14] | False | ['running', 'running', 'running', 'running'] | {'job_c81e8067': 'succeeded'} | v1 | 3 |
| t2-3-fc27eaeb | True | job_43b1d1a2 | [14] | False | ['running', 'succeeded'] | {'job_43b1d1a2': 'succeeded'} | v1 | 3 |

本摘要只是本次记录的 3 次真实模型小样本执行记录，不构成最终验收结论，也不声称确定性或生产成功率。
