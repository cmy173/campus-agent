# C-M2 · 起步轮交接（智能体 C → 智能体 B / 协调者）

- 日期：2026-10-10　分支：`agent/c`　worktree：`.worktrees/agent-c`
- 范围：`apps/web/` M2 起步轮——**C14**（窄屏响应式系统验证与修复）、**C15**（可访问性基线与键盘可达性）、**C16**（能力清单与文档对齐）、**C17**（复检文档入库与交接，本文件）
- 基准：`main@7fe381f`（v1.6 任务书，已含 M1 全部交付内容）；本分支经合并提交 `786f6c7` 并入其；本轮提交：**`9b15213`**（主，父 `786f6c7`，14 文件）→ `91eb18c`/`3c9c8f1`（文档回填）→ `e2c59c3`（E2E 清理）→ 本文件更新提交；合并请取 `agent/c` tip
- 格式对齐《00-协作总纲》§8：变更文件、输入输出契约、运行方法、测试结果、已知限制、待协作事项；§0 逐项区分已实现/已验证/模拟验证/尚未验证

## 0. 状态总览（区分口径）

| 项 | 状态 | 说明 |
|---|---|---|
| C14 窄屏响应式 | **已实现 + 已验证（真实浏览器）** | 7 路由 × 3 档（375/768/1280）共 21 格走查：修复前发现 1 类阻断缺陷（窄屏侧栏挤压内容至 160px + 横向溢出 17px）；修复后 **21/21 格溢出 0**（收束态实测，settleMs 1.3–3.9s）。展开态浮层行为用「收束态模拟」验证（零挤压/可点击/点菜单导航；动画完成态受环境限制，见 §5）。主证据：`docs/C14-窄屏响应式系统验证.md` |
| C15 可访问性 | **已实现 + 已验证（核心项）；焦点归还/陷阱为源码级核对** | 4 个点名组件＋挂接弹窗实测：打开焦点移入、Esc 关闭、关闭按钮可读名；表单错误 `aria-invalid` + `aria-describedby` 实测；全站复检 **0 个无名称控件**（唯一计数项为 antd Tabs 内建、无溢出时隐藏的操作钮）。焦点归还与 Tab 陷阱受 rc-motion 门控，隐藏页不可观测 → 源码级核对并如实标注。主证据：`docs/C15-可访问性基线与键盘可达性.md` |
| C16 能力清单对齐 | **已实现 + 已验证** | 设置页＋README 清单逐条给出可核对依据；修正过时项（导出真实模式已可用，不再写"随 B4"）；后端端点现状**实测**：`GET /api/v1/search` → 200、`GET /api/v1/exports` → 405（路由存在、仅 POST）、`GET /api/v1/health` → 200；全仓 grep 清理"随 B4/待后端"等过时表述（M1 历史文档加注记，不改史实） |
| C17 文档与交接 | **已实现** | 本文件 + C14/C15 两份主证据文档入库 `apps/web/docs/`；README 与设置页同步；gates 通过（§4） |

## 1. 变更文件（本轮，均为 `apps/web/`）

