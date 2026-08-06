from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, quote as url_quote
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler

from .config import DATA_DIR
from .db import connect
from .media_paths import media_folder
from .scrapers import search_all, extract_servers

DOWNLOAD_DIR = DATA_DIR / "downloads"
_CHUNK_SIZE = 1024 * 256
_worker_lock = threading.Lock()
_worker_thread: threading.Thread | None = None
_worker_recovered = False


class DownloadError(RuntimeError):
    pass


def _safe_request(url: str, headers: dict[str, str] | None = None, method: str | None = None, timeout: int = 20):
    """Create a urllib Request with percent-encoded URL to handle non-ASCII characters."""
    try:
        url.encode("ascii")
    except UnicodeEncodeError:
        safe_chars = ":/?#[]@!$&'()*+,;=%"
        try:
            parts = list(urlparse(url))
            parts[2] = url_quote(parts[2], safe=safe_chars)
            parts[3] = url_quote(parts[3], safe=safe_chars)
            from urllib.parse import urlunsplit
            url = urlunsplit(parts)
        except Exception:
            url = url_quote(url, safe=safe_chars)
    req = Request(url, headers=headers or {})
    if method:
        req.method = method
    return req


def _urlopen_follow(url: str, headers: dict[str, str] | None = None, method: str | None = None, timeout: int = 20):
    """Open a URL following all HTTP redirects."""
    opener = build_opener(HTTPRedirectHandler())
    req = _safe_request(url, headers=headers, method=method)
    return opener.open(req, timeout=timeout)


def discover_sources(item: dict[str, Any]) -> dict[str, Any]:
    title = item.get("title", "").strip()
    if not title:
        return {
            "item": item,
            "sources": [],
            "message": "No title provided for source discovery.",
        }
    media_type = item.get("media_type", "movie")
    scraper_results = search_all(title, media_type)
    return {
        "item": item,
        "sources": scraper_results,
        "message": None,
    }


