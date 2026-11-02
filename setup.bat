@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m venv .venv
if errorlevel 1 goto fail
.venv\Scripts\python -m pip install -r requirements.txt
if errorlevel 1 goto fail
.venv\Scripts\python -m playwright install chromium
if errorlevel 1 goto fail
if not exist config\.env copy config\.env.example config\.env >nul
echo 安装完成，请双击 start.bat。
pause
exit /b 0
:fail
echo 安装失败，请查看上面的错误。需要 Python 3.11 或更高版本。
pause
exit /b 1
