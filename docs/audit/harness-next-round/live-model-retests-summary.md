# 真实模型三项任务结果摘要（已记录 2 次会话）

- 证据集标签：`t2_retest_before_fix`
- 关联：first_9runs → `docs/audit/harness-next-round/live-model.json`
- 生成时间：2026-10-15T09:30:09.794901+00:00
- 证据 JSON：`docs/audit/harness-next-round/live-model-retests.json`
- 原始轨迹：`docs/audit/harness-next-round/traces-live/<run_id>.json`
- 产物副本：`docs/audit/harness-next-round/artifacts-live/<run_id>/`
- 状态：`partial`；金额：`unknown`（无账单依据）
- Provider：`{'id': 'prv_c68d2878', 'name': 'deepseek', 'model': 'deepseek-flash', 'protocol': 'openai_chat_completions', 'base_url_host': 'api.deepseek.com'}`

## 逐项达标

| 任务 | 会话数 | 达标 |
|---|---|---|
| t1 附件→PDF/XLSX | 0 | 0 |
| t2 课程材料→复习卷→导出 | 2 | 0 |
| t3 缺资料/授权不足 | 0 | 0 |

## 汇总

- 达标会话：0/2
- 9 次门槛（≥8 且每项 ≥2/3）：未满足
- 自动失败项：无
- 主 Agent token 合计：{'inputTokens': 12128, 'outputTokens': 3814, 'cacheReadTokens': 448128, 'cacheWriteTokens': 0, 'totalTokens': 464070}


## 逐次会话

| run | 任务 | 状态 | 达标 | 模型调用 | 工具调用 | 秒 | 最终产物 | 失败项 |
|---|---|---|---|---|---|---|---|---|
| t2-1-a7b95f3f | t2 | completed | False | 12 | 17 | 27.6 | - | worker_succeeded, paper_read, export_published, export_content |
| t2-2-2f1b5c93 | t2 | completed | False | 12 | 13 | 24.7 | - | worker_succeeded, paper_read, export_published, export_content |

## 预算耗尽而中止的会话（模型调用达到 12 次上限）

| run | 任务 | 模型调用 | 工具调用 | 已受理 job | job 真实终态（turn 结束后的核验窗口） |
|---|---|---|---|---|---|
| t2-1-a7b95f3f | t2 | 12 | 17 | job_c160071b | {'job_c160071b': 'succeeded'} |
| t2-2-2f1b5c93 | t2 | 12 | 13 | job_2aebb5c6 | {'job_2aebb5c6': 'succeeded'} |

说明：以上会话的模型调用刚好用满 12 次上限，后台 review_generate 仍在运行；本脚本在 turn 结束后用剩余核验窗口读取 job 真实终态（不延长模型/工具预算）。预算是固定上限，未因失败放宽。


本摘要只是本轮 9 次小样本的执行记录，不构成最终验收结论，也不声称确定性或生产成功率。
