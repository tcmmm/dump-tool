@echo off
chcp 65001 >nul
title dump 翻译工作台
cd /d "%~dp0"

set "PY=C:\Users\32283\.workbuddy\binaries\python\envs\default\Scripts\python.exe"

if exist "%PY%" (
    "%PY%" "%~dp0dump_gui.py" %*
) else (
    echo [错误] 未找到 Python 运行环境: %PY%
    echo 请确认已安装 WorkBuddy 管理版 Python, 或改用 python 命令启动。
    pause
)
