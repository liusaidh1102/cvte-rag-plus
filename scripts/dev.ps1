#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$SkipInfra,
    [switch]$InfraOnly,
    [switch]$NoFrontend,
    [switch]$FrontendOnly,

    # 可选：显式指定 uv 安装的 Python 解释器。
    [string]$BasePython = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $PSScriptRoot "dev_runner.py"

if (-not (Test-Path -LiteralPath $Runner)) {
    throw "缺少启动器：$Runner"
}

if (-not $BasePython) {
    $Pyvenv = Join-Path $Root ".venv\pyvenv.cfg"
    if (-not (Test-Path -LiteralPath $Pyvenv)) {
        throw "缺少 $Pyvenv。请先创建 .venv。"
    }

    $HomeLine = Get-Content -LiteralPath $Pyvenv |
        Where-Object { $_ -match '^\s*home\s*=\s*(.+?)\s*$' } |
        Select-Object -First 1
    if (-not $HomeLine) {
        throw "$Pyvenv 中没有 home 配置。"
    }

    $PythonHome = [regex]::Match($HomeLine, '^\s*home\s*=\s*(.+?)\s*$').Groups[1].Value
    $BasePython = Join-Path $PythonHome "python.exe"
}

if (-not (Test-Path -LiteralPath $BasePython)) {
    throw "Python 解释器不存在：$BasePython"
}

$RunnerArguments = @($Runner)
if ($SkipInfra) { $RunnerArguments += "--skip-infra" }
if ($InfraOnly) { $RunnerArguments += "--infra-only" }
if ($NoFrontend) { $RunnerArguments += "--no-frontend" }
if ($FrontendOnly) { $RunnerArguments += "--frontend-only" }

Write-Host "使用 Python：$BasePython" -ForegroundColor DarkGray
$Code = 1
Push-Location $Root
try {
    & $BasePython @RunnerArguments
    $Code = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $Code