# 技术选型

v0.2 实际选型：Python + FastAPI、原生 HTML/CSS/JavaScript、SQLite、pypdf/python-docx/python-pptx 与可选 OpenAI Responses HTTP 适配。前端无需 Node 或构建步骤，便于学生团队本机启动；如交互复杂度增长再迁移 React/Vue。以下表格保留最初候选比较，成本为工程判断而非报价。

| 层 | 对比 | 建议与代价 |
|---|---|---|
| 后端 | FastAPI vs 标准库 | [FastAPI 官方功能](https://fastapi.tiangolo.com/features/)提供 API schema 等能力；正式服务使用它。标准库 CLI 零外部依赖但不承担生产 Web 安全 |
| 前端 | React / Vue / Next.js | React 与 Vue 均可；暂选 React + Vite，若团队更熟 Vue 可替换。Next.js 的服务端渲染不是登录后校园工具的首要需求，初期避免多一层后端 |
| 编排 | 自定义 / Agents SDK / LangGraph | 先用显式状态机，调试成本低；代价是自行实现重试和持久任务。其他框架列为候选，本轮未验证其版本与功能，不作为依赖 |
| 数据库 | SQLite / PostgreSQL | [SQLite 官方说明](https://www.sqlite.org/whentouse.html)适合轻量本地场景；多写并发、运维要求上升后迁 PostgreSQL。避免共享网络文件直接多机读写 SQLite |
| 检索 | 字面匹配 / PostgreSQL 全文 / pgvector / FAISS | 从可评测的中文分段与关键词基线开始。[PostgreSQL 全文文档](https://www.postgresql.org/docs/current/textsearch-intro.html)支持检索排序；中文分词需另行验证。向量候选增加嵌入、索引维护与隐私成本，暂不接入 |
| 任务 | 同步 / 持久作业表 / Redis 队列 | 小型试点用作业表和独立 worker；视频等长任务才考虑 Redis，避免首版额外运维 |
| 解析 | pypdf / python-pptx / python-docx | 候选组合，正式采用前锁版本并验证中文表格、页码与异常文件。扫描 PDF 暂不支持 |
| 自动化 | 官方接口 / Playwright | 先官方许可接口；Playwright 仅在权限与平台条款核实后评估，页面变更维护成本高 |
| 音频 | 已有字幕 / ASR | 先字幕；ASR 需处理授权、耗时与算力，本期延期 |
| 接口 | REST / MCP | Web 先用 REST；MCP 仅在确有外部客户端需求时实现，避免同时维护两套入口 |

## 部署和预算

阶段 0 可在现有电脑离线运行，外部 API 费用为零。正式试点先单机应用、私有数据目录、备份；公网部署前完成身份和隔离。推荐容器化是后续实施建议，本仓库尚无容器部署文件。

月成本估算公式：固定主机 + 备份存储 + 模型输入 token×单价 + 输出 token×单价 + 嵌入 + 可选转写。例：10 人×每天 10 次×30 天=3,000 次/月；按每次输入 4,000、输出 500 token 估算即输入 1,200 万、输出 150 万 token。以上是预算假设，实际价格须选定供应商后核价；避免编造金额。

先记录每次请求 token、耗时、解析失败率和人工纠错量；设置每人额度、总预算、最大上下文和模型超时。维护工作重点是材料质量、认定规则版本和连接器变更，而非框架数量。
