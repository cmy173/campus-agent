$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExecutable = Join-Path $projectRoot 'apps/api/.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonExecutable)) {
    throw '请先运行 python tools/dev.py --setup'
}
& $pythonExecutable -m pip install -r (Join-Path $PSScriptRoot 'launcher-requirements.txt')
if ($LASTEXITCODE -ne 0) { throw '打包依赖安装失败' }
& $pythonExecutable -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name '浙大学习工作台' --distpath $projectRoot `
    --workpath (Join-Path $projectRoot 'build/launcher') `
    --specpath (Join-Path $projectRoot 'build/launcher') `
    (Join-Path $PSScriptRoot 'launcher.py')
if ($LASTEXITCODE -ne 0) { throw 'EXE 构建失败' }
Write-Host "已生成：$(Join-Path $projectRoot '浙大学习工作台.exe')"
