from __future__ import annotations

import io
import logging
import re
import zipfile
from urllib.parse import quote_plus, urljoin
from urllib.request import Request, urlopen

from .browser import BrowserPool, BrowserError


def _decode_subtitle_bytes(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "cp1256", "windows-1252", "iso-8859-1"):
        try:
            text = data.decode(enc)
            if "-->" in text or "Dialogue:" in text or len(text.strip()) > 20:
                return text.replace("\ufeff", "")
        except UnicodeError:
            continue
    return data.decode("utf-8", errors="replace").replace("\ufeff", "")
logger = logging.getLogger(__name__)

SUBSCENE_BASE = "https://subscene.com"


def search_subtitles(query: str, language: str = "english") -> list[dict]:
    logger.info("Subscene search: %s (lang=%s)", query, language)
    try:
        html = BrowserPool.fetch_html(
            f"{SUBSCENE_BASE}/subtitles/searchbytitle?query={quote_plus(query)}&l=",
            timeout=12,
        )
    except BrowserError:
        return []
    results: list[dict] = []
    item_pattern = re.compile(
        r'<a\s+href="(/subtitles/[^"]+)"[^>]*>.*?<span[^>]*>([^<]+)</span>\s*<span[^>]*>([^<]*)</span>',
        re.DOTALL | re.IGNORECASE,
    )
    for m in item_pattern.finditer(html):
        href = urljoin(SUBSCENE_BASE, m.group(1))
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        info = re.sub(r"<[^>]+>", "", m.group(3)).strip() if m.group(3) else ""
        results.append({"title": title, "info": info, "url": href, "language": language})
    logger.info("Subscene found %d results", len(results))
    return results[:15]


def get_subtitle_download_url(subtitle_page_url: str) -> str | None:
    logger.info("Subscene page: %s", subtitle_page_url)
    try:
        html = BrowserPool.fetch_html(subtitle_page_url, timeout=12)
    except BrowserError:
        return None
    dl_pattern = re.compile(r'href="(/subtitles/[^"]+/download/[^"]+)"', re.IGNORECASE)
    m = dl_pattern.search(html)
    if m:
        return urljoin(SUBSCENE_BASE, m.group(1))
    return None


def download_and_extract_srt(download_url: str) -> str | None:
    logger.info("Subscene download: %s", download_url)
    try:
        req = Request(download_url, headers={"User-Agent": "LocalMediaServer/0.4", "Referer": SUBSCENE_BASE})
        with urlopen(req, timeout=20) as resp:
            data = resp.read()
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for name in z.namelist():
                if name.lower().endswith(".srt"):
                    return _decode_subtitle_bytes(z.read(name))
        return None
    except Exception as exc:
        logger.warning("Subscene download failed: %s", exc)
        return None
