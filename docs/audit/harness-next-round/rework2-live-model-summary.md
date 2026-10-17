# 真实模型三项任务结果摘要（已记录 9 次会话）

- 证据集标签：`rework2_live`
- 生成时间：2026-10-15T13:23:54.957049+00:00
- 判定版本：judge v4（完整交付口径：turn 必须 succeeded、最终回答非空、T2 交付物须属于同一业务操作）
- 证据 JSON：`docs/audit/harness-next-round/rework2-live-model.json`
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
- 证据阶段：rework2_live:a1fa2c2057e6
- 自动失败项：无
- 主 Agent token 合计：{'inputTokens': 48603, 'outputTokens': 22498, 'cacheReadTokens': 957952, 'cacheWriteTokens': 0, 'totalTokens': 1029053}


## 逐次会话

| run | 任务 | 状态 | 达标 | 模型调用 | 工具调用 | 秒 | 最终产物 | 失败项 |
|---|---|---|---|---|---|---|---|---|
| t1-1-95ecd663 | t1 | completed | True | 3 | 3 | 34.4 | synthetic-tensile-test-report.pdf, synthetic-tensile-test-report.xlsx | - |
| t1-2-fe04e6ac | t1 | completed | True | 3 | 3 | 31.5 | synthetic-tensile-test-report.pdf, synthetic-tensile-test-report.xlsx | - |
| t1-3-a143bb3b | t1 | completed | True | 3 | 4 | 32.0 | synthetic-lab-report.pdf, synthetic-lab-samples.xlsx | - |
| t2-1-8ce322ee | t2 | completed | True | 9 | 10 | 44.2 | 合成实验课程 复习卷-完整卷.md | - |
| t2-2-d19eec37 | t2 | completed | True | 9 | 9 | 41.1 | 合成实验课程 复习卷-完整卷.md | - |
| t2-3-c3a397e7 | t2 | completed | True | 9 | 9 | 37.8 | 合成实验课程 复习卷-完整卷.md | - |
| t3-1-3d4297a8 | t3 | completed | True | 5 | 10 | 23.2 | - | - |
| t3-2-a188282a | t3 | completed | True | 6 | 10 | 27.4 | - | - |
| t3-3-75b24013 | t3 | completed | True | 5 | 9 | 23.7 | - | - |

## T2 明细（受理 / 同调用等待 / 真实 worker / 试卷）

| run | 预检 | job | wait_seconds 请求 | 本调用内观测到终态 | jobs.read 状态序列 | job 真实终态 | 卷版本 | 题数 |
|---|---|---|---|---|---|---|---|---|
| t2-1-8ce322ee | True | job_91d60017 | [14] | False | ['succeeded'] | {'job_91d60017': 'succeeded'} | v1 | 3 |
| t2-2-d19eec37 | True | job_6d756be3 | [14] | False | ['succeeded'] | {'job_6d756be3': 'succeeded'} | v1 | 3 |
| t2-3-c3a397e7 | True | job_04c985b6 | [14] | False | ['succeeded'] | {'job_04c985b6': 'succeeded'} | v1 | 3 |

本摘要只是本次记录的 9 次真实模型小样本执行记录，不构成最终验收结论，也不声称确定性或生产成功率。
