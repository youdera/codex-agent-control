@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -I -X utf8 open_panel.py
) else (
  python -I -X utf8 open_panel.py
)
