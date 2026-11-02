#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("docker", "infra", "local")]
    [string]$Mode = "docker",

    # Docker 模式：使用本地代码重新构建应用镜像后再启动。
    [switch]$Build,

    # 兼容旧用法：等价于 infra 模式，只启动基础设施。
    [switch]$InfraOnly,

    # 本地模式：默认启动全部应用；-NoFrontend 时只启动后端。
    [switch]$NoFrontend
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$DockerDir = Join-Path $Root "docker"
$EnvFile = Join-Path $DockerDir ".env"
$ComposeFile = Join-Path $DockerDir "docker-compose.yml"
$BaseComposeFile = Join-Path $DockerDir "docker-compose-base.yml"
$BuildComposeFile = Join-Path $DockerDir "docker-compose.build.yml"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "未找到 docker，请先安装并启动 Docker Desktop。"
}

if (-not (Test-Path -LiteralPath $EnvFile)) {
    throw "未找到配置文件：$EnvFile"
}

function Invoke-Compose {
    param(
        [string[]]$ComposeFiles,
        [string[]]$ComposeArguments
    )

    $arguments = @("compose", "--env-file", $EnvFile)
    foreach ($file in $ComposeFiles) {
        $arguments += @("-f", $file)
    }
    $arguments += $ComposeArguments

    & docker @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose 执行失败，退出码：$LASTEXITCODE"
    }
}

function Assert-File {
    param(
        [string]$Path,
        [string]$Hint
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "缺少 $Path。`n$Hint"
    }
}

function Stop-ApplicationContainers {
    # 只停应用容器，MySQL/Redis/MinIO/ES/Kibana 保持运行。
    Invoke-Compose -ComposeFiles @($ComposeFile) -ComposeArguments @(
        "stop", "app", "management-frontend"
    )
}

function Start-Infrastructure {
    # 先等四个核心依赖健康，再启动 Kibana。Kibana 只是观测工具，不阻塞应用启动。
    Invoke-Compose -ComposeFiles @($BaseComposeFile) -ComposeArguments @(
        "up", "-d", "--wait", "mysql", "redis", "minio", "es01"
    )
    Invoke-Compose -ComposeFiles @($BaseComposeFile) -ComposeArguments @(
        "up", "-d", "kibana"
    )
}

function Show-InfrastructureUrls {
    Write-Host ""
    Write-Host "基础设施已启动（应用没有启动）：" -ForegroundColor Green
    Write-Host "  MySQL    ：localhost:5455"
    Write-Host "  Redis    ：localhost:6379"
    Write-Host "  MinIO    ：http://localhost:9001"
    Write-Host "  Kibana   ：http://localhost:6601"
    Write-Host "  ES       ：http://localhost:1200"
}

function Start-LocalWindow {
    param(
        [string]$Title,
        [string]$WorkingDirectory,
        [string]$Command
    )

    $safeTitle = $Title.Replace("'", "''")
    $safeDirectory = $WorkingDirectory.Replace("'", "''")
    $windowCommand = "`$Host.UI.RawUI.WindowTitle = '$safeTitle'; Set-Location -LiteralPath '$safeDirectory'; $Command"
    Start-Process -FilePath "powershell.exe" `
        -ArgumentList @("-NoExit", "-Command", $windowCommand) `
        -WorkingDirectory $WorkingDirectory `
        -WindowStyle Normal | Out-Null
}

if ($Mode -eq "docker" -and -not $InfraOnly) {
    $composeFiles = @($ComposeFile)
    $composeArguments = @("up", "-d")

    if ($Build) {
        $composeFiles += $BuildComposeFile
        $composeArguments += "--build"
    }

    Invoke-Compose -ComposeFiles $composeFiles -ComposeArguments $composeArguments

    Write-Host ""
    Write-Host "Docker 模式已启动：" -ForegroundColor Green
    Write-Host "  前台：http://localhost"
    Write-Host "  API ：http://localhost:9380"
    Write-Host "  管理后台：http://localhost:8888"
    return
}

if ($Mode -eq "infra" -or $InfraOnly) {
    Stop-ApplicationContainers
    Start-Infrastructure
    Show-InfrastructureUrls
    return
}

# 本地模式统一交给 dev.ps1：
# - Docker 只负责基础设施；
# - 所有应用进程在同一个控制台输出带服务前缀的日志；
# - Ctrl+C 只停止本地应用，不停止 Docker 基础设施。
$devParameters = @{}
if ($NoFrontend) {
    $devParameters["NoFrontend"] = $true
}

& (Join-Path $PSScriptRoot "dev.ps1") @devParameters
exit $LASTEXITCODE