def enqueue_download(title: str, media_type: str, source_url: str, source_name: str = "Manual direct URL", provider: str | None = None, external_id: str | None = None, season_number: int | None = None, episode_number: int | None = None, adapter_id: str | None = None, adapter_source_id: str | None = None, adapter_server_id: str | None = None, library_id: int | None = None) -> dict[str, Any]:
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise DownloadError("Use a valid http or https download URL")
    clean_title = title.strip()
    if not clean_title:
        raise DownloadError("Title is required")
    destination_root = _resolve_destination_root(library_id, media_type)
    conn = connect()
    cursor = conn.execute(
        """INSERT INTO download_jobs(title,media_type,provider,external_id,season_number,episode_number,adapter_id,adapter_source_id,adapter_server_id,source_name,source_url,library_id,destination_root,status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'queued')""",
        (clean_title, media_type or "movie", provider, external_id, season_number, episode_number, adapter_id, adapter_source_id, adapter_server_id, source_name.strip() or "Manual direct URL", source_url.strip(), library_id, destination_root),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM download_jobs WHERE id=?", (cursor.lastrowid,)).fetchone()
    conn.close()
    ensure_worker()
    return dict(row)


def _resolve_destination_root(library_id: int | None, media_type: str) -> str | None:
    conn = connect()
    try:
        if library_id:
            row = conn.execute("SELECT root FROM libraries WHERE id=? AND root<>'portal://library'", (library_id,)).fetchone()
            if row:
                return row["root"]
        kind = "movies" if media_type == "movie" else ("series" if media_type in ("series", "episode") else "movies")
        row = conn.execute("SELECT root FROM libraries WHERE kind=? AND root<>'portal://library' ORDER BY id LIMIT 1", (kind,)).fetchone()
        if row:
            return row["root"]
        row = conn.execute("SELECT root FROM libraries WHERE kind='mixed' AND root<>'portal://library' ORDER BY id LIMIT 1").fetchone()
        if row:
            return row["root"]
        row = conn.execute("SELECT root FROM libraries WHERE root<>'portal://library' ORDER BY id LIMIT 1").fetchone()
        return row["root"] if row else None
    finally:
        conn.close()

def list_downloads(status: str | None = None) -> list[dict[str, Any]]:
    conn = connect()
    if status:
        rows = [dict(row) for row in conn.execute("SELECT * FROM download_jobs WHERE status=? ORDER BY created_at DESC", (status,))]
    else:
        rows = [dict(row) for row in conn.execute("SELECT * FROM download_jobs ORDER BY created_at DESC")]
    conn.close()
    return rows


def get_download(job_id: int) -> dict[str, Any] | None:
    conn = connect()
    row = conn.execute("SELECT * FROM download_jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def action(job_id: int, name: str) -> dict[str, Any] | None:
    if name not in {"pause", "resume", "stop", "retry"}:
        raise DownloadError("Unsupported download action")
    job = get_download(job_id)
    if not job:
        return None
    conn = connect()
    if name == "pause" and job["status"] in {"queued", "downloading"}:
        conn.execute("UPDATE download_jobs SET status='pause_requested', updated_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
    elif name == "stop" and job["status"] in {"queued", "downloading", "paused", "failed"}:
        conn.execute("UPDATE download_jobs SET status='stop_requested', updated_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
    elif name == "resume" and job["status"] in {"paused", "pause_requested", "stopped", "failed"}:
        conn.execute("UPDATE download_jobs SET status='queued', error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
    elif name == "retry" and job["status"] in {"failed", "stopped", "completed"}:
        conn.execute("UPDATE download_jobs SET status='queued', progress=0, bytes_downloaded=0, bytes_total=NULL, error=NULL, completed_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
    conn.commit()
    conn.close()
    ensure_worker()
    return get_download(job_id)


def delete_download(job_id: int, delete_file: bool = False) -> bool:
    job = get_download(job_id)
    if not job:
        return False
    if job["status"] == "downloading":
        action(job_id, "stop")
        time.sleep(0.2)
    if delete_file and job.get("destination_path"):
        for suffix in ("", ".part"):
            target = Path(job["destination_path"] + suffix) if suffix else Path(job["destination_path"])
            try:
                if target.is_file() and DOWNLOAD_DIR.resolve() in target.resolve().parents:
                    target.unlink()
            except OSError:
                pass
    conn = connect()
    cursor = conn.execute("DELETE FROM download_jobs WHERE id=?", (job_id,))
    conn.commit()
    conn.close()
    return bool(cursor.rowcount)


def refetch_download(job_id: int) -> dict[str, Any] | None:
    job = get_download(job_id)
    if not job:
        return None
    if job["status"] == "downloading":
        action(job_id, "stop")
        time.sleep(0.2)
    if job.get("destination_path"):
        for suffix in ("", ".part"):
            target = Path(job["destination_path"] + suffix) if suffix else Path(job["destination_path"])
            try:
                if target.is_file() and DOWNLOAD_DIR.resolve() in target.resolve().parents:
                    target.unlink()
            except OSError:
                pass
    conn = connect()
    if job.get("destination_path"):
        media = conn.execute("SELECT id FROM media_items WHERE path=?", (job["destination_path"],)).fetchone()
        if media:
            conn.execute("DELETE FROM playback_progress WHERE media_id=?", (media["id"],))
            conn.execute("DELETE FROM media_items WHERE id=?", (media["id"],))
    conn.execute("DELETE FROM download_jobs WHERE id=?", (job_id,))
    cursor = conn.execute(
        """INSERT INTO download_jobs(title,media_type,provider,external_id,season_number,episode_number,adapter_id,adapter_source_id,adapter_server_id,source_name,source_url,library_id,destination_root,status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'queued')""",
        (job["title"], job["media_type"], job.get("provider"), job.get("external_id"),
         job.get("season_number"), job.get("episode_number"),
         job.get("adapter_id"), job.get("adapter_source_id"), job.get("adapter_server_id"),
         job["source_name"], job["source_url"], job.get("library_id"), job.get("destination_root")),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM download_jobs WHERE id=?", (cursor.lastrowid,)).fetchone()
    conn.close()
    ensure_worker()
    return dict(row)


def ensure_worker() -> None:
    global _worker_thread, _worker_recovered
    with _worker_lock:
        if _worker_thread and _worker_thread.is_alive():
            return
        conn = connect()
        try:
            if not _worker_recovered:
                conn.execute("UPDATE download_jobs SET status='queued',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE status='downloading'")
                conn.execute("UPDATE download_jobs SET status='paused',updated_at=CURRENT_TIMESTAMP WHERE status='pause_requested'")
                conn.execute("UPDATE download_jobs SET status='stopped',updated_at=CURRENT_TIMESTAMP WHERE status='stop_requested'")
                conn.commit()
                _worker_recovered = True
            queued = conn.execute("SELECT COUNT(*) FROM download_jobs WHERE status='queued'").fetchone()[0]
        finally:
            conn.close()
        if not queued:
            return
        _worker_thread = threading.Thread(target=_worker_loop, name="download-worker", daemon=True)
        _worker_thread.start()

def _worker_loop() -> None:
    idle_ticks = 0
    while True:
        job = _next_job()
        if not job:
            idle_ticks += 1
            if idle_ticks > 30:
                return
            time.sleep(1)
            continue
        idle_ticks = 0
        _download(job)


def _next_job() -> dict[str, Any] | None:
    conn = connect()
    row = conn.execute("SELECT * FROM download_jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
    if row:
        conn.execute("UPDATE download_jobs SET status='downloading', error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?", (row["id"],))
        conn.commit()
    conn.close()
    return dict(row) if row else None


def _download(job: dict[str, Any]) -> None:
    try:
        destination = _destination_for(job)
        destination.parent.mkdir(parents=True, exist_ok=True)
        chunks = _get_download_chunks_setting()
        if chunks > 1 and _supports_range(job["source_url"]):
            _download_parallel(job, destination, chunks)
        else:
            _download_sequential(job, destination)
    except (HTTPError, URLError, OSError, TimeoutError) as exc:
        _fail(job["id"], str(exc))


def _download_sequential(job: dict[str, Any], destination: Path) -> None:
    part = Path(str(destination) + ".part")
    bytes_downloaded = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": "LocalMediaServer/0.4"}
    if bytes_downloaded:
        headers["Range"] = f"bytes={bytes_downloaded}-"
    request = _safe_request(job["source_url"], headers=headers)
    with _urlopen_follow(job["source_url"], headers=headers, timeout=20) as response:
        total = _total_size(response.headers.get("Content-Length"), bytes_downloaded, response.status)
        _update(job["id"], destination_path=str(destination), bytes_total=total, bytes_downloaded=bytes_downloaded, progress=_progress(bytes_downloaded, total))
        mode = "ab" if bytes_downloaded and response.status == 206 else "wb"
        if mode == "wb":
            bytes_downloaded = 0
        with open(part, mode) as handle:
            while True:
                status = _status(job["id"])
                if status == "pause_requested":
                    _set_status(job["id"], "paused")
                    return
                if status == "stop_requested":
                    _set_status(job["id"], "stopped")
                    return
                chunk = response.read(_CHUNK_SIZE)
                if not chunk:
                    break
                handle.write(chunk)
                bytes_downloaded += len(chunk)
                _update(job["id"], bytes_downloaded=bytes_downloaded, bytes_total=total, progress=_progress(bytes_downloaded, total))
    part.replace(destination)
    _complete(job["id"], destination, bytes_downloaded)


def _download_parallel(job: dict[str, Any], destination: Path, chunks: int) -> None:
    import concurrent.futures
    try:
        with _urlopen_follow(job["source_url"], headers={"User-Agent": "LocalMediaServer/0.4"}, method="HEAD", timeout=10) as resp:
            total = int(resp.headers.get("Content-Length", 0))
    except Exception:
        total = 0

    if not total:
        _download_sequential(job, destination)
        return

    chunk_size = max(_CHUNK_SIZE * 4, total // chunks)
    ranges: list[tuple[int, int]] = []
    pos = 0
    while pos < total:
        end = min(pos + chunk_size, total)
        ranges.append((pos, end - 1 if end < total else total - 1))
        pos = end

    def part_path(index: int) -> Path:
        return Path(str(destination) + f".part{index}")

    def saved_bytes(index: int) -> int:
        path = part_path(index)
        expected = ranges[index][1] - ranges[index][0] + 1
        try:
            return min(path.stat().st_size, expected)
        except OSError:
            return 0

    resumed_bytes = sum(saved_bytes(index) for index in range(len(ranges)))
    _update(job["id"], destination_path=str(destination), bytes_total=total,
            bytes_downloaded=resumed_bytes, progress=_progress(resumed_bytes, total))
    finished = [saved_bytes(index) == ranges[index][1] - ranges[index][0] + 1 for index in range(len(ranges))]
    error_occurred = False

    def _download_chunk(idx: int, start: int, end: int) -> int:
        nonlocal error_occurred
        if error_occurred:
            return saved_bytes(idx)
        path = part_path(idx)
        expected = end - start + 1
        existing = saved_bytes(idx)
        if existing >= expected:
            finished[idx] = True
            return existing
        if path.exists() and path.stat().st_size > expected:
            path.unlink()
            existing = 0
        headers = {"User-Agent": "LocalMediaServer/0.4", "Range": f"bytes={start + existing}-{end}"}
        written = existing
        try:
            with _urlopen_follow(job["source_url"], headers=headers, timeout=30) as resp:
                if existing and resp.status != 206:
                    raise DownloadError("Source did not honor the resumed byte range")
                with open(path, "ab" if existing else "wb") as handle:
                    while True:
                        status = _status(job["id"])
                        if status in ("pause_requested", "stop_requested"):
                            error_occurred = True
                            return written
                        data = resp.read(_CHUNK_SIZE)
                        if not data:
                            break
                        handle.write(data)
                        written += len(data)
            finished[idx] = written >= expected
        except Exception as exc:
            logging.getLogger(__name__).warning("Chunk %d failed: %s", idx, exc)
            error_occurred = True
        return written

    with concurrent.futures.ThreadPoolExecutor(max_workers=chunks) as pool:
        futures = [pool.submit(_download_chunk, idx, start, end)
                   for idx, (start, end) in enumerate(ranges)]
        while not all(future.done() for future in futures):
            status = _status(job["id"])
            if status == "pause_requested":
                _set_status(job["id"], "paused")
                return
            if status == "stop_requested":
                _set_status(job["id"], "stopped")
                return
            total_done = sum(saved_bytes(index) for index in range(len(ranges)))
            _update(job["id"], bytes_downloaded=total_done, bytes_total=total,
                    progress=_progress(total_done, total))
            time.sleep(0.5)
        for future in futures:
            future.result()

    if error_occurred or not all(finished):
        status = _status(job["id"])
        if status not in ("paused", "stopped"):
            _fail(job["id"], "One or more download chunks failed")
        return

    with open(destination, "wb") as output:
        for index in range(len(ranges)):
            path = part_path(index)
            with open(path, "rb") as source:
                while True:
                    data = source.read(_CHUNK_SIZE)
                    if not data:
                        break
                    output.write(data)
            try:
                path.unlink()
            except OSError:
                pass

    _complete(job["id"], destination, total)

def _supports_range(url: str) -> bool:
    try:
        with _urlopen_follow(url, headers={"User-Agent": "LocalMediaServer/0.4"}, method="HEAD", timeout=8) as resp:
            return resp.headers.get("Accept-Ranges") == "bytes" or resp.status == 206
    except Exception:
        return False


def _get_download_chunks_setting() -> int:
    try:
        conn = connect()
        row = conn.execute("SELECT value FROM settings WHERE key='download_chunks'").fetchone()
        conn.close()
        if row:
            val = int(row["value"])
            return max(1, min(8, val))
    except Exception:
        pass
    return 1


def get_download_settings() -> dict[str, Any]:
    conn = connect()
    row = conn.execute("SELECT value FROM settings WHERE key='download_chunks'").fetchone()
    conn.close()
    return {"download_chunks": int(row["value"]) if row else 1}


def set_download_settings(chunks: int | None = None) -> dict[str, Any]:
    conn = connect()
    if chunks is not None:
        val = max(1, min(8, chunks))
        conn.execute(
            "INSERT INTO settings(key,value) VALUES('download_chunks',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(val),),
        )
    conn.commit()
    conn.close()
    return get_download_settings()

def _destination_for(job: dict[str, Any]) -> Path:
    parsed = urlparse(job["source_url"])
    ext = Path(parsed.path).suffix
    if not ext or len(ext) > 12:
        ext = ".mp4" if job.get("media_type") in ("movie", "series", "episode") else ".bin"
    filename = _safe_filename(f"{job['title']}-{job['id']}{ext}")
    root = Path(job.get("destination_root") or DOWNLOAD_DIR)
    target_dir = media_folder(
        root, job["title"], job.get("media_type") or "movie",
        job.get("season_number"), job.get("episode_number"),
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / filename


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", value).strip(" .") or "download.bin"


def _total_size(header: str | None, already_downloaded: int, status: int) -> int | None:
    if not header:
        return None
    try:
        size = int(header)
    except ValueError:
        return None
    return size + already_downloaded if status == 206 else size


def _progress(done: int, total: int | None) -> float:
    return round((done / total) * 100, 2) if total else 0


def _status(job_id: int) -> str:
    conn = connect()
    row = conn.execute("SELECT status FROM download_jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    return row["status"] if row else "stopped"


def _set_status(job_id: int, status: str) -> None:
    conn = connect()
    conn.execute("UPDATE download_jobs SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, job_id))
    conn.commit()
    conn.close()


def _update(job_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = "CURRENT_TIMESTAMP"
    assignments, args = [], []
    for key, value in fields.items():
        if key == "updated_at":
            assignments.append("updated_at=CURRENT_TIMESTAMP")
        else:
            assignments.append(f"{key}=?")
            args.append(value)
    args.append(job_id)
    conn = connect()
    conn.execute(f"UPDATE download_jobs SET {', '.join(assignments)} WHERE id=?", args)
    conn.commit()
    conn.close()


def _complete(job_id: int, destination: Path, size: int) -> None:
    conn = connect()
    conn.execute("""UPDATE download_jobs SET status='completed', progress=100, bytes_downloaded=?, destination_path=?, completed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?""", (size, str(destination), job_id))
    conn.commit()
    row = conn.execute("SELECT * FROM download_jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    job = dict(row) if row else None
    if job:
        _save_metadata_sidecar(job, destination)
        try:
            from .scanner import scan
            scan()
        except Exception:
            pass


def _fail(job_id: int, error: str) -> None:
    conn = connect()
    conn.execute("UPDATE download_jobs SET status='failed', error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (error[:500], job_id))
    conn.commit()
    conn.close()


def _save_metadata_sidecar(job: dict[str, Any], destination: Path) -> None:
    provider = job.get("provider")
    external_id = job.get("external_id")
    if not provider or not external_id:
        return
    try:
        from .explore import fetch_details
        details = fetch_details(provider, external_id, job.get("media_type"))
    except Exception:
        return
    meta = {}
    for key in ("title", "year", "media_type", "overview", "rating", "genre",
                 "director", "actors", "runtime", "rated", "tagline", "vote_count"):
        val = details.get(key)
        if val:
            meta[key] = val
    meta["provider"] = provider
    meta["external_id"] = external_id
    if job.get("season_number") is not None:
        meta["series_title"] = details.get("title") or job.get("title")
        meta["season_number"] = int(job["season_number"])
        meta["media_type"] = "episode"
    if job.get("episode_number") is not None:
        meta["episode_number"] = int(job["episode_number"])
    try:
        json_path = destination.with_suffix(destination.suffix + ".meta.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
        nfo = _build_nfo(details)
        nfo_path = destination.with_name(destination.stem + ".nfo")
        with open(nfo_path, "w", encoding="utf-8") as f:
            f.write(nfo)
    except OSError:
        pass
    poster_url = details.get("poster_url")
    if poster_url:
        try:
            poster_path = destination.with_name(destination.stem + "-poster.jpg")
            req = Request(poster_url, headers={"User-Agent": "LocalMediaServer/0.4"})
            with urlopen(req, timeout=15) as resp:
                with open(poster_path, "wb") as f:
                    f.write(resp.read())
        except Exception:
            pass


def _build_nfo(details: dict[str, Any]) -> str:
    title = details.get("title") or ""
    year = details.get("year") or ""
    overview = details.get("overview") or ""
    rating = details.get("rating") or ""
    genre = details.get("genre") or ""
    director = details.get("director") or ""
    actors = details.get("actors") or ""
    runtime = details.get("runtime") or ""
    tag = details.get("media_type", "movie")
    out = '<?xml version="1.0" encoding="UTF-8"?>\n'
    out += f"<{tag}>\n"
    out += f"  <title>{_xml_escape(title)}</title>\n"
    if year:
        out += f"  <year>{_xml_escape(year)}</year>\n"
    if overview:
        out += f"  <plot>{_xml_escape(overview)}</plot>\n"
    if rating:
        out += f"  <rating>{_xml_escape(str(rating))}</rating>\n"
    if genre:
        out += f"  <genre>{_xml_escape(genre)}</genre>\n"
    if director:
        out += f"  <director>{_xml_escape(director)}</director>\n"
    if actors:
        out += f"  <actor>{_xml_escape(actors)}</actor>\n"
    if runtime:
        out += f"  <runtime>{_xml_escape(runtime)}</runtime>\n"
    out += f"</{tag}>\n"
    return out


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


