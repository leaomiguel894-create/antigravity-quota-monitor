#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${HOME}/.gemini/config/plugins/quota-monitor"

echo "==> [1/3] Checking Python 3.9+ environment..."
PY_EXE=""
for cand in python3 python; do
  if command -v "${cand}" >/dev/null 2>&1; then
    resolved="$("${cand}" -c 'import sys; print(sys.executable if sys.version_info >= (3, 9) else "")' 2>/dev/null || true)"
    if [[ -n "${resolved}" ]]; then
      PY_EXE="${resolved}"
      break
    fi
  fi
done

if [[ -z "${PY_EXE}" ]]; then
  echo "ERROR: Python 3.9+ is required but was not found in PATH." >&2
  exit 1
fi
echo "    Found Python: ${PY_EXE}"

echo "==> [2/3] Installing plugin files to ${TARGET_DIR} ..."
mkdir -p "${TARGET_DIR}"
for item in plugin.json README.md AGENTS.md LICENSE assets skills server; do
  if [[ -e "${SCRIPT_DIR}/${item}" ]]; then
    rm -rf "${TARGET_DIR:?}/${item}"
    cp -R "${SCRIPT_DIR}/${item}" "${TARGET_DIR}/${item}"
  fi
done

"${PY_EXE}" - "${TARGET_DIR}/mcp_config.json" "${PY_EXE}" <<'PYEOF'
import json, pathlib, sys
mcp_path = pathlib.Path(sys.argv[1])
py_exe = sys.argv[2]
cfg = {
    "mcpServers": {
        "quota-monitor": {
            "command": py_exe,
            "args": [
                "${PLUGIN_ROOT}/server/mcp_server.py",
                "--data-dir=${PLUGIN_DATA}",
            ],
        }
    }
}
mcp_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
PYEOF

echo "==> [3/3] Hot-reloading Antigravity MCP server..."
"${PY_EXE}" - <<'PYEOF'
import json, os, pathlib, urllib.request

endpoints = []
if os.environ.get("ANTIGRAVITY_LS_ADDRESS"):
    endpoints.append((os.environ["ANTIGRAVITY_LS_ADDRESS"], os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "")))
d = pathlib.Path.home() / ".gemini" / "antigravity" / "daemon"
if d.is_dir():
    for fp in sorted(d.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:10]:
        try:
            info = json.loads(fp.read_text(encoding="utf-8"))
            if info.get("httpPort"):
                endpoints.append((f"127.0.0.1:{info['httpPort']}", str(info.get("csrfToken") or "")))
        except Exception:
            pass

reloaded = False
for addr, tok in endpoints:
    try:
        req = urllib.request.Request(
            f"http://{addr}/exa.language_server_pb.LanguageServerService/RefreshMcpServers",
            data=b'{"serverName":"quota-monitor_quota-monitor"}',
            headers={"Content-Type": "application/json", "x-codeium-csrf-token": tok},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2).read()
        reloaded = True
        break
    except Exception:
        continue

print("    Hot-reload status:", "Active & Refreshed" if reloaded else "Installed (will load automatically when Antigravity runs)")
PYEOF

echo "==> Success! In Antigravity, click top-right 'Overview -> Plugins' (or '+' in the right side pane) to open Quota Monitor."
