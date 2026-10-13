# DeepSeek API Key UI 配置功能

## 概述

为 Campus Agent 添加了在浏览器 UI 中临时配置 DeepSeek API Key 的功能，无需修改环境变量或重启服务器。

## 主要特性

### 1. 用户界面
- **位置**：设置页面 → "通知整理模型"部分
- **显示条件**：仅在未配置环境变量 `DEEPSEEK_API_KEY` 时显示输入框
- **输入验证**：最小长度 8 个字符（DeepSeek key 通常以 `sk-` 开头）
- **密码保护**：使用 `type="password"` 隐藏输入内容

### 2. 安全设计
```
用户浏览器 → HTTPS → FastAPI 服务端 → 内存存储（不持久化）
                                    ↓
                            DeepSeek API 端点
```

- **内存存储**：密钥保存在 Python 进程的字典中，重启后清除
- **账号隔离**：每个用户账号有独立的密钥存储空间
- **不暴露给前端**：API 只返回配置状态（boolean），不返回密钥值
- **优先使用**：运行时配置的密钥优先于环境变量

### 3. 工作流程

```
1. 用户登录系统
2. 进入设置页面
3. 看到 API Key 输入框（如果环境变量未配置）
4. 输入 sk-xxxxxxxxx
5. 点击"保存（仅本次会话）"
6. 后端验证并存储到 runtime_api_keys[owner]
7. 立即可以用于通知整理事件提取
8. 刷新页面或重启服务器后需要重新输入
```

## 代码变更

### 后端 (web.py)
```python
# 添加内存存储
runtime_api_keys = {}
runtime_keys_lock = threading.Lock()

# 添加配置端点
@app.post('/api/model/configure', status_code=200)
async def configure_model(request: Request):
    account = user(request, True)
    data = await payload(request)
    # 保存到内存字典
    with runtime_keys_lock:
        runtime_api_keys[account['owner']] = {'deepseek': key_value}

# 修改 extractor factory
def make_extractor_factory():
    def extractor_factory(owner=None):
        runtime_key = None
        if owner:
            with runtime_keys_lock:
                runtime_key = runtime_api_keys.get(owner, {}).get('deepseek')
        return notice_model.extractor(runtime_key=runtime_key)
    return extractor_factory
```

### 前端 (app.js + index.html)
```javascript
// 保存按钮处理
$('notice-api-key-save').onclick = async () => {
    const key = $('notice-api-key-input').value.trim();
    await api('/api/model/configure', {
        method: 'POST',
        json: { key_type: 'deepseek', key_value: key }
    });
    toast('API Key 已保存（仅本次会话有效）');
    await loadSettings(); // 刷新配置状态
};

// 根据配置状态显示/隐藏输入框
const apiKeySection = $('notice-api-key-section');
if (apiKeySection) {
    apiKeySection.classList.toggle('hidden', notice.configured);
}
```

### 模型适配器 (notice_model.py)
```python
def configuration(provider=None, runtime_key=None):
    """支持运行时密钥参数"""
    key = (runtime_key or os.environ.get('DEEPSEEK_API_KEY') or '').strip()
    return {'configured': bool(key) and valid, ...}

def extractor(provider=None, runtime_key=None):
    """优先使用运行时密钥"""
    key = (runtime_key or os.environ.get('DEEPSEEK_API_KEY') or '').strip()
    if not key:
        raise ModelFailure('NOT_CONFIGURED')
    return DeepSeekExtractor(config['model'], key)
```

## 测试覆盖

### Python 测试（43 个全部通过）
- ✅ 配置端点不泄露密钥
- ✅ 运行时密钥正确传递给 extractor
- ✅ 失败重试时保留上一次结果
- ✅ 并发请求处理
- ✅ 账户隔离

### 前端测试（9 个全部通过）
- ✅ 源卡片保持官方来源信息
- ✅ 事件卡片分离截止时间/集合时间/开始时间
- ✅ 模型分析需要明确同意
- ✅ 离线规则可用且不声称模型结果
- ✅ 未配置的提供方隐藏模型按钮
- ✅ 失败的运行保持上次结果可见
- ✅ 运行中的分析会轮询并阻止第二次运行
- ✅ 账户切换清除面板
- ✅ 部分结果说明哪些段落未分析

## 使用场景

### 适合使用的情况
1. **本地开发测试**：快速切换不同的 API Key 进行测试
2. **演示环境**：向他人展示功能时无需修改环境变量
3. **多用户共享服务器**：每个用户使用自己的密钥
4. **临时试用**：不想永久配置环境变量

### 不适合使用的情况
1. **生产部署**：应使用环境变量或密钥管理服务
2. **长期稳定配置**：环境变量更可靠
3. **自动化脚本**：UI 交互不适合自动化

## 注意事项

1. **重启失效**：服务器重启后所有运行时密钥都会丢失
2. **优先级**：运行时密钥 > 环境变量
3. **格式验证**：由 DeepSeek API 服务端验证密钥有效性
4. **错误提示**：如果密钥无效，会在通知整理时显示具体错误（AUTH_REJECTED 等）
5. **不影响现有功能**：未配置运行时密钥时，仍可使用环境变量或离线规则提取

## 相关文件

- `campus_agent/web.py` - API 端点和运行时密钥存储
- `campus_agent/notice_model.py` - 支持运行时密钥的 extractor
- `campus_agent/notice_analysis.py` - 传递 owner 给 extractor factory
- `campus_agent/static/index.html` - UI 输入框
- `campus_agent/static/app.js` - 前端逻辑
- `campus_agent/static/style.css` - 样式
- `tests/test_notice_analysis.py` - 后端测试
- `tests/frontend-notice-analysis.test.cjs` - 前端测试
- `docs/notion-api-key-ui.md` - 技术文档
