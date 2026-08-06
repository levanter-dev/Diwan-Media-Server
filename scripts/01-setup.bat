@echo off
setlocal
cd /d "%~dp0.."
echo [1/5] Setting up native development environment...

where python >nul 2>nul || (echo ERROR: Python 3.12 or newer is required. & exit /b 1)
where node >nul 2>nul || (echo ERROR: Node.js 20 or newer is required. & exit /b 1)

if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 exit /b 1

call ".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1

if not exist "vendor\ffmpeg\bin" mkdir "vendor\ffmpeg\bin"
echo.
echo Setup complete.
echo Next: scripts\02-run-development.bat
endlocal

