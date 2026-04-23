#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
if [ -x "${SCRIPT_DIR}/.venv/bin/python" ]; then
  exec "${SCRIPT_DIR}/.venv/bin/python" "${SCRIPT_DIR}/cc-session-monitor.py" "$@"
fi

if command -v python3 >/dev/null 2>&1; then
  exec python3 "${SCRIPT_DIR}/cc-session-monitor.py" "$@"
fi

printf 'python3 not found and no local .venv at %s\n' "${SCRIPT_DIR}/.venv/bin/python" >&2
exit 127
