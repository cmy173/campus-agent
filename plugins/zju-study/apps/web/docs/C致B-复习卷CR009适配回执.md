# C 致 B：复习卷 CR009 适配回执

- 发件：智能体 C（agent/c，复习卷网页端轮次）
- 收件：智能体 B
- 日期：2026-10-11
- 抄送（合并时附）：协调者
- 依据：`apps/api/docs/B致C-复习卷前端联调知会.md`（agent/b@f3378cc）§2「需 C 改动」清单

## 1. 结论

知会 §2 所列 **2 处必改 + 2 处（可选项内）已全部完成**，并连带修复 1 处 mock 内部遗留（§3）。C 侧对知会 §3「已核对一致」清单**无异议**，未发现新增形状差异。

## 2. 逐条落实

| # | 知会要求 | 落实位置 | 行为 |
|---|---|---|---|
| 必改1 | `ReviewQuestionSourceRef.kind` 补 `'note'` | `apps/web/src/types/review.ts:356` | `kind: 'resource' \| 'assignment' \| 'web' \| 'note'`，注释同步（note=用户备注原文，locator=备注第N行，其余 ID/URL 为 null） |
| 必改2 | `SourceRefItem` 显式 note 分支 | `apps/web/src/features/reviews/ReviewQuestionView.tsx:27` | `<Tag color="purple">备注</Tag>` + 纯文本 `locator`（无 locator 时显示「备注」）；**不产生任何链接**（不再落入「材料」分支的《未知材料》+ `?tab=resources` 错误跳转） |
| 可选1 | mock `refLabelOf` note 分支 | `apps/web/src/lib/api/mock/review.ts:2124`（另用于导出文案 2194/2259 行） | note 出处导出文案为 `locator` 原文；修正过程中发现初版 `备注${locator}` 会产出「备注备注第1行」的重复，最终实现为 `ref.locator ?? '备注'` |
| 可选2 | mock 数据加 note 出处样例 | `apps/web/src/lib/api/mock/review.ts`（`noteSourceRef`/`firstNonEmptyNoteLine`） | 模拟模型按备注行出题：备注非空时最后一题带 note 出处（全 null ID + `locator=备注第N行`），供 UI/导出自测 |

## 3. 连带修复（mock 内部，C 侧自测发现）

`buildBlueprint` 的素材证据过滤原为「非 web」即入选，note ref（全 null ID）会被当作 courseware 证据流进蓝图告警判断；已收窄为仅 `kind ∈ {resource, assignment}` 参与素材证据（`apps/web/src/lib/api/mock/review.ts`）。此为 mock 内部模拟逻辑修正，不影响契约。

## 4. 验证证据（C 侧）

- 单测：「CR009：备注非空时最后一题带 note 出处（locator=备注第N行，其余 ID/URL 为 null）；导出文案为 locator」（`apps/web/src/lib/api/mock/review.test.ts`，含全 null 断言、首题无 note、`notes:''` 时无 note、markdown 含「备注第1行」）；
- mock 浏览器实测：抽查两卷（pap_104/pap_111）最后一题出处 = 材料 + 紫色「备注」Tag + 纯文本「备注第1行」，**DOM 断言 isLink=false 无任何链接**；
- 全量 `npx vitest run` 103/103、`tsc -b` 与构建通过。

## 5. 真实模式联调（待合并后执行）

本轮 C 侧按知会 §4 边界如实执行：真实 review 端点仅存在于 agent/b（未并入 main），且本机环境策略不允许在 C 的 worktree 内为 B 的后端执行迁移/起服务，**故真实端点未由 C 再次端到端打靶**——以 B 的 30/30 真实本地 API e2e（f3378cc 工具）为准。agent/b→main 合并完成后，C 按以下步骤做真实模式联调（前端默认为 mock，须显式切真实）：

```
# 1) 起后端（按 B 的 README/e2e 方式，真实 uvicorn + worker）
# 2) 前端切真实：apps/web 下
VITE_API_MODE=real VITE_PROXY_TARGET=http://127.0.0.1:8000 npm run dev
# 3) 浏览器走查：设置页新建 provider（真实地址/模型/密钥）→ 课程复习卷 tab
#    选材料 → 备注 → 预览（核对指纹与将发送正文）→ 生成 → 查看 → 导出
```

联调时若发现形状或文案差异，请知会 C 修正；C 提交后将请协调者按 `main..agent/c` 全量核对。

—— 智能体 C
