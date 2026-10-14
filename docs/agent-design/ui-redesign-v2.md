# V2 UI 改版与工作区指南

2026-10-14。范围是当前 React 产品结构，视觉品牌仍可继续设计。真实模型和校方登录验收独立于本文件。

## 产品入口

`/chat/:conversationId?` 是唯一学生主要入口。顶部不再提供今日、课程、计划、复习、同步等平行应用导航。聊天的日历、文件与成果、浏览器和“资料与任务”按当前任务打开；模型、校园账号、可用时间和数据导出集中在设置抽屉。

复杂编辑能力仍存在于任务工作区：课程资料上传、解析版本、作业、回放和复习生成；学习任务生成与反馈；计划预览、应用、撤销和锁定；复习卷修订与导出。它们是按需挂载的组件，不再提供独立工作台页面。旧重复首次引导已删除，平台登录与课程导入复用账号设置和课程工作区。

## 改版边界

| 职责 | 文件 | 改视觉时的规则 |
| --- | --- | --- |
| 请求与事件 | `features/agent/api.ts`、`useTurnEvents.ts`、`lib/api` | 不复制请求实现，不用组件样式改变重试、归属或幂等 |
| 会话状态 | `useConversationController.ts`、`draft.ts` | 草稿、发送、取消、滚动恢复与课程上下文保留同一控制器 |
| 工作区状态 | `useTaskWorkspace.ts`、`workspaceRoutes.ts` | 用 URL 保存工作区及实体定位；切换工作区不切换会话 ID |
| 文件与浏览器状态 | `useConversationFiles.ts`、`useBrowserWorkspace.ts` | 继续使用同一文件归属、动作确认和回执事实源 |
| 展示 | `AgentPage.tsx`、`TurnView.tsx`、`TaskWorkspace.tsx`、各编辑器 | 工具结果使用稳定投影；不要把业务授权放在 CSS/布局里 |
| 布局 | `ChatLayout.tsx`、`agent.css`、`AppLayout.tsx` | 替换 slot 排布，保留 transcript/messages ref 和输入框行为 |
| 视觉 | `agent-theme.css`、统一 Ant Design ConfigProvider | 改 tokens；局部主题作用于展示根节点，避免全局泄漏 |

`ChatLayout` 只接收 toolbar/context/notices/transcript/setup/composer 六个展示 slot。布局不持有 provider、API、草稿或工具执行状态。现有 `useConversationController` 接口本次未改变。增加工具只扩展统一事件投影或产物组件，不为每个工具增加主导航。

## 换布局实例

开发启动后访问 `/__dev/chat-layout`。相同固定会话和原生工具事件通过同一 `projectTurnEvents`/`ToolActivities` 渲染，可切换对话布局与侧栏布局、柔和与高对比 tokens。切换后保留输入草稿、答案、来源及已完成工具结果；不请求模型/API，不作为第二产品入口。生产构建不注册该路由，也不输出预览 chunk。

后续设计人员可以先在这里替换布局和 tokens，再将通过验证的 `ChatLayout` 默认选项用于实际聊天。不要另建新的模型控制器或会话存储。

## 旧 URL 过渡

这些映射保留至 V2.5 发布，届时检查外部书签与文档链接后移除；现阶段只有跳转，没有旧页面挂载。

| 旧 URL | 当前入口 |
| --- | --- |
| `/today` | `/chat?workspace=tasks` |
| `/courses`、`/courses/:id` | `/chat?workspace=courses&course=:id`（课程列表省略 course） |
| `/plan` | `/chat?workspace=calendar` |
| `/reviews`、`/reviews/:id` | `/chat?workspace=reviews&paper=:id`（列表省略 paper） |
| `/settings` | `/chat?workspace=settings` |
| `/sync`、`/onboarding` | `/chat?workspace=settings&settings_tab=accounts` |

课程 tab、复习 revision/base 与列表筛选 query 保留。工作区内部 `WorkspaceLink` 保留当前 `/chat/:conversationId`，避免新建会话或丢失草稿。独立打印页 `/print/reviews/:paperId` 继续存在，它是产物打印视图。

## 已验证与限制

- 相关测试：`npm --prefix apps/web test -- --maxWorkers=2 src/features/agent/AgentPage.test.tsx src/features/agent/workspaceNavigation.test.tsx src/features/agent/ChatLayoutPreview.test.tsx src/features/settings/PersonalSettings.test.tsx src/features/plan/PlanEditor.test.tsx src/features/today/TaskWorkspace.test.tsx src/features/sync/ConnectionsSettings.test.tsx`，7 文件 32 项通过，最终复验 44.29 秒。覆盖会话/草稿/不确定重试、计划、反馈、连接、导出、工作区导航及换布局。
- 实际独立合成 API/Harness 查询资料成功；课程编辑抽屉在同一会话中打开，刷新恢复工作区与草稿。1280×900 桌面和 390×844 窄屏截图实际完整覆盖视口，页面无横向溢出，输入区域保留在视口内。
- 狭窄课程表格仍使用内部横向滚动，复杂编辑表单不承诺所有字段在单屏内展示。最终视觉品牌、长任务/调度最终交互和性能基准继续由主 Goal 验收。
- TypeScript/Vite 生产构建通过，Ant Design UI vendor 大块提示保留。页面和编辑器按需分块；不把隐藏构建提示当成性能改善。
- 独立生产预览真实恢复同一会话，课程编辑器懒加载成功；生产 `/__dev/chat-layout` 返回“页面不存在”。生产预览与开发页使用不同端口，sessionStorage 草稿按 origin 隔离；生产验证没有据此声称跨 origin 同步草稿。

真实浏览器证据：`var/agent-integration-o78y5qkz/ui-workspace-evidence.json`；`chat-desktop.jpg`、`chat-narrow.jpg`、`course-workspace-desktop.jpg`、`course-workspace-narrow.jpg`、`accounts-settings-narrow.jpg`、`layout-preview-desktop.jpg`、`layout-preview-narrow.jpg`。
