@echo off
set PYTHONUTF8=1
where py >nul 2>nul && (start "" pyw -3 "%~dp0kamusan_upgrader.py") || (start "" pythonw "%~dp0kamusan_upgrader.py")
