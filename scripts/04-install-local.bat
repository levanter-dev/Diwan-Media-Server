@echo off
setlocal
cd /d "%~dp0.."
echo [4/5] Installing Local Media Server for this Windows user...

set "BUILD_DIR=%CD%\dist\LocalMediaServer"
set "INSTALL_DIR=%LOCALAPPDATA%\Programs\LocalMediaServer"
set "INSTALL_EXE=%INSTALL_DIR%\LocalMediaServer.exe"
set "TASK_NAME=Local Media Server"
set "STARTUP_LINK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Local Media Server.lnk"

if not exist "%BUILD_DIR%\LocalMediaServer.exe" (
  echo ERROR: Run scripts\03-build-exe.bat first.
  exit /b 1
)

echo Stopping the currently installed server...
schtasks /End /TN "%TASK_NAME%" >nul 2>nul
powershell.exe -NoProfile -Command "$target=[IO.Path]::GetFullPath('%INSTALL_EXE%');Get-Process LocalMediaServer -ErrorAction SilentlyContinue|Where-Object{$_.Path -eq $target}|Stop-Process -Force"
powershell.exe -NoProfile -Command "Start-Sleep -Seconds 2"

if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
robocopy "%BUILD_DIR%" "%INSTALL_DIR%" /MIR /COPY:DAT /DCOPY:DAT /R:3 /W:1 /NFL /NDL /NJH /NJS
if errorlevel 8 (
  echo ERROR: Could not update the application at %INSTALL_DIR%.
  exit /b 1
)

schtasks /Create /TN "%TASK_NAME%" /SC ONLOGON /TR "\"%INSTALL_EXE%\"" /RL LIMITED /F >nul 2>nul
if errorlevel 1 (
  powershell.exe -NoProfile -Command "$s=(New-Object -COM WScript.Shell).CreateShortcut('%STARTUP_LINK%');$s.TargetPath='%INSTALL_EXE%';$s.WorkingDirectory='%INSTALL_DIR%';$s.Save()"
  if errorlevel 1 (
    echo ERROR: Could not register startup.
    exit /b 1
  )
  echo Startup registered through your Windows Startup folder.
  start "Local Media Server" "%INSTALL_EXE%"
) else (
  if exist "%STARTUP_LINK%" del /F "%STARTUP_LINK%"
  echo Startup registered through Windows Task Scheduler.
  schtasks /Run /TN "%TASK_NAME%" >nul
)

>"%USERPROFILE%\Desktop\Local Media Server.url" echo [InternetShortcut]
>>"%USERPROFILE%\Desktop\Local Media Server.url" echo URL=http://localhost:8080

echo Installed at %INSTALL_EXE%
echo Data is stored at %LOCALAPPDATA%\LocalMediaServer
echo A portal shortcut was added to your Desktop.
echo Next: scripts\05-run-installed.bat
endlocal
exit /b 0

