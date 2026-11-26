#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${HOME}/.gemini/config/plugins/quota-monitor"

echo "==> Checking Python 3.9+ ..."
PY_EXE="$(command -v python3 || command -v python || true)"
if [[ -z "${PY_EXE}" ]]; then
  echo "Error: Python 3.9+ is required but was not found in PATH." >&2
  exit 1
fi
PY_EXE="$("${PY_EXE}" -c 'import sys; assert sys.version_info >= (3, 9), "Need Python >= 3.9"; print(sys.executable)')"
echo "    Found Python: ${PY_EXE}"

echo "==> Installing quota-monitor to ${TARGET_DIR} ..."
mkdir -p "${TARGET_DIR}"
for item in plugin.json README.md AGENTS.md assets skills server; do
  if [[ -e "${SCRIPT_DIR}/${item}" ]]; then
    cp -Rf "${SCRIPT_DIR}/${item}" "${TARGET_DIR}/"
  fi
done

cat > "${TARGET_DIR}/mcp_config.json" <<EOF
{
  "mcpServers": {
    "quota-monitor": {
      "command": "${PY_EXE}",
      "args": [
        "\${PLUGIN_ROOT}/server/mcp_server.py",
        "--data-dir=\${PLUGIN_DATA}"
      ]
    }
  }
}
EOF

echo "==> Installed successfully! Open Antigravity -> Right Pane '+' -> '额度监控'."
