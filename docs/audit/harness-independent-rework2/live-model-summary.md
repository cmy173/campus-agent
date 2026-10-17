# 真实模型三项任务结果摘要（已记录 3 次会话）

- 证据集标签：`independent_rework2_acceptance`
- 生成时间：2026-10-15T13:38:23.575596+00:00
- 判定版本：judge v4（完整交付口径：turn 必须 succeeded、最终回答非空、T2 交付物须属于同一业务操作）
- 证据 JSON：`docs/audit/harness-independent-rework2/live-model.json`
- 原始轨迹：`docs/audit/harness-independent-rework2/traces-live/<run_id>.json`
- 产物副本：`docs/audit/harness-independent-rework2/artifacts-live/<run_id>/`
- 状态：`partial`；金额：`unknown`（无账单依据）
- Provider：`{'id': 'prv_c68d2878', 'name': 'deepseek', 'model': 'deepseek-flash', 'protocol': 'openai_chat_completions', 'base_url_host': 'api.deepseek.com'}`

## 逐项达标

| 任务 | 会话数 | 达标 |
|---|---|---|
| t1 附件→PDF/XLSX | 1 | 1 |
| t2 课程材料→复习卷→导出 | 1 | 1 |
| t3 缺资料/授权不足 | 1 | 1 |

## 汇总

- 达标会话：3/3
- 本文件只含 3 次复测会话；9 次门槛只适用于 first_9runs，本文件不参与该判定
- 证据阶段：independent_rework2_acceptance:a1fa2c2057e6
- 自动失败项：无
- 主 Agent token 合计：{'inputTokens': 18850, 'outputTokens': 8267, 'cacheReadTokens': 337920, 'cacheWriteTokens': 0, 'totalTokens': 365037}


## 逐次会话

| run | 任务 | 状态 | 达标 | 模型调用 | 工具调用 | 秒 | 最终产物 | 失败项 |
|---|---|---|---|---|---|---|---|---|
| t1-1-1af863d8 | t1 | completed | True | 3 | 4 | 37.2 | 合成材料拉伸测试报告.pdf, 合成材料拉伸测试报告.md, 合成材料拉伸测试样本数据.xlsx | - |
| t2-1-a8ddd93a | t2 | completed | True | 9 | 10 | 51.9 | 合成实验课程 复习卷-完整卷.md | - |
| t3-1-46df9c4d | t3 | completed | True | 6 | 13 | 32.2 | - | - |

## T2 明细（受理 / 同调用等待 / 真实 worker / 试卷）

| run | 预检 | job | wait_seconds 请求 | 本调用内观测到终态 | jobs.read 状态序列 | job 真实终态 | 卷版本 | 题数 |
|---|---|---|---|---|---|---|---|---|
| t2-1-a8ddd93a | True | job_5e366bb3 | [14] | False | ['succeeded'] | {'job_5e366bb3': 'succeeded'} | v1 | 3 |

本摘要只是本次记录的 3 次真实模型小样本执行记录，不构成最终验收结论，也不声称确定性或生产成功率。
