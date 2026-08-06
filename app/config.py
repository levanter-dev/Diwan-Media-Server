import os
import string
import sys
from pathlib import Path

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
if _ENV_FILE.exists():
    with open(_ENV_FILE, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _, _val = _line.partition("=")
                _key = _key.strip()
                _val = _val.strip().strip('"').strip("'")
                if _key and _key not in os.environ:
                    os.environ[_key] = _val

NATIVE_MODE = os.getenv("LOCAL_MEDIA_NATIVE") == "1" or (os.name == "nt" and not os.getenv("MEDIA_ROOTS"))
default_data = Path(os.getenv("LOCALAPPDATA", Path.home())) / "LocalMediaServer" if NATIVE_MODE else Path("./data")
DATA_DIR = Path(os.getenv("DATA_DIR", str(default_data)))
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", str(DATA_DIR / "media-server.db")))

def windows_drives() -> list[Path]:
    if os.name != "nt":
        return []
    return [Path(f"{letter}:\\") for letter in string.ascii_uppercase if Path(f"{letter}:\\").exists()]

configured_roots = os.getenv("MEDIA_ROOTS")
MEDIA_ROOTS = (
    [Path(part.strip()) for part in configured_roots.split(",") if part.strip()]
    if configured_roots else (windows_drives() if NATIVE_MODE else [Path("./media")])
)

bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
bundled_ffmpeg = bundle_root / "vendor" / "ffmpeg" / "bin" / "ffmpeg.exe"
bundled_ffprobe = bundle_root / "vendor" / "ffmpeg" / "bin" / "ffprobe.exe"
FFMPEG_PATH = os.getenv("FFMPEG_PATH", str(bundled_ffmpeg) if NATIVE_MODE else "ffmpeg")
FFPROBE_PATH = os.getenv("FFPROBE_PATH", str(bundled_ffprobe) if NATIVE_MODE else "ffprobe")
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav"}
SUPPORTED_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS

