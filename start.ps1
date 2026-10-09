param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$venvPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host 'Please run the setup commands in README.md first.'
    exit 1
}
Write-Host "Campus Agent: http://127.0.0.1:$Port (Ctrl+C to stop)"
& $venvPython -m campus_agent.server --port $Port
exit $LASTEXITCODE
