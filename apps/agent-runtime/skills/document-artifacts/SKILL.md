---
name: document-artifacts
description: 将已读取的资料整理成可下载的中文 PDF、Markdown 或 Excel 表格，保留来源和真实数据类型。
---

先用文件或网页工具读取用户指定材料，确定需要的内容与格式。不要把工具调用日志、应用内部路径或未核实的日期复制进成品。

普通报告和清单可用本技能的 `scripts/export_documents.py`，通过应用 Python 环境执行：

```powershell
& $env:DSH_PYTHON_EXECUTABLE -X utf8 $env:DSH_DOCUMENT_EXPORTER --input "文档内容.json" --output-dir "成果" --stem "资料整理" --formats pdf,xlsx,md
```

只生成用户需要的格式。先读取 [references/input-format.md](references/input-format.md)，再用 write 工具把已核对内容写成 JSON。脚本默认创建新文件，已有同名文件会拒绝覆盖；需要修订时换一个名称，保留之前交付的版本。

脚本是常用报告/表格的起点，不限制用户要求的复杂排版、分析、图表或其他文档类型；复杂需求可直接使用已安装的 reportlab、openpyxl、pypdf 和 Pillow 编写代码。任务不需要固定模板时不必套用脚本。

生成后核对实际 PDF 页数/正文和表格值、日期及公式，确认内容没有截断。需要检查版式时使用可用的预览或渲染工具。公式以公式对象明确传入，原始文本不会被自动转成公式；openpyxl 不计算公式缓存，未经过计算引擎验证时不能宣称公式结果已算好。

对每个完成的成果调用 `publish_file`，这样用户可以从聊天卡片预览和下载。仅返回本机路径不算完成交付。保留材料名称、资料 ID 或可访问来源 URL，日期未知就标记未知。
