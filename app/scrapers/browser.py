from __future__ import annotations

import logging
import os
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from playwright.sync_api import sync_playwright

from .models import ServerSource

logger = logging.getLogger(__name__)

_MAX_BROWSERS = int(os.environ.get("SCRAPE_MAX_BROWSERS", "7"))
_browser_semaphore = threading.BoundedSemaphore(_MAX_BROWSERS)


class BrowserError(RuntimeError):
    pass


class BrowserPool:
    _local = threading.local()
    _access_lock = threading.Lock()

    def __init__(self) -> None:
        self._browser = None
        self._playwright = None
        self._initialized = False
        self._semaphore_acquired = False

    @classmethod
    def get(cls) -> BrowserPool:
        if not hasattr(cls._local, "instance"):
            cls._local.instance = cls()
        return cls._local.instance

    def _ensure_browser(self) -> None:
        if self._initialized:
            return
        with BrowserPool._access_lock:
            if self._initialized:
                return
            _browser_semaphore.acquire()
            self._semaphore_acquired = True
            logger.info("Launching headless Chromium browser ...")
            try:
                pw = sync_playwright().start()
                browser = pw.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
                self._playwright = pw
                self._browser = browser
                self._initialized = True
                logger.info("Chromium browser launched successfully")
            except ImportError:
                if self._semaphore_acquired:
                    _browser_semaphore.release()
                    self._semaphore_acquired = False
                raise BrowserError(
                    "Playwright is not installed. Run: pip install playwright && playwright install chromium"
                )
            except BrowserError:
                if self._semaphore_acquired:
                    _browser_semaphore.release()
                    self._semaphore_acquired = False
                raise
            except Exception as exc:
                if self._semaphore_acquired:
                    _browser_semaphore.release()
                    self._semaphore_acquired = False
                self._initialized = False
                raise BrowserError(f"Failed to launch browser: {exc}")

    def _new_page(self, timeout: int = 30000):
        self._ensure_browser()
        if not self._browser:
            raise BrowserError("Browser not initialized — previous launch may have failed")
        context = self._browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            java_script_enabled=True,
            bypass_csp=True,
        )
        context.set_default_timeout(timeout)
        page = context.new_page()
        return context, page

    def extract_video_urls(self, page_url: str, timeout: int = 30000) -> list[ServerSource]:
        logger.info("Extracting video URLs from: %s", page_url)
        servers, html = self._extract_from_page(page_url, timeout)
        if servers:
            logger.info("Found %d server sources via browser extraction", len(servers))
            return servers
        found_in_html = _extract_mp4_from_text(html)
        if found_in_html:
            logger.info("Found %d MP4 URLs via HTML regex scan", len(found_in_html))
        return _build_servers(found_in_html, page_url)

    def extract_from_iframe_urls(
        self, iframe_urls: list[str], timeout: int = 20000
    ) -> list[ServerSource]:
        results: list[ServerSource] = []
        for i, iframe_url in enumerate(iframe_urls):
            logger.info("Inspecting iframe %d/%d: %s", i + 1, len(iframe_urls), iframe_url[:120])
            try:
                page_result, _html = self._extract_from_page(iframe_url, timeout)
                if page_result:
                    for s in page_result:
                        s.server_name = _domain_label(iframe_url) + " - " + s.server_name
                    results.extend(page_result)
                    continue
                mp4_in_html = _extract_mp4_from_text(_html)
                if mp4_in_html:
                    results.extend(
                        [
                            ServerSource(
                                server_id=f"mp4_{len(results)}",
                                server_name=f"{_domain_label(iframe_url)} - Source {i + 1}",
                                video_url=url,
                                quality=_guess_quality(url),
                                direct=True,
                            )
                            for j, url in enumerate(mp4_in_html)
                        ]
                    )
            except BrowserError as exc:
                logger.warning("Iframe %d/%d failed: %s", i + 1, len(iframe_urls), exc)
                continue
        return results

    def _extract_from_page(
        self, page_url: str, timeout: int = 30000
    ) -> tuple[list[ServerSource], str]:
        self._ensure_browser()
        mp4_urls: list[tuple[str, str | None]] = []
        nav_timeout = min(timeout, 15000)
        context, page = self._new_page(timeout)
        logger.info("Chromium navigating to: %s", page_url)

        def _on_response(response):
            url = response.url
            content_type = response.headers.get("content-type", "")
            if url.endswith(".mp4") or "video/mp4" in content_type:
                quality = _guess_quality(url)
                logger.info("Network detected MP4: %s  (quality=%s)", url[:120], quality or "unknown")
                if (url, quality) not in mp4_urls:
                    mp4_urls.append((url, quality))

        context.on("response", _on_response)
        page_html = ""

        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=nav_timeout)
            time.sleep(2)

            _try_click_play(page)
            _try_dismiss_overlays(page)

            try:
                page.wait_for_selector(
                    "video, iframe[src*='vid'], iframe[src*='embed'], iframe[src*='stream'], iframe",
                    timeout=8000,
                )
            except Exception:
                logger.info("No video/iframe selectors appeared within 8s — continuing")
            time.sleep(3)

            page_html = page.content()
            found_in_html = _extract_mp4_from_text(page_html)
            for url_match in found_in_html:
                full_url = urljoin(page_url, url_match)
                quality = _guess_quality(full_url)
                if (full_url, quality) not in mp4_urls:
                    mp4_urls.append((full_url, quality))
            if found_in_html:
                logger.info("HTML regex found %d MP4 URLs", len(found_in_html))

            if not mp4_urls:
                mp4_urls = _extract_mp4_from_iframes(page, page_url)
        except Exception as exc:
            logger.warning("Page load failed for %s: %s — skipping", page_url, exc)
            page_html = ""
        finally:
            try:
                page.close()
            except Exception:
                pass
            try:
                context.close()
            except Exception:
                pass

        logger.info("Total MP4 sources found for %s: %d", page_url, len(mp4_urls))
        return _build_servers_from_tuples(mp4_urls), page_html

    @staticmethod
    def fetch_html(url: str, timeout: int = 10) -> str:
        request = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            },
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError) as exc:
            raise BrowserError(f"Failed to fetch {url}: {exc}")

    def fetch_rendered_html(self, url: str, timeout: int = 30000) -> str:
        """Fetch fully JS-rendered HTML using headless Chromium."""
        self._ensure_browser()
        context, page = self._new_page(timeout)
        logger.info("Chromium fetching rendered HTML from: %s", url)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=min(timeout, 20000))
            for _ in range(10):
                time.sleep(2)
                title = page.title()
                if "just a moment" not in title.lower() and "cloudflare" not in title.lower():
                    break
                html = page.content()
                if len(html) > 5000 and "cloudflare" not in html.lower():
                    break
            html = page.content()
            logger.info("Chromium rendered page: %d chars of HTML", len(html))
            return html
        except Exception as exc:
            logger.error("Chromium fetch failed for %s: %s", url, exc)
            raise BrowserError(f"Chromium failed to load {url}: {exc}")
        finally:
            try:
                page.close()
            except Exception:
                pass
            try:
                context.close()
            except Exception:
                pass

    def extract_download_button_url(
        self,
        page_url: str,
        button_selector: str = '[data-link-id="0"]',
        wait_seconds: int = 15,
        timeout: int = 30000,
    ) -> str | None:
        self._ensure_browser()
        context, page = self._new_page(timeout)
        logger.info("Download extraction: navigating to %s", page_url)

        captured_urls: list[str] = []

        def _on_response(response):
            url = response.url
            content_type = response.headers.get("content-type", "")
            if url.endswith(".mp4") or "video/mp4" in content_type or url.endswith(".m3u8") or "mpegurl" in content_type:
                logger.info("Download page: detected stream %s", url[:120])
                captured_urls.append(url)

        context.on("response", _on_response)

        button_selectors = [button_selector]
        if button_selector == '[data-link-id="0"]':
            button_selectors = [
                '[data-link-id="0"]',
                '[data-link-id="1"]',
                '.download-btn',
                'a.download-link',
                'button:has-text("Download")',
                'a:has-text("Download")',
            ]

        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=timeout)
            time.sleep(3)

            if captured_urls:
                logger.info("Download page: network captured %d URLs before button click", len(captured_urls))
                return captured_urls[0]

            button = None
            for sel in button_selectors:
                button = page.query_selector(sel)
                if button:
                    logger.info("Found download button: %s", sel)
                    break

            if not button:
                logger.warning("No download button found on page, checking page HTML for links")
                html = page.content()
                mp4_urls = _extract_mp4_from_text(html)
                for u in mp4_urls:
                    full = urljoin(page_url, u)
                    if full not in captured_urls:
                        captured_urls.append(full)
                if captured_urls:
                    logger.info("Found %d MP4 URLs in download page HTML", len(captured_urls))
                    return captured_urls[0]
                return None

            logger.info("Clicking download button: %s", button_selector)
            button.click(timeout=5000)
            logger.info("Waiting %ds for download link to generate ...", wait_seconds)

            elapsed = 0
            poll_interval = 0.5
            href = None
            while elapsed < wait_seconds:
                time.sleep(poll_interval)
                elapsed += poll_interval
                try:
                    current_button = page.query_selector(button_selector)
                    if current_button:
                        href = current_button.get_attribute("href")
                        if href and href.strip():
                            break
                except Exception:
                    continue
                if captured_urls:
                    break

            if captured_urls:
                logger.info("Download page: network captured URL: %s", captured_urls[0][:150])
                return captured_urls[0]

            if href and href.strip():
                logger.info("Download link extracted: %s", href[:150])
                return href.strip()

            logger.warning("No download link obtained after %ds wait", wait_seconds)
            return None
        except Exception as exc:
            logger.error("Download button extraction failed: %s", exc)
            return None
        finally:
            try:
                page.close()
            except Exception:
                pass
            try:
                context.close()
            except Exception:
                pass

    def close(self) -> None:
        logger.info("Shutting down browser pool")
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._browser = None
        self._playwright = None
        self._initialized = False
        if self._semaphore_acquired:
            _browser_semaphore.release()
            self._semaphore_acquired = False


