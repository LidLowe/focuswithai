@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
".venv\Scripts\python.exe" "launcher.py" %*
set "APP_EXIT=%errorlevel%"
if not "%APP_EXIT%"=="0" if "%~1"=="" pause
exit /b %APP_EXIT%
