@echo off
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
chcp 65001 >nul
where py >nul 2>nul && (py -3 "%~dp0kamusan_upgrader.py" --auto --restore-latest) || (python "%~dp0kamusan_upgrader.py" --auto --restore-latest)
echo.
pause
