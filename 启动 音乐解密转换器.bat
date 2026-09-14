@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 音乐解密转换器

set PY=C:\Users\xth26\AppData\Local\Programs\Python\Python311\python.exe
if not exist "%PY%" (
  echo 找不到 Python：%PY%
  echo 请修改本文件里的 PY 变量，或改用 py 命令启动。
  pause
  exit /b 1
)

"%PY%" server.py
echo.
pause
