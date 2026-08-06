from __future__ import annotations

import gzip
import io
import json
import zipfile
import logging
import os
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..db import connect


def _decode_subtitle_bytes(data: bytes) -> str:
    if not data:
        return ""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for name in z.namelist():
                if name.lower().endswith((".srt", ".vtt", ".ass", ".ssa", ".sub", ".txt")):
                    return _decode_subtitle_bytes(z.read(name))
    except zipfile.BadZipFile:
        pass
    except Exception:
        pass
    if data[:2] == b"\x1f\x8b":
        try:
            return _decode_subtitle_bytes(gzip.decompress(data))
        except Exception:
            pass
    for enc in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "cp1256", "windows-1252", "iso-8859-1"):
        try:
            text = data.decode(enc)
            if "-->" in text or "Dialogue:" in text or "WEBVTT" in text or len(text.strip()) > 20:
                return text.replace("\ufeff", "")
        except UnicodeError:
            continue
    return data.decode("utf-8", errors="replace").replace("\ufeff", "")

logger = logging.getLogger(__name__)

_API_BASE = "https://api.opensubtitles.com/api/v1"
_DEFAULT_API_KEY = ""
_token: str | None = None
_token_expiry: float = 0
_base_url: str = _API_BASE
_last_error: str = ""


def _setting(name: str, env_name: str = "") -> str:
    env_value = os.environ.get(env_name or name.upper(), "").strip()
    if env_value:
        return env_value
    try:
        conn = connect()
        row = conn.execute("SELECT value FROM settings WHERE key=?", (name,)).fetchone()
        conn.close()
        return row["value"].strip() if row else ""
    except Exception:
        return ""


def set_settings(username: str | None = None, password: str | None = None, api_key: str | None = None) -> dict:
    global _token, _token_expiry
    conn = connect()
    for key, value in (("opensubtitles_username", username), ("opensubtitles_password", password), ("opensubtitles_api_key", api_key)):
        if value is None:
            continue
        clean = value.strip()
        if clean:
            conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, clean))
        else:
            conn.execute("DELETE FROM settings WHERE key=?", (key,))
    conn.commit()
    conn.close()
    _token = None
    _token_expiry = 0
    return status()


def status() -> dict:
    return {
        "username_configured": bool(_setting("opensubtitles_username", "OPENSUBTITLES_USERNAME")),
        "password_configured": bool(_setting("opensubtitles_password", "OPENSUBTITLES_PASSWORD")),
        "api_key_configured": bool(_setting("opensubtitles_api_key", "OPENSUBTITLES_API_KEY")),
    }


def _api_key() -> str:
    return _setting("opensubtitles_api_key", "OPENSUBTITLES_API_KEY") or _DEFAULT_API_KEY


def last_error() -> str:
    return _last_error


def _language(value: str) -> str:
    clean = (value or "en").lower().strip()
    return {"english": "en", "arabic": "ar", "deutsch": "de", "german": "de"}.get(clean, clean[:2] or "en")


def _login() -> str | None:
    global _token, _token_expiry, _base_url
    if _token and time.time() < _token_expiry - 60:
        return _token
    username = _setting("opensubtitles_username", "OPENSUBTITLES_USERNAME")
    password = _setting("opensubtitles_password", "OPENSUBTITLES_PASSWORD")
    if not username or not password:
        logger.warning("OpenSubtitles: credentials not configured")
        return None
    try:
        body = json.dumps({"username": username, "password": password}).encode()
        req = Request(f"{_API_BASE}/login", data=body, headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "LocalMediaServer/0.4",
            "Api-Key": _api_key(),
        })
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        _token = data.get("token")
        base = data.get("base_url")
        if base:
            _base_url = base if base.startswith("http") else f"https://{base}/api/v1"
        if _token:
            _token_expiry = time.time() + 24 * 3600
            logger.info("OpenSubtitles: logged in")
            return _token
    except Exception as exc:
        logger.warning("OpenSubtitles login failed: %s", exc)
    return None


def search_subtitles(query: str, language: str = "en") -> list[dict]:
    global _last_error
    _last_error = ""
    if not _api_key():
        _last_error = "OpenSubtitles API key is not configured"
        return []
    if not query.strip():
        return []
    try:
        params = urlencode({"query": query.strip(), "languages": _language(language), "order_by": "download_count", "page": "1"})
        req = Request(f"{_API_BASE}/subtitles?{params}", headers={
            "Accept": "application/json",
            "User-Agent": "LocalMediaServer/0.4",
            "Api-Key": _api_key(),
        })
        with urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read())
        results = []
        for item in data.get("data", [])[:20]:
            attrs = item.get("attributes", {})
            files = attrs.get("files") or []
            results.append({
                "id": item.get("id"),
                "title": attrs.get("release") or attrs.get("feature_details", {}).get("title") or "Subtitle",
                "language": attrs.get("language", ""),
                "download_count": attrs.get("download_count", 0),
                "file_id": files[0].get("file_id") if files else None,
                "source": "opensubtitles",
            })
        logger.info("OpenSubtitles search: %d results for '%s'", len(results), query)
        return results
    except Exception as exc:
        _last_error = str(exc)
        logger.warning("OpenSubtitles search failed: %s", exc)
        return []


def get_subtitle_download_url(file_id: int) -> str | None:
    token = _login()
    if not token:
        return None
    try:
        body = json.dumps({"file_id": int(file_id), "sub_format": "srt"}).encode()
        req = Request(f"{_base_url}/download", data=body, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "LocalMediaServer/0.4",
            "Api-Key": _api_key(),
        })
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return data.get("link")
    except Exception as exc:
        logger.warning("OpenSubtitles download failed: %s", exc)
        return None


def download_and_extract_srt(download_url: str) -> str | None:
    try:
        req = Request(download_url, headers={"User-Agent": "LocalMediaServer/0.4"})
        with urlopen(req, timeout=20) as resp:
            return _decode_subtitle_bytes(resp.read())
    except Exception as exc:
        logger.warning("OpenSubtitles srt download failed: %s", exc)
        return None
