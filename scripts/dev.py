"""Run the native API and Node portal together during development."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val


def main() -> int:
    _load_dotenv()
    api_env = os.environ.copy()
    api_env.update({"LOCAL_MEDIA_NATIVE": "1", "MEDIA_API_BIND": "127.0.0.1", "MEDIA_API_PORT": "8081"})
    portal_env = os.environ.copy()
    portal_env.update({"PORT": "8080", "MEDIA_API_HOST": "127.0.0.1", "MEDIA_API_PORT": "8081"})
    if os.environ.get("DOMAIN"):
        portal_env["DOMAIN"] = os.environ["DOMAIN"]

    api = subprocess.Popen([sys.executable, "native_server.py"], cwd=ROOT, env=api_env)
    portal = None
    try:
        time.sleep(1)
        if api.poll() is not None:
            return api.returncode or 1
        portal = subprocess.Popen(["node", "web/server.mjs"], cwd=ROOT, env=portal_env)
        print("Open http://localhost:8080")
        return portal.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        for process in (portal, api):
            if process is not None and process.poll() is None:
                process.terminate()
        for process in (portal, api):
            if process is not None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
