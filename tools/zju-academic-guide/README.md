# ZJU 教务信息采集工具（zju-academic-guide）

在你**本人登录**浙大通行证后，自动把教务系统里的学业材料抓下来、整理成
`var/agent-input/` 目录，交给 App 里的大模型阅读与分析。

**本工具只负责抓取与整理，不做解析、不算学分、不下结论。**
培养方案 PDF 原样交出——大模型能直接读 PDF，比在本工具里写一堆易错的解析规则更可靠。

---

## 设计要点

| 原则 | 做法 |
|---|---|
| 不接触密码 | 由用户在浏览器窗口里自己登录；程序只检测登录是否完成 |
| 以登录者为准 | 登录后读出**当前登录者**的学号/专业/年级，不用配置里的旧值 |
| 每次完整重读 | 每次运行清空旧材料重新抓——课表与选课目录每学期变化 |
| 方案按学籍定位 | 专业 + 学院 + 入学年份 三层匹配，并处理转专业、方案逐年修订 |
| 如实标注不确定 | 匹配过程与材料缺口都写进 `manifest.json`，不替模型下结论 |

---

## 快速开始

```powershell
# 首次：创建虚拟环境并安装依赖（playwright + pypdf）
setup.cmd

# 运行
run.cmd
```

运行后：

1. 弹出浏览器窗口 → **你自己**输入学号与密码登录
2. 程序自动采集（只读，不触发任何选课/报名操作）
3. 生成 `var/agent-input/`，并自动打开该目录

---

## 产出：`var/agent-input/`

| 文件 | 内容 |
|---|---|
| `manifest.json` | 材料清单：来源、时间、匹配过程、缺口说明 |
| `prompt.md` | 建议交给大模型的任务描述 |
| `README.txt` | 给人看的说明 |
| `01-学籍信息.json` | 学号、姓名、专业、年级、学院、班级 |
| `02-培养方案.pdf` | **本人专业的完整培养方案**（教务导出） |
| `02-培养方案列表-全部专业.json` | 全部专业 × 全部年级的方案概要 |
| `03-课表.json` | 本人本学期课表 |
| `04-选课-全部课程.json` | 选课系统里的全部课程（遍历所有分组去重） |

### `manifest.json` 的关键字段

```jsonc
{
  "identity":     { "student_id": "...", "major": "统计学", "enroll_year": 2025, ... },
  "freshness":    { "note": "每次登录都会清空旧材料并完整重抓……" },
  "matched_plan": {
    "matched_by": "专业+学院+入学年份（2025 级）精确匹配",
    "plan_year": 2025,
    "total_required_credits": 146.5,
    "notes": ["该专业存在多个年份版本：2026/2025/2024/...；已选用与学籍入学年份相符的一版"],
    "same_major_versions": [ /* 该专业各年份版本 */ ]
  },
  "files": [ /* 每份材料及其用途 */ ],
  "limits": [ "本程序不读取成绩，因此无法计算个人已修学分与剩余学分" ]
}
```

---

## 已实测的教务接口

以下接口均在真实登录会话下验证通过（2026-10，2025 级）。

### 1. 学籍信息（本人专业/年级/学院/班级）

```
GET /jwglxt/zxzygl/zxzyqr_cxZxzyxsxxIndex.html?gnmkdm=N3015&layout=default&su=<学号>
```

页面为 JS 渲染，按标签取字段即可：`类（专业）` / `行政班` / `学院` / `学号` / `姓名`。

> 注意：`/xsxxxggl/xsgrxxwb_cxXsgrxx.html?gnmkdm=N100801` 是 **404**，不要用。

### 2. 培养方案列表（全部专业 × 全部年级）

```
POST /jwglxt/pyfagl/pyfaxxcx_cxPyfaxscxIndex.html?doType=query&gnmkdm=N153020&su=<学号>
Content-Type: application/x-www-form-urlencoded; charset=UTF-8
X-Requested-With: XMLHttpRequest

synj=<入学年份>&xydm=&zydm=&dlbs=&jxjhh=&_search=false&nd=1
&queryModel.showCount=100&queryModel.currentPage=<页>
&queryModel.sortName=synj desc, xymc, jxjhh&queryModel.sortOrder=asc&time=0
```

- 返回 JSON，字段含 `synj`(年级) `zymc`(专业) `xymc`(学院) `zydm` `jxjhh`
  `zdbyxf`(最低毕业学分) `zyhxkc`(核心课程名) `pymb`(培养目标) 等。