def _try_click_play(page) -> None:
    play_selectors = [
        "button[aria-label*='play' i]",
        "button[title*='play' i]",
        "button.vjs-big-play-button",
        ".vjs-big-play-button",
        "button.play-btn",
        ".play-button",
        "div[role='button'][aria-label*='play' i]",
        "button:has-text('Play')",
        "a:has-text('Play')",
        ".jw-icon-playback",
        ".plyr__control--overlaid",
        "video",
    ]

    def _click_in_frame(frame):
        for selector in play_selectors:
            try:
                el = frame.query_selector(selector)
                if el:
                    el.click(timeout=2000)
                    logger.info("Clicked play button: %s (frame: %s)", selector, frame.url[:80])
                    time.sleep(1)
                    return True
            except Exception:
                continue
        return False

    if _click_in_frame(page):
        return

    for frame in page.frames:
        try:
            if frame == page.main_frame:
                continue
            if _click_in_frame(frame):
                return
        except Exception:
            continue

    logger.info("No play button found on page")


def _try_dismiss_overlays(page) -> None:
    dismiss_selectors = [
        "button.close",
        "a.close",
        ".modal button.close",
        "[aria-label='Close']",
        ".overlay .close",
        ".popup .close",
    ]
    for selector in dismiss_selectors:
        try:
            el = page.query_selector(selector)
            if el:
                el.click(timeout=2000)
                time.sleep(0.5)
        except Exception:
            continue


