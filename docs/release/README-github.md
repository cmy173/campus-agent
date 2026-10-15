# 浙大学习工作台

本地网页工具：整理学在浙大的课件、作业和智云课堂回放，生成学习计划，并结合课程材料与备注生成复习卷。

## 当前可用

- 持久化受控浏览器登录、已有会话复用及平台连接检查。
- 学在浙大课程/课件/作业同步；智云课程发现、回放目录/详情、按需字幕。
- PDF、PPTX、DOCX正文解析，有限ZIP解包、任务与学习计划。
- 自定义兼容Chat Completions模型地址、模型名与本地保存的密钥。
- 复习卷材料预览、原文备注、历年卷上传参考、生成/修订、答案解析、Markdown导出及打印页。
- 正式出卷最低32768、最高65536输出token，按题量分配。具体模型的服务端限制仍适用。

PPT与作业为主要命题依据，历年卷辅助参考题型和难度。扫描件可能无法提取正文；不保证题目及答案学术正确，使用前应核对。

音频重新转写、AI字幕校对和板书识别仍属于独立试验，不包含在本发布包，也未自动加入命题材料。回放访问需要平台允许的登录和权限，不支持批量视频下载。

## 启动

需要Python 3.11或以上、Node.js及npm；Windows平台连接器默认使用已安装的Microsoft Edge。

在项目根目录运行：

```powershell
python tools/dev.py --setup
python tools/dev.py --check
python tools/dev.py --platforms xzzd zykt
```

浏览器打开终端显示的网页地址。受控浏览器若直接进入课程页，说明可能已复用登录，无须反复退出账号。

Windows使用包还包含“浙大学习工作台-修复版.exe”：与apps、tools等目录放在同一级，首次点击“初始化依赖”，完成后点击“启动”。EXE不能单独运行，未内置Node、完整后端环境和个人资料。

启动器自动执行数据库迁移，默认数据在var/personal.db，文件在var/personal-files，模型密钥在var/secrets，学校登录保存在仓库外的独立浏览器配置。首次启动不导入演示课程。

模型设置中的基础地址应为服务API地址。例如DeepSeek填写https://api.deepseek.com/v1，模型名填写服务实际提供的具体标识；管理后台地址不是模型API。

## 开发与目录

apps/api：FastAPI后端、迁移与测试；apps/web：React网页；connectors：平台连接器；packages/learning：解析/排程/出卷引擎；contracts：共享契约；tools：启动与打包工具。

源码包包含测试样本，不含用户数据、密钥、浏览器会话、安装依赖或构建缓存。开发演示数据与个人模式不同，请勿给个人数据库执行app.seed或测试建库。

重新构建Windows启动器：初始化依赖后运行tools/build_launcher.ps1。
