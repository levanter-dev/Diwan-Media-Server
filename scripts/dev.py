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
    api_env.update(
        {
            "LOCAL_MEDIA_NATIVE": "1",
            "MEDIA_API_BIND": "127.0.0.1",
            "MEDIA_API_PORT": "8081",
            "MEDIA_PUBLIC_PORT": "8080",
            "MEDIA_DISABLE_DOMAIN_ADVERTISEMENT": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    portal_env = os.environ.copy()
    portal_env.update({"PORT": "8080", "MEDIA_API_HOST": "127.0.0.1", "MEDIA_API_PORT": "8081"})
    if os.environ.get("DOMAIN"):
        portal_env["DOMAIN"] = os.environ["DOMAIN"]

    api = None
    portal = None
    try:
        api = subprocess.Popen([sys.executable, "-u", "native_server.py"], cwd=ROOT, env=api_env)
        time.sleep(1.5)
        if api.poll() is not None:
            print(f"ERROR: Native API exited with code {api.returncode}.", file=sys.stderr)
            return api.returncode or 1

        portal = subprocess.Popen(["node", "web/server.mjs"], cwd=ROOT, env=portal_env)
        print("Open http://localhost:8080", flush=True)
        if portal_env.get("DOMAIN"):
            print(f"Domain mode: {portal_env['DOMAIN']}", flush=True)

        while True:
            api_code = api.poll()
            portal_code = portal.poll()
            if api_code is not None:
                print(f"ERROR: Native API stopped with code {api_code}.", file=sys.stderr)
                return api_code or 1
            if portal_code is not None:
                print(f"Portal stopped with code {portal_code}.", flush=True)
                return portal_code
            time.sleep(0.5)
    except FileNotFoundError as exc:
        print(f"ERROR: Could not start development server: {exc}", file=sys.stderr)
        return 1
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
