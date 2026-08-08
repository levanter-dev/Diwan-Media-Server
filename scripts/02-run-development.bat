@echo off
setlocal
cd /d "%~dp0.."

set "INSTALL_EXE=%LOCALAPPDATA%\Programs\LocalMediaServer\LocalMediaServer.exe"
set "TASK_NAME=Local Media Server"

echo Stopping the currently installed server...
schtasks /End /TN "%TASK_NAME%" >nul 2>nul
if exist "%INSTALL_EXE%" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$target=[IO.Path]::GetFullPath('%INSTALL_EXE%');Get-Process LocalMediaServer -ErrorAction SilentlyContinue|Where-Object{$_.Path -eq $target}|Stop-Process -Force"
  timeout /t 2 /nobreak >nul
)

echo [2/5] Starting native media server and Node portal...

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Run scripts\01-setup.bat first.
  exit /b 1
)
where node >nul 2>nul || (echo ERROR: Node.js is not available. & exit /b 1)

call ".venv\Scripts\python.exe" scripts\dev.py
set "EXIT_CODE=%ERRORLEVEL%"
endlocal
exit /b %EXIT_CODE%
