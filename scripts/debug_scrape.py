"""Live debug script  -  searches for a movie across ALL sources in parallel, then tries to extract video streams.

Usage from project root:
    py scripts/debug_scrape.py "The Invention of Lying"
    py scripts/debug_scrape.py "The Invention of Lying" --extract
    py scripts/debug_scrape.py "The Invention of Lying" --extract --dump
"""

from __future__ import annotations

import logging
import os
import sys
import time
import json
import threading
import queue
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


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


_load_dotenv()
os.environ.setdefault("EXPERIMENTAL", "1")
os.environ.setdefault("MEDIA_DEBUG_LOGS", "1")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(threadName)-12s] %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("debug-scrape")

from app.scrapers import search_all, extract_servers, get_adapters
from app.scrapers.models import SourceMedia

COLORS = {
    "larroza": "\033[92m",
    "shahid": "\033[93m",
    "ramoflix": "\033[94m",
    "shuttletv": "\033[95m",
    "aether": "\033[96m",
    "soap2day": "\033[91m",
    "hdtoday": "\033[97m",
    "RESET": "\033[0m",
    "GREEN": "\033[92m",
    "RED": "\033[91m",
    "BOLD": "\033[1m",
}

log_queue: queue.Queue[tuple[str, float, str]] = queue.Queue()


class QueueHandler(logging.Handler):
    def emit(self, record):
        log_queue.put((record.threadName, time.time(), self.format(record)))


def _install_queue_handler():
    handler = QueueHandler()
    handler.setFormatter(logging.Formatter("%(levelname)-5s %(message)s"))
    handler.setLevel(logging.INFO)
    logging.getLogger("app.scrapers").addHandler(handler)
    logging.getLogger("app.scrapers").setLevel(logging.INFO)


def _flush_logs():
    while not log_queue.empty():
        _tname, _ts, msg = log_queue.get_nowait()
        print(f"  {msg}")


def _color_for(adapter_id: str) -> str:
    return COLORS.get(adapter_id, "")


def _reset() -> str:
    return COLORS["RESET"]


def search_movie(query: str):
    print(f"\n{COLORS['BOLD']}======= SEARCHING: '{query}' (PARALLEL across all {len(get_adapters())} sources) ======={_reset()}")
    print(f"Timeout per adapter: {os.environ.get('SCRAPE_ADAPTER_TIMEOUT', '20')}s\n")

    t0 = time.time()
    results = search_all(query, "movie")
    elapsed = time.time() - t0

    _flush_logs()

    total_found = 0
    for r in results:
        c = _color_for(r["adapter_id"])
        count = len(r.get("media_list", []))
        total_found += count
        status = f"{c}{count} results{_reset()}"
        if r.get("error"):
            status = f"{_reset()}{COLORS['RED']}ERROR: {r['error']}{_reset()}"
        print(f"  [{c}{r['adapter_name']:12s}{_reset()}] {status}")

    print(f"\n{COLORS['BOLD']}Total: {total_found} results across {len(results)} sources in {elapsed:.1f}s{_reset()}")

    return results


