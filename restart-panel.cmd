@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -I -X utf8 service_control.py restart
) else (
  python -I -X utf8 service_control.py restart
)
if errorlevel 1 (
  pause
  exit /b 1
)