- **路径是 Index 页本身 + `doType=query`**；`_cxPyfaxx.html` 之类会返回 910。
- 不传 `synj` 可用来**探测当前存在哪些年级**（本工具据此自动发现年级，不写死）。

### 3. 培养方案 PDF（方案详情）

```
GET /jwglxt/pyfagl/pyfayl_cxPyfaylPdf.html?id=<方案ID>&doType=query&gnmkdm=N153020&su=<学号>
```

返回 **PDF** 字节（不是 JSON）。方案 ID 与 `jxjhh` 的关系：
`id = "0" + jxjhh[1:]`（观测：`jxjhh=20250613` → `id=020250613`）；
`jxjhh` 本身也能取到。

> 即前端 PDF 查看器的来源：
> `/jwglxt/js/plugins/pdfjs/web/viewer.htm?file=<上面的路径>&gnmkdm=N153020`

### 4. 选课系统（全部课程）

```
POST /jwglxt/xsxk/zzxkghb_cxZzxkGhbKcList.html
Content-Type: application/x-www-form-urlencoded; charset=UTF-8

dl=<分组>&lx=<类型>&xkmc=&kcbs=&nj=<年级>&xn=<学年>&xq=<学期>
&zydm=<专业代码>&jxjhh=&xnxq=(<学年>-<学期>)-&kspage=<起>&jspage=<止>
```

- 返回 JSON 数组，字段含 `kcdm`(课程号) `kcmc`(课程名) `kcxx`(学分~周学时)
  `kclb`(类别) `xkkh`(教学班号) 等。
- **课程按分组懒加载**：遍历 `dl` 才能取全。页面上的分组来自 `[data-dl]` 元素，
  例如：`xk_b`(全部课程) `xk_1`(本类) `xk_1_1`(跨类) `E`(思政军体) `F`(外语)
  `G`(计算机) `T`(自然科学通识) `Z`(专业基础) `zy_qb`(所有类专业) `xk_8`(体育)
  `zhct`/`sjwm`/`ddsh`/`kjcx`/`wysm`/`smts`/`byjy`(通识各类) `xhxk`(通识核心)
  `gjhkc`(国际化) 等。
- 同时可用页面上的 `kspage`/`jspage` 翻页（`jspage = kspage + 页长 - 1`）。
- 搜索上下文（学年/学期/年级/专业）从页面隐藏域读取：
  `#xn` `#xq` `#nj` `#zydm`。

### 5. 课表

```
POST /jwglxt/kbcx/xskbcx_cxXsKb.html?gnmkdm=N253508&su=<学号>
```

---

## 培养方案匹配策略

培养方案**逐年修订**（实测同一专业不同年份学分不一致），用户还可能**转专业**，
因此匹配"以专业为主、年份为辅"，并把过程如实记录：

| 顺序 | 规则 | 适用情形 |
|---|---|---|
| 1 | 专业 + 学院 + 入学年份 | 正常情况 |
| 2 | 专业 + 学院（取入学年份及之前最近一版） | 转专业 / 新专业 |
| 3 | 专业（不限学院） | 院系名写法差异 |
| 4 | 专业名包含匹配 | 最后手段，标注建议人工确认 |

结果写入 `manifest.json` 的 `matched_plan`，包含 `matched_by`、`notes` 与
`same_major_versions`，供模型判断适用性。

---

## 已知限制

- **不读取成绩**，因此无法计算个人已修/剩余学分（如需，可另加成绩接口）。
- 培养方案 PDF 由教务导出，历史上有过与网页版内容不一致的情况（例如某门课
  在 PDF 中缺失）。分析时应结合网页版核对。
- 选课课程池按轮次开放，非选课季可能为空。
- 登录态保存在本机 `var/` 下，仅本机可用；工具只读，不提交任何表单。

---

## 依赖

```
playwright   # 浏览器自动化（复用系统 Edge，channel="msedge"）
pypdf        # setup.cmd 一并安装，供其他脚本使用
```

`setup.cmd` 会依次尝试：DSH 内置 Python → 标准安装路径 → `where python`
（**跳过 WindowsApps 下的 Microsoft Store 占位程序**，它执行时会返回 9009）
→ `py` 启动器，并逐个验证解释器真的能运行。

---

## 文件说明

| 文件 | 说明 |
|---|---|
| `academic_guide.py` | 主程序：登录、采集、整理材料包 |
| `kill_stale_browser.py` | 清理上次异常退出残留的 Edge 进程 |
| `setup.cmd` | 一次性环境准备（建虚拟环境、装依赖） |
| `run.cmd` | 启动器（纯 ASCII，避免 cmd 代码页问题） |
