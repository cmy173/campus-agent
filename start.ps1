param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 launch.py --port $Port
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python launch.py --port $Port
} else {
    Write-Host 'Install Python 3.12 from https://www.python.org/downloads/ and enable Add Python to PATH.'
    exit 1
}
exit $LASTEXITCODE
