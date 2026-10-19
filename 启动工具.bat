@echo off
chcp 65001 >nul
title dump 翻译工作台
cd /d "%~dp0"

REM 优先使用系统 PATH 里的 python，找不到则提示
where python >nul 2>nul
if %errorlevel%==0 (
    python "%~dp0dump_gui.py" %*
) else (
    echo [错误] 未在 PATH 中找到 python 命令。
    echo 请安装 Python 3.8+ 并加入 PATH，或直接使用 Releases 里的 exe 版本。
    pause
)