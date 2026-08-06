@echo off
setlocal
cd /d "%~dp0.."
echo [2/5] Starting native media server and Node portal...

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Run scripts\01-setup.bat first.
  exit /b 1
)
where node >nul 2>nul || (echo ERROR: Node.js is not available. & exit /b 1)

call ".venv\Scripts\python.exe" scripts\dev.py
endlocal

