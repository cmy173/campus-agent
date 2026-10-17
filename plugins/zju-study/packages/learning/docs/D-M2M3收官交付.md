# D · M2/M3 收官轮交付（D15–D18）

- 智能体：D（`packages/learning/`），分支 `agent/d`，本轮基准 `main@683c74b`（v1.8 裁定）
- 日期：2026-10-10
- 依据：`docs/coord/tasks/最终轮-M2M3收官任务书.md` §2 工作包 D、§4、§8 交接与验收口径

## 0. 结论（一段话）

**部分达成：任务书四项中，D16（三项产品决策落地）、D17（ICS 导出时间与约束正确性）、D18（无模型降级复核）三项完成且全绿（257 passed 0 skipped）；D15（真实数据估算与任务生成验证）未执行——其上游 A21「一门真实课程真实同步」截至本轮收尾仍未落地**（`agent/a@576375e` 仅至 A20），D15 如实标注「尚未验证」，未以任何近似数据冒充真实课程结论（D10 冻结快照的观察条件另列 §3）。D 侧无其它未闭合项。

## 1. 逐项状态

### D16 三项产品决策落地 —— 已实现（并已在真实样例冻结快照上观察，§3）

| 裁决 | D 侧实现 | 验证 |
|---|---|---|
| PD-1 解析上限 64MB | `ParseLimits.max_file_bytes` 默认 `64_000_000`（十进制，超 1 字节即 `FILE_TOO_LARGE`） | 单元边界测试（恰等放行 / 超 1 字节拒收）；真实快照观察见 §3 |
| PD-2 视频 → `watching` | `kind=other` 且（MIME `video/*` 或标题视频扩展名）→ `watching`；稳定键 `t:{course}:watching:…`；完成标准「观看视频资料并记录要点」（无「阅读」字样）；估算走 `default_minutes_by_type["watching"]`（=60，与 `contracts/config-defaults.json` 一致）+ D6 校正 | 13 项裁决测试 + 真实快照观察（4 个 mp4 → watching）；非视频 reading 路径逐字段不变 |
| PD-3 平台已提交不排程 | `build_tasks_json` 输出 `platform_submitted`（`submission_status ∈ {submitted, graded}` → true，契约外字段）；`generate_plan`/`revise_plan` 不产生时间块、不进未安排清单；前置视同已满足；`validate_plan` 新增 `PLATFORM_SUBMITTED_TASK_SCHEDULED`（锁定块豁免）；revise 释放旧块原因 `PLATFORM_SUBMITTED`；任务行保留（学习状态与平台提交状态分离） | 13 项裁决测试 + 真实快照观察（3 条已提交仍生成、flag=true） |

- B 侧对账（已核对 `agent/b@3f07cba`）：B 以**自己的方式**落地同一裁决——解析常量统一为 `config.MAX_PARSE_BYTES`=64MB（与 D 同值）；排程排除改用「来源引用 platform+external_id 关联已提交/已评分作业」在 `plan_engine` 内部完成，D 的 `platform_submitted` 字段**未进入 B 落库/API 契约**（B 的裁定路径，两边双重排除幂等一致）。呈报文件：`D致B-D16-三项裁决落库与计划输入动作.md`。
- 已知未闭合（界面侧，不属 D 范围）：C 的「平台已提交」弱化标注缺数据来源（B 未落库该字段）——已转答 C：`D致C-D16D17-展示口径与ICS接通.md` §2，明确要求不得用间接推断冒充。

### D17 ICS 导出时间与约束正确性 —— 已实现（配合 B19 已接通；真实日历客户端导入尚未验证）

- 实现：`learning.schedule.ics.plan_to_ics`（RFC 5545：CRLF、75 八位组 UTF-8 安全折行、TEXT 转义；用户时区墙钟 + `TZID`/`VTIMEZONE`；**DST 多偏移自动 UTC 回退**（不伪造时区规则）；全天 `VALUE=DATE`（DTEND=次日）与精确时间区分；跨日块不截断；UID 稳定、DTSTAMP 可复现）。
- 约束一致性被机器验证：`cross_midnight`/`tight_daily_limit` 样例上 ICS 账本 == `split_by_date` == 每日上限（参数化测试）；`validate_plan == []`。
- B19 对账（`agent/b@bee0dcc`）：B 已并行落地自有渲染器并接入 `POST /api/v1/exports`（format=ics），主路径一致；两处实质差异（DST 多偏移 VTIMEZONE 写法、截止事件是否含 completed/platform_submitted）已呈报 B 裁定：`D致B-D17-ICS导出接入说明.md` §7。D 不改 `apps/api`。
- **尚未验证**：真实日历客户端（系统日历/Google/Outlook）导入——本机无客户端与账号，仅验证 ICS 文本层正确性与自研解析回归。交付文档应同步标注。

