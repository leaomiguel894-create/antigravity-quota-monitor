# AI 智能体一键安装指南 (AGENTS.md)

当用户要求你安装或更新本仓库（`quota-monitor` — Antigravity 额度与会话监控插件）时，请按以下步骤执行：

## 1. 环境预检（零外部依赖）

本插件服务端仅使用 **Python 3.9+ 标准库**，无需 `pip install` 任何第三方包，也无需 Node.js。

先检测本机可用的 Python 3 可执行文件绝对路径：
- **Windows (PowerShell)**：`(Get-Command python -ErrorAction SilentlyContinue).Source`（若无则尝试 `py -3 -c "import sys; print(sys.executable)"`）
- **macOS / Linux**：`command -v python3 || command -v python`

确认版本 `>= 3.9`。

## 2. 安装到 Antigravity 全局插件目录

无需修改客户端 `app.asar`，无需重启客户端，直接安装到官方标准插件目录：
- **目标路径**：`~/.gemini/config/plugins/quota-monitor`（Windows 下即 `$HOME\.gemini\config\plugins\quota-monitor`）

执行步骤：
1. 创建目标目录并将本仓库中的 `plugin.json`、`mcp_config.json`、`README.md`、`assets/`、`skills/`、`server/` 复制进去。
2. 将目标目录下 `mcp_config.json` 中的 `"command"` 字段更新为第 1 步检测到的本机 Python 可执行文件绝对路径（避免部分系统 GUI 进程 `PATH` 不含 `python`）。

## 3. 热刷新 MCP 服务并验证

在 Antigravity 终端中通知本地 Language Server 重新加载该 MCP 服务（免重启即时生效）：

- **macOS / Linux**：
  ```bash
  curl -sf -X POST \
    "http://${ANTIGRAVITY_LS_ADDRESS:-localhost:5387}/exa.language_server_pb.LanguageServerService/RefreshMcpServers" \
    -H "Content-Type: application/json" \
    -H "x-codeium-csrf-token: ${ANTIGRAVITY_CSRF_TOKEN}" \
    -d '{"serverName": "quota-monitor_quota-monitor"}'
  ```
- **Windows (PowerShell)**：
  ```powershell
  $ls = if ($env:ANTIGRAVITY_LS_ADDRESS) { $env:ANTIGRAVITY_LS_ADDRESS } else { "localhost:5387" }
  Invoke-RestMethod -Uri "http://$ls/exa.language_server_pb.LanguageServerService/RefreshMcpServers" `
    -Method Post -Headers @{ "Content-Type"="application/json"; "x-codeium-csrf-token"=$env:ANTIGRAVITY_CSRF_TOKEN } `
    -Body '{"serverName":"quota-monitor_quota-monitor"}'
  ```

安装完成后告知用户：点击界面右上角 **Overview ➔ 插件 ➔ 额度监控**（或右侧辅助面板顶部的 **`+` ➔ 额度监控**）即可在右侧栏打开面板。
