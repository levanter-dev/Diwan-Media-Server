# Native Windows workflow

Run these files in order:

1. 01-setup.bat  -  checks Python and Node, creates the Python environment, and installs runtime dependencies.
2. 02-run-development.bat  -  runs the native API on port 8081 and the Node portal on port 8080.
3. 03-build-exe.bat  -  installs build dependencies and creates dist\LocalMediaServer\LocalMediaServer.exe.
4. 04-install-local.bat  -  installs the executable under Local AppData, registers startup at logon through Task Scheduler or the per-user Startup folder, and creates a Desktop portal shortcut.
5. 05-run-installed.bat  -  starts the installed server and opens the portal.

Optional:

- 90-uninstall-local.bat removes the installed executable, startup task, and shortcut while preserving media and application data.

The build step requires vendor\ffmpeg\bin\ffmpeg.exe and ffprobe.exe.

During development, press Ctrl+C to stop both child processes.


