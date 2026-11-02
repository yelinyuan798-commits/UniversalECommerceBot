#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  printf '请先在项目目录运行 bash setup.sh。\n'
  exit 1
fi
if command -v open >/dev/null 2>&1; then (sleep 2; open http://127.0.0.1:8765) & fi
exec .venv/bin/python main.py console
