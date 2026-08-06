"""Entry point for the native Windows media-server executable."""

import logging
import os
import sys
from pathlib import Path

os.environ.setdefault("LOCAL_MEDIA_NATIVE", "1")
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

_data_dir = Path(os.getenv("LOCALAPPDATA", Path.home())) / "LocalMediaServer" if os.getenv("LOCAL_MEDIA_NATIVE") == "1" else Path("./data")
_data_dir.mkdir(parents=True, exist_ok=True)

handlers: list[logging.Handler] = [
    logging.StreamHandler(sys.stderr),
]

_trace_file = _data_dir / "scraper-traces.log"
try:
    fh = logging.FileHandler(str(_trace_file), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    handlers.append(fh)
except OSError:
    pass

logging.basicConfig(
    level=logging.DEBUG if os.getenv("MEDIA_DEBUG_LOGS") else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=handlers,
)

import uvicorn
from app.main import app


def main() -> None:
    host = os.getenv("MEDIA_API_BIND", "0.0.0.0")
    port = int(os.getenv("MEDIA_API_PORT", "8080"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()

