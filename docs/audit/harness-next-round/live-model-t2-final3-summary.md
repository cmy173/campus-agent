# 真实模型三项任务结果摘要（已记录 3 次会话）

- 证据集标签：`t2_after_envelope_fix`
- 关联：first_9runs → `docs/audit/harness-next-round/live-model.json`
- 关联：t2_retest_before_fix → `docs/audit/harness-next-round/live-model-retests.json`
- 关联：t2_retest_after_first_fix → `docs/audit/harness-next-round/live-model-t2-retest.json`
- 关联：t2_final_after_bounded_wait_fix → `docs/audit/harness-next-round/live-model-t2-final.json`
- 关联：t2_after_wait_window_14s → `docs/audit/harness-next-round/live-model-t2-final2.json`
- 生成时间：2026-10-15T09:46:06.932434+00:00
- 证据 JSON：`docs/audit/harness-next-round/live-model-t2-final3.json`
- 原始轨迹：`docs/audit/harness-next-round/traces-live/<run_id>.json`
- 产物副本：`docs/audit/harness-next-round/artifacts-live/<run_id>/`
- 状态：`partial`；金额：`unknown`（无账单依据）
- Provider：`{'id': 'prv_c68d2878', 'name': 'deepseek', 'model': 'deepseek-flash', 'protocol': 'openai_chat_completions', 'base_url_host': 'api.deepseek.com'}`

## 逐项达标

| 任务 | 会话数 | 达标 |
|---|---|---|
| t1 附件→PDF/XLSX | 0 | 0 |
| t2 课程材料→复习卷→导出 | 3 | 3 |
| t3 缺资料/授权不足 | 0 | 0 |

## 汇总

- 达标会话：3/3
- 本文件只含 3 次复测会话；9 次门槛只适用于 first_9runs，本文件不参与该判定
- 自动失败项：无
- 主 Agent token 合计：{'inputTokens': 29902, 'outputTokens': 7103, 'cacheReadTokens': 535808, 'cacheWriteTokens': 0, 'totalTokens': 572813}


## 逐次会话

| run | 任务 | 状态 | 达标 | 模型调用 | 工具调用 | 秒 | 最终产物 | 失败项 |
|---|---|---|---|---|---|---|---|---|
| t2-1-ca7d4b41 | t2 | completed | True | 9 | 9 | 44.8 | 合成实验课程 复习卷-完整卷.md | - |
| t2-2-00bc3c8c | t2 | completed | True | 12 | 16 | 52.0 | 合成实验课程复习卷-完整卷.md | - |
| t2-3-2e4c0ed9 | t2 | completed | True | 9 | 9 | 41.7 | 合成实验课程复习卷-完整卷.md | - |

## 预算耗尽而中止的会话（模型调用达到 12 次上限）

| run | 任务 | 模型调用 | 工具调用 | 已受理 job | job 真实终态（turn 结束后的核验窗口） |
|---|---|---|---|---|---|
| t2-2-00bc3c8c | t2 | 12 | 16 | job_bb17c5f3 | {'job_bb17c5f3': 'succeeded'} |

说明：以上会话在模型调用达到 12 次上限时被运行时中止；后台 review_generate 的真实终态见上表（本脚本在 turn 结束后用剩余核验窗口读取，不延长模型/工具预算）。预算是固定上限，未因失败放宽。


## T2 明细（受理 / 同调用等待 / 真实 worker / 试卷）

| run | 预检 | job | wait_seconds 请求 | 本调用内观测到终态 | jobs.read 状态序列 | job 真实终态 | 卷版本 | 题数 |
|---|---|---|---|---|---|---|---|---|
| t2-1-ca7d4b41 | True | job_0e51a37e | [14] | False | ['succeeded'] | {'job_0e51a37e': 'succeeded'} | v1 | 3 |
| t2-2-00bc3c8c | True | job_bb17c5f3 | [14] | False | ['running', 'running', 'running', 'succeeded'] | {'job_bb17c5f3': 'succeeded'} | v1 | 3 |
| t2-3-2e4c0ed9 | True | job_f80b5d78 | [14] | False | ['succeeded'] | {'job_f80b5d78': 'succeeded'} | v1 | 3 |

本摘要只是本次记录的 3 次真实模型小样本执行记录，不构成最终验收结论，也不声称确定性或生产成功率。
