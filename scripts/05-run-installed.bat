@echo off
setlocal
echo [5/5] Starting installed Local Media Server...
set "INSTALL_EXE=%LOCALAPPDATA%\Programs\LocalMediaServer\LocalMediaServer.exe"

schtasks /Run /TN "Local Media Server" >nul 2>nul
if not errorlevel 1 goto opened

if not exist "%INSTALL_EXE%" goto not_installed
start "Local Media Server" "%INSTALL_EXE%"

:opened
timeout /t 2 /nobreak >nul
start "" http://localhost:8080
endlocal
exit /b 0

:not_installed
echo ERROR: Run scripts\04-install-local.bat first.
endlocal
exit /b 1