def extract_from_results(results: list[dict], dump_html: bool = False):
    print(f"\n{COLORS['BOLD']}======= EXTRACTING VIDEO STREAMS (PARALLEL) ======={_reset()}\n")

    items = []
    for r in results:
        for m in r.get("media_list", []):
            items.append({
                "title": m["title"],
                "adapter_id": r["adapter_id"],
                "source_id": m["source_id"],
                "media_type": m.get("media_type", "movie"),
            })

    if not items:
        print(f"  {COLORS['RED']}No items to extract  -  search returned nothing{_reset()}")
        return

    print(f"Extracting {len(items)} items across {len(set(i['adapter_id'] for i in items))} adapters...\n")

    max_extract = int(os.environ.get("DEBUG_MAX_EXTRACT", "5"))
    items = items[:max_extract]

    def extract_one(idx: int, item: dict):
        with _LogCapture() as cap:
            try:
                result = extract_servers(item["adapter_id"], item["source_id"], item["title"], item["media_type"])
                servers = result.get("servers", [])
                return idx, item, servers, result.get("error"), cap.logs
            except Exception as exc:
                return idx, item, [], str(exc), cap.logs

    from concurrent.futures import ThreadPoolExecutor, as_completed
    extract_timeout = int(os.environ.get("SCRAPE_BATCH_ITEM_TIMEOUT", "45"))

    t0 = time.time()
    results_by_idx: dict[int, tuple] = {}
    with ThreadPoolExecutor(max_workers=min(len(items), 5), thread_name_prefix="extract") as executor:
        futures = {executor.submit(extract_one, i, item): i for i, item in enumerate(items)}
        for future in as_completed(futures):
            try:
                idx, item, servers, error, cap_logs = future.result(timeout=extract_timeout)
                results_by_idx[idx] = (item, servers, error, cap_logs)
            except Exception as exc:
                idx = futures[future]
                item = items[idx]
                results_by_idx[idx] = (item, [], f"timed out: {exc}", [])

    elapsed = time.time() - t0

    for idx in range(len(items)):
        if idx not in results_by_idx:
            continue
        item, servers, error, cap_logs = results_by_idx[idx]
        c = _color_for(item["adapter_id"])
        title_short = item["title"][:60]
        print(f"\n{COLORS['BOLD']}--- [{c}{item['adapter_id']}{_reset()}] {title_short} ---{_reset()}")

        if error:
            print(f"  {COLORS['RED']}ERROR: {error}{_reset()}")
            continue

        if not servers:
            print(f"  {COLORS['RED']}No video streams found{_reset()}")
            continue

        print(f"  {COLORS['GREEN']}Found {len(servers)} stream(s):{_reset()}")
        for s in servers:
            url = s.get("video_url", "")
            quality = s.get("quality", "")
            direct = s.get("direct", False)
            lang = s.get("language", "")
            subs = s.get("subtitles", "")
            tags = []
            if quality:
                tags.append(quality)
            if lang:
                tags.append(lang)
            if subs:
                tags.append(subs)
            if direct:
                tags.append("DIRECT")
            tag_str = f" [{', '.join(tags)}]" if tags else ""
            print(f"    {COLORS['GREEN']}-> {url[:150]}{tag_str}{_reset()}")

        if dump_html:
            dump_dir = ROOT / "data" / "debug"
            dump_dir.mkdir(parents=True, exist_ok=True)
            dump_path = dump_dir / f"extract_{item['adapter_id']}_{idx}.json"
            dump_path.write_text(json.dumps({
                "item": item,
                "servers": servers,
                "error": error,
                "logs": cap_logs,
            }, indent=2, default=str), encoding="utf-8")
            print(f"  Dumped to: {dump_path}")

    print(f"\n{COLORS['BOLD']}Extraction done in {elapsed:.1f}s{_reset()}")


class _LogCapture:
    def __init__(self):
        self._buf = []

    def __enter__(self):
        self._handler = logging.StreamHandler(sys.stdout)
        self._handler.setLevel(logging.DEBUG)
        self._handler.addFilter(self._filter)
        return self

    def _filter(self, record):
        self._buf.append(record.getMessage())
        return False

    def __exit__(self, *a):
        logging.getLogger().removeHandler(self._handler)

    @property
    def logs(self):
        return list(self._buf)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Debug scraper sources in parallel")
    parser.add_argument("query", nargs="?", default="The Invention of Lying", help="Movie/series to search for")
    parser.add_argument("--extract", "-e", action="store_true", help="Also extract video streams from results")
    parser.add_argument("--dump", "-d", action="store_true", help="Dump extraction results to JSON")
    parser.add_argument("--timeout", "-t", type=int, default=20, help="Per-adapter search timeout (seconds)")
    parser.add_argument("--max-extract", "-m", type=int, default=5, help="Max items to extract servers for")
    args = parser.parse_args()

    os.environ["SCRAPE_ADAPTER_TIMEOUT"] = str(args.timeout)
    os.environ["DEBUG_MAX_EXTRACT"] = str(args.max_extract)

    _install_queue_handler()

    t_total = time.time()

    results = search_movie(args.query)

    _flush_logs()

    if args.extract:
        extract_from_results(results, dump_html=args.dump)
        _flush_logs()

    total_elapsed = time.time() - t_total
    print(f"\n{COLORS['BOLD']}======= DEBUG COMPLETE ({total_elapsed:.1f}s total) ======={_reset()}")


if __name__ == "__main__":
    main()
