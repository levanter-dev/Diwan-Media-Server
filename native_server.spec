from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

project = Path(SPECPATH)
ffmpeg = project / "vendor" / "ffmpeg" / "bin"
binaries = []
for name in ("ffmpeg.exe", "ffprobe.exe"):
    candidate = ffmpeg / name
    if candidate.exists():
        binaries.append((str(candidate), "vendor/ffmpeg/bin"))

a = Analysis(
    ["native_server.py"],
    pathex=[str(project)],
    binaries=binaries,
    datas=[(str(project / "web"), "web")] + collect_data_files("nudenet") + collect_data_files("open_clip"),
    hiddenimports=["uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto"] + collect_submodules("nudenet") + collect_submodules("open_clip"),
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LocalMediaServer",
    console=False,
)
collect = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    name="LocalMediaServer",
)



