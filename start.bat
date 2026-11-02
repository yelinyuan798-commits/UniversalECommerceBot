@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo 请先运行 setup.bat。
  pause
  exit /b 1
)
start "" http://127.0.0.1:8765
.venv\Scripts\python main.py console
pause