def _extract_mp4_from_iframes(page, page_url: str) -> list[tuple[str, str | None]]:
    results: list[tuple[str, str | None]] = []
    try:
        frames = page.frames
        for frame in frames:
            try:
                frame_url = frame.url
                if frame_url == page_url or frame_url == "about:blank":
                    continue
                html = frame.content()
                found = _extract_mp4_from_text(html)
                for url in found:
                    full = urljoin(frame_url, url)
                    if (full, _guess_quality(full)) not in results:
                        results.append((full, _guess_quality(full)))
            except Exception:
                continue
    except Exception:
        pass
    return results


def _extract_mp4_from_text(text: str) -> list[str]:
    results: list[str] = []
    patterns = [
        r'(?:src|href|source|data-url|data-src)\s*=\s*["\']([^"\']*\.mp4[^"\']*)["\']',
        r'["\'](https?://[^"\'\s,]+\.mp4[^"\'\s,]*)["\']',
        r'(https?://[^\s"\'<>,]+\.mp4[^\s"\'<>,]*)',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            url = match.group(1).strip()
            if url and url not in results:
                results.append(url)
    return results


def _guess_quality(url: str) -> str | None:
    mapping = {
        "2160p": "4K",
        "1440p": "2K",
        "1080p": "1080p",
        "720p": "720p",
        "480p": "480p",
        "360p": "360p",
    }
    for key, label in mapping.items():
        if key in url.lower():
            return label
    return None


_LANG_PATTERNS: list[tuple[str, str]] = [
    (r"[-_.](?:dub|dubbed)[-_.]", "Dubbed"),
    (r"[-_.]arabic[-_.]", "Arabic"),
    (r"[-_.](?:ar|ara)[-_.]", "Arabic"),
    (r"[-_.]english[-_.]", "English"),
    (r"[-_.](?:en|eng)[-_.]", "English"),
    (r"[-_.]french[-_.]", "French"),
    (r"[-_.](?:fr|fra|fre)[-_.]", "French"),
    (r"[-_.]spanish[-_.]", "Spanish"),
    (r"[-_.](?:es|spa)[-_.]", "Spanish"),
    (r"[-_.]hindi[-_.]", "Hindi"),
    (r"[-_.](?:hi|hin)[-_.]", "Hindi"),
    (r"[-_.]turkish[-_.]", "Turkish"),
    (r"[-_.](?:tr|tur)[-_.]", "Turkish"),
    (r"[-_.]german[-_.]", "German"),
    (r"[-_.](?:de|deu|ger)[-_.]", "German"),
    (r"[-_.]korean[-_.]", "Korean"),
    (r"[-_.](?:ko|kor)[-_.]", "Korean"),
    (r"[-_.]japanese[-_.]", "Japanese"),
    (r"[-_.](?:ja|jp|jpn)[-_.]", "Japanese"),
    (r"[-_.]tagalog[-_.]", "Tagalog"),
    (r"[-_.]tamil[-_.]", "Tamil"),
    (r"[-_.]telugu[-_.]", "Telugu"),
    (r"[-_.]malayalam[-_.]", "Malayalam"),
    (r"[-_.]indonesian[-_.]", "Indonesian"),
    (r"[-_.](?:id|ind)[-_.]", "Indonesian"),
    (r"[-_.]portuguese[-_.]", "Portuguese"),
    (r"[-_.](?:pt|por)[-_.]", "Portuguese"),
    (r"[-_.]italian[-_.]", "Italian"),
    (r"[-_.](?:it|ita)[-_.]", "Italian"),
    (r"[-_.]russian[-_.]", "Russian"),
    (r"[-_.](?:ru|rus)[-_.]", "Russian"),
    (r"[-_.]thai[-_.]", "Thai"),
    (r"[-_.](?:th|tha)[-_.]", "Thai"),
]


def _guess_language(url: str) -> str | None:
    """Detect dub/sub language from URL path or filename."""
    lower = url.lower()
    # filename extraction
    filename = lower.rsplit("/", 1)[-1] if "/" in lower else lower
    for pattern, label in _LANG_PATTERNS:
        if re.search(pattern, filename):
            return label
    # Look for language codes with quality notation: ...1080p.ar.mp4
    m = re.search(r"[-_.]([a-z]{2,3})(?:[-_.]|\.[a-z0-9]+$)", filename)
    if m:
        code = m.group(1)
        for pattern, label in _LANG_PATTERNS:
            if code in pattern:
                return label
    return None


_SUB_PATTERNS: list[tuple[str, str]] = [
    (r"[-_.]hc[-_.]", "Hardcoded"),
    (r"[-_.]hardsub", "Hardcoded"),
    (r"[-_.]hardcoded", "Hardcoded"),
    (r"[-_.](?:sub|subbed)[-_.]", "Soft subs"),
    (r"[-_.]softsub", "Soft subs"),
    (r"[-_.]cc[-_.]", "Soft subs"),
    (r"[-_.]nosub", "None"),
    (r"[-_.]raw[-_.]", "None"),
]


def _guess_subtitles(url: str) -> str | None:
    """Detect subtitle hints from URL."""
    filename = url.lower().rsplit("/", 1)[-1] if "/" in url.lower() else url.lower()
    for pattern, label in _SUB_PATTERNS:
        if re.search(pattern, filename):
            return label
    return None


def _build_servers_from_tuples(
    mp4_urls: list[tuple[str, str | None]],
) -> list[ServerSource]:
    servers: list[ServerSource] = []
    seen: set[str] = set()
    _ad = re.compile(r"notification|gambling|bonus-stars|/ads/|popunder|/sb/", re.IGNORECASE)
    for idx, (url, quality) in enumerate(mp4_urls):
        if url in seen:
            continue
        if _ad.search(url):
            continue
        seen.add(url)
        label_parts = []
        if quality:
            label_parts.append(quality)
        label_parts.append(f"Source {idx + 1}")
        servers.append(
            ServerSource(
                server_id=f"mp4_{idx}",
                server_name=" ".join(label_parts),
                video_url=url,
                quality=quality,
                direct=True,
                language=_guess_language(url),
                subtitles=_guess_subtitles(url),
            )
        )
    return servers


def _build_servers(urls: list[str], base_url: str) -> list[ServerSource]:
    servers: list[ServerSource] = []
    seen: set[str] = set()
    _ad = re.compile(r"notification|gambling|bonus-stars|/ads/|popunder|/sb/", re.IGNORECASE)
    for url in urls:
        full = urljoin(base_url, url)
        if full in seen:
            continue
        if _ad.search(full):
            continue
        seen.add(full)
        quality = _guess_quality(full)
        label = quality if quality else None
        servers.append(
            ServerSource(
                server_id=f"mp4_{len(servers)}",
                server_name=label or f"Source {len(servers) + 1}",
                video_url=full,
                quality=quality,
                direct=True,
                language=_guess_language(full),
                subtitles=_guess_subtitles(full),
            )
        )
    return servers


def _domain_label(url: str) -> str:
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        host = parsed.hostname or parsed.netloc or url
        return host.replace("www.", "")
    except Exception:
        return url[:40]
