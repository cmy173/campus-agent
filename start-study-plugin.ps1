param(
    [switch]$Setup,
    [string]$Python = 'python'
)
$ErrorActionPreference = 'Stop'
$pluginRoot = Join-Path $PSScriptRoot 'plugins/zju-study'
if (-not (Test-Path -LiteralPath (Join-Path $pluginRoot 'tools/dev.py'))) {
    throw '学习插件源码不完整，请下载完整仓库。'
}
Push-Location $pluginRoot
try {
    if ($Setup) {
        & $Python tools/dev.py --setup
        if ($LASTEXITCODE -ne 0) { throw '学习插件依赖初始化失败，请查看上方日志。' }
        & ./apps/api/.venv/Scripts/python.exe -m pip install -e plugins/zju-study
        if ($LASTEXITCODE -ne 0) { throw 'MCP 插件依赖安装失败，请查看上方日志。' }
        Write-Host '初始化完成。运行 ./start-study-plugin.ps1 启动学习插件。'
    } else {
        $backendPython = Join-Path $pluginRoot 'apps/api/.venv/Scripts/python.exe'
        if (-not (Test-Path -LiteralPath $backendPython)) {
            throw '请先运行 ./start-study-plugin.ps1 -Setup。'
        }
        & $backendPython tools/dev.py --platforms xzzd zykt --auto-ports --open-browser --state-file (Join-Path $pluginRoot 'var/runtime.json')
        if ($LASTEXITCODE -ne 0) { throw '学习插件启动失败，请查看上方日志。' }
    }
} finally {
    Pop-Location
}
