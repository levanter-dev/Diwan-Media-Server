from __future__ import annotations

import logging
import re
import time
from urllib.parse import urljoin

from .browser import BrowserPool

logger = logging.getLogger(__name__)


def resolve_embed_to_stream(embed_url: str, max_wait: int = 15) -> str | None:
    """Load an embed URL in Chromium, interact with frames/players,
    and capture a direct video/m3u8 stream URL. Returns None if unsuccessful."""
    pool = BrowserPool.get()
    context, page = pool._new_page(timeout=30000)

    captured: list[str] = []
    popup_urls: list[str] = []

    def _on_response(response):
        url = response.url
        ct = response.headers.get("content-type", "")
        if any(url.endswith(e) for e in (".mp4", ".m3u8", ".ts")) or \
           "video/mp4" in ct or "mpegurl" in ct or "application/vnd.apple.mpegurl" in ct:
            if url not in captured:
                captured.append(url)
                logger.info("Embed resolver: captured stream %s", url[:120])

    context.on("response", _on_response)

    def _handle_popup(popup):
        popup_urls.append(popup.url)
        logger.info("Embed resolver: popup detected, closing: %s", popup.url[:100])
        try:
            popup.close()
        except Exception:
            pass

    page.on("popup", _handle_popup)

    def _block_ads(route):
        url_lower = route.request.url.lower()
        if any(d in url_lower for d in ("adexchange", "popunder", "notification", "/ads/", "gambling", "bonus-stars", "ad.")):
            route.abort()
        else:
            route.continue_()

    try:
        page.route("**/*", _block_ads)
    except Exception:
        pass

    try:
        page.goto(embed_url, wait_until="domcontentloaded", timeout=15000)
        logger.info("Embed resolver: page loaded")

        title = page.title()
        if "cloudflare" in title.lower() or "attention required" in title.lower():
            logger.info("Embed resolver: Cloudflare block detected, skipping")
            page.close()
            context.close()
            return None

        time.sleep(3)

        if captured:
            return captured[0]

        _click_everywhere(page)

        time.sleep(2)

        if captured:
            return captured[0]

        _click_video_player(page)

        time.sleep(4)

        if captured:
            logger.info("Embed resolver: returning captured stream after video click")
            return captured[0]

        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                frame_url = frame.url
                if any(frame_url.endswith(e) for e in (".mp4", ".m3u8", ".ts")):
                    return frame_url
            except Exception:
                continue

        if captured:
            return captured[0]

        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                fhtml = frame.content()
                for pat in [r'(?:src|href)\s*=\s*["\']([^"\']*\.mp4[^"\']*)["\']',
                             r'(?:src|href)\s*=\s*["\']([^"\']*\.m3u8[^"\']*)["\']']:
                    for m in re.finditer(pat, fhtml, re.IGNORECASE):
                        url = m.group(1).strip()
                        if url.startswith("http"):
                            return url
                        full = urljoin(frame.url, url)
                        if full.startswith("http"):
                            return full
            except Exception:
                continue

        page_html = page.content()
        for pat in [r'(?:src|href)\s*=\s*["\']([^"\']*\.mp4[^"\']*)["\']',
                     r'(?:src|href)\s*=\s*["\']([^"\']*\.m3u8[^"\']*)["\']']:
            for m in re.finditer(pat, page_html, re.IGNORECASE):
                url = m.group(1).strip()
                if url.startswith("http"):
                    return url

        if captured:
            return captured[0]

    except Exception as exc:
        logger.warning("Embed resolver error: %s", exc)
    finally:
        try:
            page.close()
        except Exception:
            pass
        try:
            context.close()
        except Exception:
            pass

    return None


def _click_everywhere(page) -> None:
    selectors = [
        "#player_iframe", "iframe", "#player", ".player",
        "video", "#iframe", ".embed-responsive",
        "button[aria-label*='play' i]", ".vjs-big-play-button",
        ".play-button", ".plyr__control--overlaid",
        ".jw-icon-playback", "button:has-text('Play')",
        "a:has-text('Play')", "#play-button", ".btn-play",
        ".jwplayer", "#myVideo", "#video-player",
        "div[onclick]", "div[role='button']",
    ]
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click(timeout=2000)
                logger.info("Embed resolver: clicked %s", sel)
                time.sleep(1)
                return
        except Exception:
            continue

    # Click inside child iframes
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            frame.click("body", timeout=2000)
            logger.info("Embed resolver: clicked frame body")
            time.sleep(1)
            return
        except Exception:
            pass
        try:
            frame.click("video", timeout=1500)
            logger.info("Embed resolver: clicked frame video")
            time.sleep(1)
            return
        except Exception:
            pass

    try:
        page.mouse.click(960, 540)
        logger.info("Embed resolver: clicked center of page")
        time.sleep(1)
    except Exception:
        pass


def _click_video_player(page) -> None:
    play_selectors = [
        "video",
        "button[aria-label*='play' i]",
        ".vjs-big-play-button",
        ".plyr__control--overlaid",
        ".jw-icon-playback",
        "button:has-text('Play')",
        "a:has-text('Play')",
        ".play-button",
        "#play-button",
        ".btn-play",
        ".jwplayer",
        "#myVideo",
        "#video-player",
        "div[role='button']",
    ]
    for sel in play_selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click(timeout=3000)
                logger.info("Embed resolver: clicked video play via %s", sel)
                time.sleep(1)
                return
        except Exception:
            continue

    for frame in page.frames:
        if frame == page.main_frame:
            continue
        for sel in play_selectors:
            try:
                el = frame.query_selector(sel)
                if el and el.is_visible():
                    el.click(timeout=3000)
                    logger.info("Embed resolver: clicked video play in frame via %s", sel)
                    time.sleep(1)
                    return
            except Exception:
                continue
        try:
            frame.click("body", timeout=2000)
            logger.info("Embed resolver: clicked frame body")
            time.sleep(1)
            return
        except Exception:
            pass
        try:
            frame.click("video", timeout=2000)
            logger.info("Embed resolver: clicked frame video")
            time.sleep(1)
            return
        except Exception:
            pass

    try:
        page.mouse.click(960, 540)
        logger.info("Embed resolver: clicked center (fallback)")
        time.sleep(1)
    except Exception:
        pass


def _click_frame_content(frame) -> None:
    try:
        frame.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    try:
        frame.click("body", timeout=3000)
        logger.info("Embed resolver: clicked frame body")
        time.sleep(1)
    except Exception:
        pass
    try:
        frame.click("video", timeout=2000)
        logger.info("Embed resolver: clicked frame video")
    except Exception:
        pass

