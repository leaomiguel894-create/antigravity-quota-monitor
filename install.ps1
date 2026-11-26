# Antigravity Quota Monitor - Windows Installer (Pure ASCII, UTF-8 No-BOM output)
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$TargetDir = Join-Path $HOME ".gemini\config\plugins\quota-monitor"

function Test-RealPython39([string]$Candidate) {
    if ([string]::IsNullOrWhiteSpace($Candidate)) { return $null }
    if ($Candidate -like "*\Microsoft\WindowsApps\*") { return $null }
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & $Candidate -c "import sys; print(sys.executable if sys.version_info >= (3, 9) else '')" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) {
            $exe = ($out | Select-Object -Last 1).ToString().Trim()
            if ($exe -and (Test-Path -LiteralPath $exe)) {
                return $exe
            }
        }
    } catch {
    } finally {
        $ErrorActionPreference = $oldEap
    }
    return $null
}

Write-Host "==> [1/3] Checking Python 3.9+ environment..." -ForegroundColor Cyan
$PyExe = $null

foreach ($cmdName in @("python", "python3")) {
    if (-not $PyExe) {
        $cmds = Get-Command $cmdName -All -ErrorAction SilentlyContinue
        foreach ($c in $cmds) {
            $PyExe = Test-RealPython39 $c.Source
            if ($PyExe) { break }
        }
    }
}

if (-not $PyExe) {
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        $oldEap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $out = & $pyLauncher.Source -3 -c "import sys; print(sys.executable if sys.version_info >= (3, 9) else '')" 2>$null
            if ($LASTEXITCODE -eq 0 -and $out) {
                $cand = ($out | Select-Object -Last 1).ToString().Trim()
                if ($cand -and (Test-Path -LiteralPath $cand)) { $PyExe = $cand }
            }
        } catch {} finally {
            $ErrorActionPreference = $oldEap
        }
    }
}

if (-not $PyExe) {
    $searchPatterns = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python3*\python.exe"),
        (Join-Path $env:ProgramFiles "Python3*\python.exe"),
        "C:\Python3*\python.exe"
    )
    foreach ($pat in $searchPatterns) {
        $found = Get-Item $pat -ErrorAction SilentlyContinue | Sort-Object FullName -Descending
        foreach ($f in $found) {
            $PyExe = Test-RealPython39 $f.FullName
            if ($PyExe) { break }
        }
        if ($PyExe) { break }
    }
}

if (-not $PyExe) {
    Write-Host "ERROR: Python 3.9+ was not found on this machine." -ForegroundColor Red
    Write-Host "Please install Python 3.9 or newer from https://www.python.org/downloads/ and re-run." -ForegroundColor Yellow
    exit 1
}
Write-Host "    Found Python: $PyExe" -ForegroundColor Green

Write-Host "==> [2/3] Installing plugin files to $TargetDir ..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null

$Items = @("plugin.json", "README.md", "AGENTS.md", "LICENSE", "assets", "skills", "server")
foreach ($item in $Items) {
    $src = Join-Path $ScriptDir $item
    $dst = Join-Path $TargetDir $item
    if (Test-Path -LiteralPath $src) {
        if ((Get-Item -LiteralPath $src).PSIsContainer) {
            New-Item -ItemType Directory -Force -Path $dst | Out-Null
            Copy-Item -Path (Join-Path $src "*") -Destination $dst -Recurse -Force
        } else {
            Copy-Item -LiteralPath $src -Destination $dst -Force
        }
    }
}

$McpPath = Join-Path $TargetDir "mcp_config.json"
$McpObj = [ordered]@{
    mcpServers = [ordered]@{
        "quota-monitor" = [ordered]@{
            command = $PyExe
            args    = @(
                '${PLUGIN_ROOT}/server/mcp_server.py',
                '--data-dir=${PLUGIN_DATA}'
            )
        }
    }
}
$McpJson = ($McpObj | ConvertTo-Json -Depth 5) + "`n"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($McpPath, $McpJson, $Utf8NoBom)

Write-Host "==> [3/3] Hot-reloading Antigravity MCP server..." -ForegroundColor Cyan
$Endpoints = New-Object System.Collections.Generic.List[object]
if ($env:ANTIGRAVITY_LS_ADDRESS) {
    $Endpoints.Add(@{ Addr = $env:ANTIGRAVITY_LS_ADDRESS; Token = "$env:ANTIGRAVITY_CSRF_TOKEN" })
}
$DaemonDir = Join-Path $HOME ".gemini\antigravity\daemon"
if (Test-Path -LiteralPath $DaemonDir) {
    $DaemonFiles = Get-ChildItem -LiteralPath $DaemonDir -Filter "*.json" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 10
    foreach ($df in $DaemonFiles) {
        try {
            $info = Get-Content -LiteralPath $df.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($info.httpPort) {
                $Endpoints.Add(@{ Addr = "127.0.0.1:$($info.httpPort)"; Token = "$($info.csrfToken)" })
            }
        } catch {}
    }
}

$Reloaded = $false
foreach ($ep in $Endpoints) {
    try {
        $uri = "http://$($ep.Addr)/exa.language_server_pb.LanguageServerService/RefreshMcpServers"
        $hdrs = @{ "Content-Type" = "application/json"; "x-codeium-csrf-token" = $ep.Token }
        Invoke-RestMethod -Uri $uri -Method Post -Headers $hdrs -Body '{"serverName":"quota-monitor_quota-monitor"}' -TimeoutSec 2 | Out-Null
        $Reloaded = $true
        break
    } catch {}
}

if ($Reloaded) {
    Write-Host "    Hot-reload status: Active & Refreshed" -ForegroundColor Green
} else {
    Write-Host "    Hot-reload status: Installed (will load automatically when Antigravity runs)" -ForegroundColor Yellow
}

Write-Host "==> Success! Open Antigravity -> Top-right 'Overview -> Plugins' (or '+' in right side pane)." -ForegroundColor Green
