# AI 智能体一键安装指南 (AGENTS.md)

当用户要求你安装或更新本仓库（`quota-monitor` — Antigravity 额度与会话监控插件）时，请按以下步骤执行：

## 1. 获取源码与环境预检（零外部依赖，支持无 Git 环境）

本插件服务端仅使用 **Python 3.9+ 标准库**，无需 `pip install` 任何第三方包，也无需 Node.js。

1. **获取仓库源码**（若当前工作目录不在本仓库内）：
   - **优先判断 Git**：若本机已安装 `git`，执行 `git clone https://github.com/leaomiguel894-create/antigravity-quota-monitor.git` 到系统临时目录。
   - **无 Git 自动兜底**：若本机未安装 `git`，直接下载源码归档 `https://github.com/leaomiguel894-create/antigravity-quota-monitor/archive/refs/heads/main.zip` 并解压到系统临时目录（Windows 可用 PowerShell `Invoke-WebRequest` + `Expand-Archive`；macOS / Linux 可用 `curl -fsSL` + `unzip` 或 `python3 -m zipfile -e`）。

## 2. 运行跨平台安装器（自动配置 Python 绝对路径 + 免重启热加载）

解压或克隆进入仓库根目录后，直接运行内置安装脚本（脚本会自动跳过 Windows Store 假别名、写入无 BOM 标准 `mcp_config.json`，并自动调用本地 `RefreshMcpServers` 热刷新服务）：

- **Windows (PowerShell)**：
  ```powershell
  powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
  ```
- **macOS / Linux**：
  ```bash
  bash ./install.sh
  ```

## 3. 验证与交付提示

- **严禁关闭或强杀宿主客户端进程**（无需重启客户端）。
- 确认脚本输出 `Hot-reload status: Active & Refreshed`（或确认 `~/.gemini/config/plugins/quota-monitor/mcp_config.json` 已生成且 `command` 指向真实 Python 可执行文件）。
- 安装完成后告知用户：点击界面右上角 **Overview ➔ 插件 ➔ 额度监控**（或右侧辅助面板顶部的 **`+` ➔ 额度监控**）即可在右侧栏常驻打开面板。
