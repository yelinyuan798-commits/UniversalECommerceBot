#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
if [ ! -f config/.env ]; then cp config/.env.example config/.env; chmod 600 config/.env; fi
printf '\n安装完成。运行 ./start.command 打开本地商家工作台。\n'
