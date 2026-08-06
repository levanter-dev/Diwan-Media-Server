@echo off
setlocal
echo Removing the local installation...
schtasks /End /TN "Local Media Server" >nul 2>nul
schtasks /Delete /TN "Local Media Server" /F >nul 2>nul
if exist "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Local Media Server.lnk" del /F "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Local Media Server.lnk"
if exist "%LOCALAPPDATA%\Programs\LocalMediaServer" rmdir /S /Q "%LOCALAPPDATA%\Programs\LocalMediaServer"
if exist "%USERPROFILE%\Desktop\Local Media Server.url" del /F "%USERPROFILE%\Desktop\Local Media Server.url"
echo Program files and startup task removed.
echo Media and application data under %LOCALAPPDATA%\LocalMediaServer were preserved.
endlocal


