@echo off
chcp 65001 >nul
title Universal E-Commerce Bot - һ������

cd /d "%~dp0"

echo ========================================
echo   ͨ�õ���AI�ͷ������� - һ������
echo ========================================
echo.

echo [1/4] �������⻷��...
python -m venv venv
call venv\Scripts\activate.bat
echo.

echo [2/4] ��װ����...
pip install -r requirements.txt -q
echo.

echo [3/4] ������ʾ���ļ�...
if not exist "prompts\classify_prompt.txt" copy "prompts\classify_prompt_example.txt" "prompts\classify_prompt.txt" >nul 2>&1
if not exist "prompts\price_prompt.txt" copy "prompts\price_prompt_example.txt" "prompts\price_prompt.txt" >nul 2>&1
if not exist "prompts\tech_prompt.txt" copy "prompts\tech_prompt_example.txt" "prompts\tech_prompt.txt" >nul 2>&1
if not exist "prompts\default_prompt.txt" copy "prompts\default_prompt_example.txt" "prompts\default_prompt.txt" >nul 2>&1
echo.

echo [4/4] ���������ļ�...
if not exist "config\.env" copy "config\.env.example" "config\.env" >nul 2>&1
if not exist "config\delivery_items.json" echo {"items":{}} > "config\delivery_items.json"
echo.

echo ========================================
echo   �������!
echo ========================================
echo.
echo   ���: ˫�� start.bat
echo   ����: �༭ config\.env ���� API_KEY �� Cookie
echo.
pause
