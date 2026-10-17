# 真实模型三项任务结果摘要（已记录 9 次会话）

- 证据集标签：`rework_v1`
- 关联：first_round → `docs/audit/harness-next-round/live-model.json`
- 关联：independent → `docs/audit/harness-independent/live-model.json`
- 关联：judge_v4_rejudge → `docs/audit/harness-next-round/live-model-rejudged-v4.json`
- 生成时间：2026-10-15T11:58:31.348737+00:00
- 判定版本：judge v4（完整交付口径：turn 必须 succeeded、最终回答非空、T2 交付物须属于同一业务操作）
- 证据 JSON：`docs/audit/harness-next-round/rework-live-model.json`
- 原始轨迹：`docs/audit/harness-next-round/traces-live/<run_id>.json`
- 产物副本：`docs/audit/harness-next-round/artifacts-live/<run_id>/`
- 状态：`complete`；金额：`unknown`（无账单依据）
- Provider：`{'id': 'prv_c68d2878', 'name': 'deepseek', 'model': 'deepseek-flash', 'protocol': 'openai_chat_completions', 'base_url_host': 'api.deepseek.com'}`

## 逐项达标

| 任务 | 会话数 | 达标 |
|---|---|---|
| t1 附件→PDF/XLSX | 3 | 3 |
| t2 课程材料→复习卷→导出 | 3 | 2 |
| t3 缺资料/授权不足 | 3 | 3 |

## 汇总

- 达标会话：8/9
- 9 次门槛（≥8 且每项 ≥2/3）：满足
- 证据阶段：rework_v1:07c19c1248ec
- 自动失败项：无
- 主 Agent token 合计：{'inputTokens': 61215, 'outputTokens': 21722, 'cacheReadTokens': 962944, 'cacheWriteTokens': 0, 'totalTokens': 1045881}


## 逐次会话

| run | 任务 | 状态 | 达标 | 模型调用 | 工具调用 | 秒 | 最终产物 | 失败项 |
|---|---|---|---|---|---|---|---|---|
| t1-1-0f1b8021 | t1 | completed | True | 3 | 3 | 33.1 | 合成材料拉伸测试报告.pdf, 合成材料拉伸测试报告.xlsx | - |
| t1-2-e7a3ca5c | t1 | completed | True | 3 | 3 | 31.9 | synthetic-tensile-test-report.pdf, synthetic-tensile-test-report.xlsx | - |
| t1-3-87d3bbd2 | t1 | completed | True | 3 | 4 | 32.6 | synthetic-tensile-report.pdf, synthetic-tensile-data.xlsx | - |
| t2-1-e491db8a | t2 | completed | True | 9 | 9 | 37.4 | 合成实验课程 复习卷-完整卷.md | - |
| t2-2-b0f50296 | t2 | completed | True | 8 | 9 | 38.1 | 合成实验课程复习卷-完整卷.md | - |
| t2-3-5ec4b475 | t2 | completed | False | 12 | 13 | 44.7 | - | turn_succeeded, final_answer_nonempty, worker_succeeded, paper_read, export_published, export_content, answer_cites_delivery, single_operation_lineage |
| t3-1-fe316ab9 | t3 | completed | True | 5 | 9 | 22.3 | - | - |
| t3-2-87be399a | t3 | completed | True | 5 | 12 | 26.2 | - | - |
| t3-3-a82b0510 | t3 | completed | True | 6 | 10 | 25.1 | - | - |

## 预算耗尽而中止的会话（模型调用达到 12 次上限）

| run | 任务 | 模型调用 | 工具调用 | 已受理 job | job 真实终态（turn 结束后的核验窗口） |
|---|---|---|---|---|---|
| t2-3-5ec4b475 | t2 | 12 | 13 | job_593665be | {'job_593665be': 'succeeded'} |

说明：以上会话在模型调用达到 12 次上限时被运行时中止；后台 review_generate 的真实终态见上表（本脚本在 turn 结束后用剩余核验窗口读取，不延长模型/工具预算）。预算是固定上限，未因失败放宽。


## T2 明细（受理 / 同调用等待 / 真实 worker / 试卷）

| run | 预检 | job | wait_seconds 请求 | 本调用内观测到终态 | jobs.read 状态序列 | job 真实终态 | 卷版本 | 题数 |
|---|---|---|---|---|---|---|---|---|
| t2-1-e491db8a | True | job_c459f969 | [14] | False | ['succeeded'] | {'job_c459f969': 'succeeded'} | v1 | 3 |
| t2-2-b0f50296 | True | job_b34f6909 | [14] | True | ['succeeded'] | {'job_b34f6909': 'succeeded'} | v1 | 3 |
| t2-3-5ec4b475 | True | job_593665be | [14, 14] | False | ['running', 'running', 'running', 'running', 'running'] | {'job_593665be': 'succeeded'} | v1 | 3 |

本摘要只是本次记录的 9 次真实模型小样本执行记录，不构成最终验收结论，也不声称确定性或生产成功率。
