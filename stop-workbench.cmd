@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo [!] .venv not found.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" launcher.py --stop
