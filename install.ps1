$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$TargetDir = Join-Path $HOME ".gemini\config\plugins\quota-monitor"

Write-Host "==> Checking Python 3 environment..." -ForegroundColor Cyan
$PyExe = $null
if (Get-Command python -ErrorAction SilentlyContinue) {
    $ver = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
    if ($LASTEXITCODE -eq 0 -and [version]$ver -ge [version]"3.9") {
        $PyExe = (& python -c "import sys; print(sys.executable)").Trim()
    }
}
if (-not $PyExe -and (Get-Command py -ErrorAction SilentlyContinue)) {
    $PyExe = (& py -3 -c "import sys; print(sys.executable)" 2>$null).Trim()
}
if (-not $PyExe) {
    Write-Error "Python 3.9+ not found. Please install Python 3.9 or newer first."
    exit 1
}
Write-Host "    Found Python: $PyExe" -ForegroundColor Green

Write-Host "==> Installing quota-monitor to $TargetDir ..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
$Items = @("plugin.json", "README.md", "AGENTS.md", "assets", "skills", "server")
foreach ($item in $Items) {
    $src = Join-Path $ScriptDir $item
    if (Test-Path $src) {
        Copy-Item -Path $src -Destination $TargetDir -Recurse -Force
    }
}

$McpConfig = @{
    mcpServers = @{
        "quota-monitor" = @{
            command = $PyExe
            args    = @(
                "`${PLUGIN_ROOT}/server/mcp_server.py",
                "--data-dir=`${PLUGIN_DATA}"
            )
        }
    }
} | ConvertTo-Json -Depth 5
Set-Content -Path (Join-Path $TargetDir "mcp_config.json") -Value $McpConfig -Encoding UTF8

Write-Host "==> Done! Open Antigravity -> Right Pane '+' (or Overview -> Plugins) -> '额度监控'." -ForegroundColor Green