| 文件 | 说明 |
|---|---|
| `src/index.css` | **C14 修复**：< 992px 时 `.app-sider` 固定定位浮层 + `.app-sider-mask` 遮罩（含实测背景注释） |
| `src/components/AppLayout.tsx` | **C14**：Sider 加 `app-sider` 类；窄屏展开时渲染遮罩（点击收起，`aria-hidden`） |
| `src/features/today/TodayPage.tsx` | **C15**：反馈弹窗移除 `forceRender`（修复打开焦点移入）；打开时 `resetFields + setFieldsValue` 预填（修 `destroyOnHidden` 重挂载的表单残留） |
| `src/features/courses/AttachSourceRef.tsx` | **C15**：移除 `forceRender`；`onCancel` 重置表单；平台下拉 `aria-label="选择平台"` |
| `src/features/settings/AvailabilityForm.tsx` | **C15**：补 7 处 `aria-label`（时段起止、删除时段、每日上限、例外日期/备注、删除例外） |
| `src/features/settings/SettingsPage.tsx` | **C16**：能力清单更新（导出/搜索/同步按实测写明）；卡片与导语改为"能力状态与未接通项（如实标注）"口径；音视频转写、时间块拖动补依据 |
| `src/features/sync/SyncCenterPage.tsx` | **C15**：两个筛选下拉补 `aria-label` |
| `src/features/tasks/BuildTasksDrawer.tsx` | **C15**：范围下拉 `aria-label="生成范围（课程）"` |
| `src/features/onboarding/OnboardingPage.tsx` | **C15**：导入课程下拉 `aria-label="选择导入课程"` |
| `src/lib/api/real-mode.e2e.ts` | **M1 遗留清理（B 移交，`e2c59c3`）**：移除过时自更新提示 `B-COURSE-WRITE-IMPLEMENTED`（`createCourse` 成功改记 notes）；`B-POST-COURSES`/`B-PUT-AVAILABILITY` 更名 `-REGRESSION` 作回归观测；修正「未实现/B4 未覆盖」注释 |
| `README.md` | 状态/测试数（55）/能力清单/浏览器复检段落/无障碍章节（C14/C15 指针与口径）更新 |
| `docs/C14-窄屏响应式系统验证.md`（新） | C14 主证据：环境与方法、缺陷与修复、21 格矩阵、收束态模拟、限制、复现步骤 |
| `docs/C15-可访问性基线与键盘可达性.md`（新） | C15 主证据：焦点/Esc/错误 aria 实测、计数表、aria 变更清单、限制 |
| `docs/C-M1-交付与集成交接.md` | M2 注记（历史快照标注：能力清单以 README 与本文件为准） |
| `docs/C2-真实模式联调记录.md` | 顶部状态句与 §3.4 补 M2 起步轮处置（第七次运行 10/10、deviations=0；`-REGRESSION` 更名） |
| `docs/C-M2起步轮交接.md` | 本文件 |

## 2. 输入输出契约

- **无契约变更**：本轮为纯 UI/CSS/ARIA 与文案调整，不新增/修改/删除任何字段、端点、请求响应结构（未触碰 `contract.ts`、mock 语义与端点面）。
- 消费端点不变（M1 全集）；C16 引用的后端现状均为**只读探测**（search/health GET、exports GET 得 405）或既有 C2 联调记录，未触发任何写操作；§4 的 E2E 清理复跑按既有 C2 联调设计对**本地开发后端**执行写流程（第七次运行，10/10）。
- 安全红线延续（与 A14 v0.2 一致）：不在任何页面收集/代填/保存凭据；不出现"安装浏览器扩展"引导；跳转前展示权威域名；`auth_required` 不循环重试。

## 3. 运行方法（可复制命令）

```bash
# ── 常规（apps/web 目录）──
npx tsc -b
npx vitest run            # 55/55
npx vite build            # 无 >500 kB 告警

# ── 浏览器走查（C14/C15 方法见各自文档 §1/§9）──
npx vite --port 5173 --strictPort                        # 真实模式（后端 127.0.0.1:8000 在线）
VITE_API_MODE=mock npx vite --port 5175 --strictPort     # mock 模式
# 站内 DevTools：375/768/1280 三档逐路由检查横向溢出；弹窗/抽屉检查焦点与 Esc
```

## 4. 测试结果