### D18 无模型降级复核 —— 已实现（测试层强断言）

- socket 层封锁全部出网入口（含实例级 `connect`/`connect_ex`），全链路完成：解析（PDF/DOCX/PPTX）→ 资料整理 → 任务生成 → 排程 → 局部重排 → **ICS 导出** → 估算校正 → 回放字幕；
- 模型网关三种情形均降级零调用：默认未启用 / 启用但配置不全 / 启用且配置完整但默认 `NullModelClient`（新增断言 attempts==1 且零网络）；
- 元测试证明守卫生效；D8 红线（默认零网络、密钥仅引用形态）无回归。

### D15 真实数据估算与任务生成验证 —— 尚未验证（未执行）

- 阻塞事实：依赖 A21 真实同步落库数据；`agent/a@576375e` 无 A21 提交（A21 需用户真实登录 + 同步授权，任务书 §6 硬依赖 1/2，尚未发生）。
- 只用真实输入才能回答的问题（`extent` 是否可用、兜底比例、告警误报）**保持开放**，不预先给结论。
- 恢复路径（A21 落地后）：以 B 落库导出的该课程「资源/作业/回放」JSON 为输入，走 `build_tasks_json` + `generate_plan`，报告实际观察（沿用 D10 校准报告格式，注明观察条件）。

## 2. 提交清单（供 B 合并时逐条机械核对）

> 合并提醒：本分支已自测全绿（257 passed 0 skipped），请按惯例合并并复跑 learning 全量；合并后逐条列提交号（协调者要求合并声明可与机械核对一致）。工作树干净、无未提交改动。

`agent/d` 本轮提交（供 B 合并时逐条机械核对；以下清单随本文档落盘，其后的补录提交亦在同一区间，最终以 `git log --oneline main..agent/d` 全量输出为准）：

| 提交 | 内容 |
|---|---|
| `036bd9c` | D16 三项产品决策落地 + 13 项裁决测试（含 D10 快照测试同步更新） |
| `50a00bc` | D17 `plan_to_ics` + 16 项约束测试 |
| `6d2df68` | D17 转答 B（ICS 接入说明） |
| `38facbc` | D18 无模型降级复核（零网络全链 + 守卫加固） |
| `6440f09` | 转答 B（D16 落库动作 + D17 §7 与 B19 对账） |
| `f9cde21` | 转答 C（展示口径 + ICS 接通） |
| `d528cc4` | 本文档（收官交付） |

## 3. 真实数据结论的观察条件（如实，不外推）

- 数据：D10/A16 **冻结快照** `packages/learning/samples/real-2026-10-10/`（sha256 钉死；ID 虚构化，见其 `PROVENANCE.md`），xzzd 平台导出，两门课程（`crs_5a1b01`/`crs_5a1b02`），28 资源 + 62 作业。
- 观察（本轮 D16 更新后复跑固化）：90 任务 = 24 reading（22 文档 + 2 slides）+ **4 watching（原 mp4，PD-2 后）** + 62 assignment（全兜底 90 分钟）；due 62/62 = ISO `Z`+毫秒（北京 23:59×56 / 21:15×6）；5/28 超 64MB 全部为 `preview_only` 视频（411–867MB），42,879,434 字节 full PDF 在新护栏下**不再超限**；3 条已提交（`asg_r10b20/27/28`）仍生成且 `platform_submitted=true`。
- 边界：该快照是**采集时点的只读复制**，非真实同步全链（无解析产物、无反馈观测）；结论仅对该快照成立，不得外推其它课程。

## 4. 可复现命令

```bash
cd packages/learning && .venv/Scripts/python -m pytest -q      # 257 passed, 0 skipped（本机 Python 3.13.5）
# 分类查看：
.venv/Scripts/python -m pytest -q tests/test_d16_product_decisions.py tests/test_ics.py tests/test_no_network.py tests/test_d10_real_samples.py
```

## 5. 未做项（显式，无「进行中」）

1. **D15 未执行**（上游 A21 未落地，见 §1；不冒充）。
2. **ICS 未被真实日历客户端导入验证**（无客户端环境）。
3. D 不改 `apps/api`/`apps/web`：PD-3 界面弱化（C）与 B 侧字段落库、ICS 渲染器差异裁定（B）均转答后由对应方执行；呈报文件见 §1 各条。
4. zykt 真实化（A23）、部署材料（B21）等属他方范围，不在本文档声明。

## 6. 交接要求对齐自查（任务书 §8）

- [x] 开头明确结论（部分达成 + 原因）；[x] 逐项四态区分（§1 每项标注）；[x] 可复现命令（§4）；[x] 真实数据注明观察条件（§3）；[x] 未做项显式列出（§5）；[x] 合并提交号逐条（§2）。
