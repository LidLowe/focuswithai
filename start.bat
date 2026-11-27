@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 exit /b 1
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 exit /b 1
)
".venv\Scripts\python.exe" launcher.py %*
exit /b %errorlevel%
