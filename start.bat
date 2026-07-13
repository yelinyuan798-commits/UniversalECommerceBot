@echo off
chcp 65001 >nul
title ͨ�õ���AI�ͷ�������

cd /d "%~dp0"
call venv\Scripts\activate.bat
python main.py
pause