| 项 | 结果 |
|---|---|
| `tsc -b` | ✅ 0 错误 |
| `vitest run` | ✅ **55/55**（5 文件，无回归；含反馈流程与任务生成抽屉等本轮触及组件） |
| `vite build` | ✅ 成功且**无 >500 kB 告警**（最大独立分包 rc-vendor 413.48 kB gzip 138.20） |
| C14 走查（真实浏览器） | ✅ 21/21 格 `overflowX=0`（7 路由×3 档；375/768 侧栏浮层收起、1280 桌面不变） |
| C15 复检（真实浏览器） | ✅ 焦点移入 ×5 组件、Esc 关闭 ×5、表单错误 aria ×1、计数 7 路由 0 无名称控件（1 处 antd 内部隐藏钮） |
| C16 后端实测 | ✅ `GET /api/v1/search` 200；`GET /api/v1/exports` 405（路由在，仅 POST）；`/api/v1/health` 200 |
| 真实模式 E2E（清理后复跑） | ✅ **10/10、deviations=0**（runStamp `20261010095639`）——`createCourse=200（POST /courses 可用）` 记 notes、PUT availability「写入并回读一致」、检查哨兵如实 `check=502`；报告 `var/c2-e2e-report.json` |

## 5. 已知限制（如实标注）

- 浏览器为**隐藏标签页**（QoderCN 0.4.3 / Chromium 150）：rAF 冻结、rc-motion 动画不结束 → **动画完成态、关闭后焦点归还、真实 Tab 遍历**不可观测；已用源码级核对补足（rc-dialog/rc-drawer 的 `useLockFocus`、焦点还原的 motion 门控），并标注"待前台复核"。Esc 为合成事件（与真实按键同一 window keydown 链路）。
- C14 展开态为「收束态模拟」（写入收束样式后度量），非动画断言；关闭动画在隐藏页停留于中间态属环境伪影。
- 未做：真机/前台视觉与 390px 更窄档、屏幕阅读器实测、颜色对比度审计（建议 M3）。
- 窄屏走查基于真实模式；mock 同一布局代码，未重复全走查。

## 6. 待协作事项

1. **B**：合并 `agent/c` 本轮提交。**预期无冲突**——本轮 10 个源码/文档文件在 `main@a6663d3` 上与分支基准逐字一致（已 `git diff` 核对），本轮另新增 2 份证据 docs 并小幅更新 2 份既有文件（`real-mode.e2e.ts` E2E 清理、`C2-真实模式联调记录.md` 补第七次运行；均在 `apps/web/` 内，不触契约）。**M1 遗留的 E2E 自更新提示已按你方移交清理**（`B-COURSE-WRITE-IMPLEMENTED` 移除、`-REGRESSION` 更名，见 §1）——第七次真实复跑 10/10、deviations=0，无需其他动作。另：本轮能力清单按后端实测收敛（导出已可用、搜索/同步端点就绪），若协调者台账/能力矩阵仍有旧字样，建议据此同步。
2. **A**：受控浏览器登录结论（A17，若已出）对 C 侧为**零代码改动**。按任务书要求记录现有连接/同步页在新形态下的表现：① 用户在外部窗口完成登录后，网页端**无自动侦测**（需点「检查连接」或刷新）——是否加轻量提示，建议下一轮与 C 一起定；② `auth_required` 的 recovery 入口（`entry_url` → 重登录弹窗）已就绪且不循环重试，与新形态兼容。**本轮未发现阻断缺口**。
3. **协调者**：C14 的"收束态模拟"项与 C15 的"焦点归还/Tab 陷阱"项，建议在 M3 于前台/真机环境用真实动画复核（清单分见两份文档"已知限制"）。
4. **D**：无需动作。

## 7. 状态口径小结（总纲 §8）

- **已实现**：C14 窄屏修复（侧栏浮层化＋遮罩）与两份证据文档；C15 aria 补齐与 forceRender 修复；C16 设置页/README 清单对齐；C17 交接。
- **已验证**（真实浏览器/后端实测）：21/21 窄屏走查；5 组件焦点/Esc/错误 aria；0 无名称控件计数；后端 search/exports/health 现状；55/55 测试；tsc；构建无告警；真实模式 E2E 清理后复跑 10/10、deviations=0（§4）。
- **模拟验证**（如实标注）：窄屏侧栏展开态几何（收束态模拟）。
- **尚未验证**（环境限制，M3 复核）：动画完成态、关闭后焦点归还、真实 Tab 遍历、真机视觉与屏幕阅读器、390px 更窄档。
