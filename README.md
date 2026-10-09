# Campus Agent

面向浙江大学学生的校园资料工作台。**v0.2 已实现可运行的本地 Web 闭环**：上传资料、提取待办、检索原文、核对时间及删除资料。支持由用户在本机配置的 OpenAI 接口，默认关闭。

这不是学校官方服务。目前未接入真实校园账号、实时通知、自动报名或真实学分认定。

![本地工作台预览（合成演示资料）](docs/assets/preview.jpg)

交付范围、验证结果和后续事项见 [成果清单](docs/delivery.md)。

## 已实现

- Web 对话、资料库、待办、活动演示和设置，含窄屏布局。
- 独立本地账号、密码哈希、会话/CSRF 校验及按账号隔离的 SQLite 数据。
- TXT、PDF、DOCX、PPTX 上传、异步解析、去重、状态与错误提示。
- 按行、PDF 页、DOCX 段落/表格、PPTX 页定位，引用可回到原文。
- 通知候选待办、明确截止时间提取、人工修正与完成状态。
- 本地中文关键词检索，以及可选 OpenAI Responses 问答、引用校验和失败回退。
- 单份/全部资料删除，级联清理片段与待办；脱敏操作日志。
- 保留阶段 0 的 CLI、Mock Connector 和活动冲突规则。

## 启动

安装 Python 3.10+，在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m campus_agent.server
```

打开 **http://127.0.0.1:8765**，创建本地账号，点击“体验演示资料”。不要使用校园密码。后续可运行 `./start.ps1`，Ctrl+C 停止。详细的模型配置、备份和常见限制见 [本地运行说明](docs/local-setup.md)。

## 当前状态

| 能力 | 状态 |
|---|---|
| 上传、检索、原文引用、待办核对 | 已实现并以合成资料验证 |
| OpenAI 问答 | 接口和模拟响应测试完成，真实密钥/模型尚未联调 |
| 活动推荐 | 仅虚构数据，不代表浙大学分规则 |
| 真实平台、SSO、自动报名 | 未接入，需要合法接口与授权 |
| OCR、音视频、完整课程规划 | 未实现 |
| 公网多人服务 | 未完成，当前限定本机试用 |

本地检索使用中文双字片段及英文词匹配，不等同于语义检索。日期不明确时保留待核对。来源存在性校验不等于模型事实正确性保证。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用合成资料与模拟模型，不消耗 API 额度。实际验证结果见 [验证记录](docs/validation.md)。

## 文档与代码

[愿景](docs/vision.md) · [需求](docs/requirements.md) · [架构](docs/architecture.md) · [平台调研](docs/integrations.md) · [安全](docs/security.md) · [技术选型](docs/tech-stack.md) · [MVP](docs/mvp.md) · [路线图](docs/roadmap.md) · [待决策事项](docs/open-questions.md) · [API](docs/api.md)

`core.py` 为阶段 0 原型；`web.py` 为 REST、身份与编排；`storage.py` 管理 SQLite；`parser_worker.py` 解析文件；`services.py` 提取与检索；`llm.py` 适配 OpenAI；`static/` 为无需构建的原生 HTML/CSS/JS 前端；`server.py` 为启动入口。

数据默认保存在 `data/`，不要提交或分享。许可证仍待团队决定。
