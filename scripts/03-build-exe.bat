@echo off
setlocal
cd /d "%~dp0.."
echo [3/5] Building LocalMediaServer.exe...

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Run scripts\01-setup.bat first.
  exit /b 1
)
if not exist "vendor\ffmpeg\bin\ffmpeg.exe" (
  echo ERROR: vendor\ffmpeg\bin\ffmpeg.exe is missing.
  exit /b 1
)
if not exist "vendor\ffmpeg\bin\ffprobe.exe" (
  echo ERROR: vendor\ffmpeg\bin\ffprobe.exe is missing.
  exit /b 1
)

call ".venv\Scripts\python.exe" -m pip install -r requirements-build.txt
if errorlevel 1 exit /b 1
call ".venv\Scripts\pyinstaller.exe" --noconfirm --clean native_server.spec
if errorlevel 1 exit /b 1

echo Build complete: dist\LocalMediaServer\LocalMediaServer.exe
echo Next: scripts\04-install-local.bat
endlocal


