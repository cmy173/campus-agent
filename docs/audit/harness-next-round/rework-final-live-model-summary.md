# 真实模型三项任务结果摘要（已记录 9 次会话）

- 证据集标签：`rework_final`
- 关联：prior_rework_set → `docs/audit/harness-next-round/rework-live-model.json`
- 关联：first_round → `docs/audit/harness-next-round/live-model.json`
- 关联：independent_acceptance → `docs/audit/harness-independent/live-model.json`
- 生成时间：2026-10-15T12:19:51.317449+00:00
- 判定版本：judge v4（完整交付口径：turn 必须 succeeded、最终回答非空、T2 交付物须属于同一业务操作）
- 证据 JSON：`docs/audit/harness-next-round/rework-final-live-model.json`
- 原始轨迹：`docs/audit/harness-next-round/traces-live/<run_id>.json`
- 产物副本：`docs/audit/harness-next-round/artifacts-live/<run_id>/`
- 状态：`complete`；金额：`unknown`（无账单依据）
- Provider：`{'id': 'prv_c68d2878', 'name': 'deepseek', 'model': 'deepseek-flash', 'protocol': 'openai_chat_completions', 'base_url_host': 'api.deepseek.com'}`

## 逐项达标

| 任务 | 会话数 | 达标 |
|---|---|---|
| t1 附件→PDF/XLSX | 3 | 3 |
| t2 课程材料→复习卷→导出 | 3 | 3 |
| t3 缺资料/授权不足 | 3 | 3 |

## 汇总

- 达标会话：9/9
- 9 次门槛（≥8 且每项 ≥2/3）：满足
- 证据阶段：rework_final:f03e496dae97
- 自动失败项：无
- 主 Agent token 合计：{'inputTokens': 63335, 'outputTokens': 21049, 'cacheReadTokens': 945536, 'cacheWriteTokens': 0, 'totalTokens': 1029920}


## 逐次会话

| run | 任务 | 状态 | 达标 | 模型调用 | 工具调用 | 秒 | 最终产物 | 失败项 |
|---|---|---|---|---|---|---|---|---|
| t1-1-db8590d9 | t1 | completed | True | 3 | 3 | 30.9 | synthetic-tensile-test-report.pdf, synthetic-tensile-test-report.xlsx | - |
| t1-2-923ed23b | t1 | completed | True | 3 | 3 | 31.1 | synthetic-tensile-test-report.pdf, synthetic-tensile-test-report.xlsx | - |
| t1-3-60b9e5bf | t1 | completed | True | 3 | 3 | 33.8 | synthetic-tensile-test-report.pdf, synthetic-tensile-test-report.xlsx | - |
| t2-1-2ed88943 | t2 | completed | True | 9 | 10 | 51.2 | 合成实验课程 复习卷-完整卷.md | - |
| t2-2-fd406bc7 | t2 | completed | True | 9 | 9 | 38.1 | 合成实验课程 复习卷-完整卷.md | - |
| t2-3-c3dc0081 | t2 | completed | True | 9 | 10 | 38.4 | 合成实验课程 复习卷-完整卷.md | - |
| t3-1-b6b6ed37 | t3 | completed | True | 5 | 8 | 23.6 | - | - |
| t3-2-2aa1bbce | t3 | completed | True | 6 | 10 | 27.3 | - | - |
| t3-3-eb8c2222 | t3 | completed | True | 5 | 10 | 29.0 | - | - |

## T2 明细（受理 / 同调用等待 / 真实 worker / 试卷）

| run | 预检 | job | wait_seconds 请求 | 本调用内观测到终态 | jobs.read 状态序列 | job 真实终态 | 卷版本 | 题数 |
|---|---|---|---|---|---|---|---|---|
| t2-1-2ed88943 | True | job_f6f0c0be | [14] | False | ['succeeded'] | {'job_f6f0c0be': 'succeeded'} | v1 | 3 |
| t2-2-fd406bc7 | True | job_405c9649 | [14] | False | ['succeeded'] | {'job_405c9649': 'succeeded'} | v1 | 3 |
| t2-3-c3dc0081 | True | job_c1f36bfd | [14] | False | ['succeeded'] | {'job_c1f36bfd': 'succeeded'} | v1 | 3 |

本摘要只是本次记录的 9 次真实模型小样本执行记录，不构成最终验收结论，也不声称确定性或生产成功率。
