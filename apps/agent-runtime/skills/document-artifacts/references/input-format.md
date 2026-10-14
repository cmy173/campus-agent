# 文档输入格式

UTF-8 JSON。报告使用 `title`、`subtitle`、`sections` 和 `sources`；Excel 使用 `sheets`。选择 xlsx 时至少提供一个 sheet，选择 PDF/Markdown 时至少提供一个 section。

```json
{
  "title": "实验资料整理",
  "subtitle": "2026 年秋季",
  "sections": [
    {"heading": "明确要求", "paragraphs": ["报告需要附上原始数据。"], "bullets": ["核对原始记录与报告中的数值。"]},
    {"heading": "资料清单", "table": {"columns": ["资料", "截止时间"], "rows": [["实验说明", "未知"]]}}
  ],
  "sources": ["实验说明.pdf，第2页"],
  "sheets": [{
    "name": "资料清单",
    "columns": ["资料", "页数", "发布日期", "截止时间"],
    "rows": [["实验说明", 3, {"date": "2026-10-13"}, "未知"]],
    "sources": ["实验说明.pdf"]
  }]
}
```

- `sections` 支持 heading、paragraphs、bullets、table。表格列数必须与每行一致；长报告自动分页，表头在新页重复。
- `sheets` 支持 name、columns、rows、sources。一个清单通常只需一张表，不为标题和说明另建多张空表。
- 单元格值可以是字符串、数字、布尔值、null、`{"date":"YYYY-MM-DD"}` 或 `{"formula":"=SUM(B5:B6)"}`。文本 `"=1+1"` 保持文本，只有明确的 formula 对象成为公式。日期、数值保持真实类型。
- 工作表第2行是标题，第4行是表头，第5行起为数据，公式引用据此编写。sources 在表格下方单独列出。普通文本不要包含超过 Excel 单元格上限的32767个字符；脚本会明确报错，不静默截断。
- `--formats` 可选 pdf、xlsx、md，逗号分隔；`--stem` 是文件名，不包含目录。`--font` 可指定可嵌入的中文 TrueType 字体；Windows 默认使用微软雅黑。
- 成功时 stdout 返回实际产物的 path/size/sha256 及 PDF 页数或表格行数。报错时不会自动交付文件，请按错误修复输入后重试。
