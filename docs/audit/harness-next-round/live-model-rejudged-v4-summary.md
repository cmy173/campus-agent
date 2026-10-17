# 真实模型三项任务结果摘要（已记录 9 次会话）

- 生成时间：2026-10-15T11:34:36.029054+00:00
- 判定版本：judge v4（完整交付口径：turn 必须 succeeded、最终回答非空、T2 交付物须属于同一业务操作）
- 证据 JSON：`docs/audit/harness-next-round/live-model-rejudged-v4.json`
- 原始轨迹：`docs/audit/harness-next-round/traces-live/<run_id>.json`
- 产物副本：`docs/audit/harness-next-round/artifacts-live/<run_id>/`
- 状态：`complete`；金额：`unknown`（无账单依据）
- Provider：`{'id': 'prv_c68d2878', 'name': 'deepseek', 'model': 'deepseek-flash', 'protocol': 'openai_chat_completions', 'base_url_host': 'api.deepseek.com'}`

## 逐项达标

| 任务 | 会话数 | 达标 |
|---|---|---|
| t1 附件→PDF/XLSX | 3 | 3 |
| t2 课程材料→复习卷→导出 | 3 | 0 |
| t3 缺资料/授权不足 | 3 | 3 |

## 汇总

- 达标会话：6/9
- 9 次门槛（≥8 且每项 ≥2/3）：未满足
- 证据阶段：unlabelled
- 自动失败项：无
- 主 Agent token 合计：{'inputTokens': 51515, 'outputTokens': 21633, 'cacheReadTokens': 1069824, 'cacheWriteTokens': 0, 'totalTokens': 1142972}


## 逐次会话

| run | 任务 | 状态 | 达标 | 模型调用 | 工具调用 | 秒 | 最终产物 | 失败项 |
|---|---|---|---|---|---|---|---|---|
| t1-1-5754bc6e | t1 | completed | True | 3 | 4 | 18.1 | synthetic-tensile-test-report.pdf, synthetic-tensile-test-data.xlsx | - |
| t1-2-63c81253 | t1 | completed | True | 3 | 3 | 32.1 | synthetic-tensile-test-report.pdf, synthetic-tensile-test-report.xlsx | - |
| t1-3-3c2b6182 | t1 | completed | True | 4 | 5 | 38.5 | 合成材料拉伸测试报告.pdf, 合成材料拉伸测试数据.xlsx, 合成材料拉伸测试数据.xlsx | - |
| t2-1-03525889 | t2 | completed | False | 12 | 13 | 26.3 | - | turn_succeeded, final_answer_nonempty, worker_succeeded, paper_read, export_published, export_content, material_traceable, answer_cites_delivery, single_operation_lineage |
| t2-2-a201a8c6 | t2 | completed | False | 12 | 14 | 31.3 | - | turn_succeeded, final_answer_nonempty, worker_succeeded, paper_read, export_published, export_content, answer_cites_delivery, single_operation_lineage |
| t2-3-e0226e62 | t2 | completed | False | 12 | 15 | 24.0 | - | turn_succeeded, final_answer_nonempty, worker_succeeded, paper_read, export_published, export_content, answer_cites_delivery, single_operation_lineage |
| t3-1-1b67a153 | t3 | completed | True | 5 | 8 | 25.9 | - | - |
| t3-2-da649e62 | t3 | completed | True | 5 | 9 | 24.2 | - | - |
| t3-3-b4024110 | t3 | completed | True | 6 | 7 | 24.7 | - | - |

## 预算耗尽而中止的会话（模型调用达到 12 次上限）

| run | 任务 | 模型调用 | 工具调用 | 已受理 job | job 真实终态（turn 结束后的核验窗口） |
|---|---|---|---|---|---|
| t2-1-03525889 | t2 | 12 | 13 | job_781e5b8b | {'job_781e5b8b': 'running'} |
| t2-2-a201a8c6 | t2 | 12 | 14 | job_f8d8c69d | {'job_f8d8c69d': 'succeeded'} |
| t2-3-e0226e62 | t2 | 12 | 15 | job_9f23ff0e | {'job_9f23ff0e': 'succeeded'} |

说明：以上会话在模型调用达到 12 次上限时被运行时中止；后台 review_generate 的真实终态见上表（本脚本在 turn 结束后用剩余核验窗口读取，不延长模型/工具预算）。预算是固定上限，未因失败放宽。


## 复测（单独统计，不并入上面 9 次）

- 文件：`docs/audit/harness-next-round/live-model-retests.json`
- 说明：documented re-tests of the failing task at the SAME fixed budget; they are NOT part of the 9-run statistics above and never replace a first record
- t2-1-a7b95f3f 任务=t2 达标=False 模型调用=12 工具调用=17 job真实终态={'job_c160071b': 'succeeded'} 错误路径=budget_exhausted_turn_stopped
- t2-2-2f1b5c93 任务=t2 达标=False 模型调用=12 工具调用=13 job真实终态={'job_2aebb5c6': 'succeeded'} 错误路径=budget_exhausted_turn_stopped

## T2 明细（受理 / 同调用等待 / 真实 worker / 试卷）

| run | 预检 | job | wait_seconds 请求 | 本调用内观测到终态 | jobs.read 状态序列 | job 真实终态 | 卷版本 | 题数 |
|---|---|---|---|---|---|---|---|---|
| t2-1-03525889 | True | job_781e5b8b | [0] | None | ['running', 'running', 'running', 'running'] | {'job_781e5b8b': 'running'} | vNone | 0 |
| t2-2-a201a8c6 | True | job_f8d8c69d | [0] | None | ['running', 'running', 'running'] | {'job_f8d8c69d': 'succeeded'} | v1 | 3 |
| t2-3-e0226e62 | True | job_9f23ff0e | [0] | None | ['running', 'running', 'running', 'running', 'running'] | {'job_9f23ff0e': 'succeeded'} | v1 | 3 |

本摘要只是本次记录的 9 次真实模型小样本执行记录，不构成最终验收结论，也不声称确定性或生产成功率。
