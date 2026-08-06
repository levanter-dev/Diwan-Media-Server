import asyncio
import json
import logging
import os
import time
import uuid
import re as _re
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from .content_analysis import (
    AnalysisError, analysis_payload, cancel_analysis, category_definitions, clear_analysis,
    enqueue_analysis, ensure_worker as ensure_analysis_worker, get_filter_settings, list_analysis_jobs, runtime_status, save_filter_settings, save_media_filter_overrides, save_content_segment_override,
)
from .config import MEDIA_ROOTS
from .db import connect
from .downloads import DOWNLOAD_DIR, DownloadError, action as download_action, delete_download, discover_sources, enqueue_download, ensure_worker as ensure_download_worker, get_download, get_download_settings, list_downloads, refetch_download, set_download_settings
from .explore import ExploreError, discover, explore_home, explore_search, fetch_details, provider_status, selected_search_provider, set_secret, set_setting, suggestions_from_seeds
from .media_paths import media_folder, safe_folder_name
from .scanner import scan, scan_media_folder
from .scrapers.browser import BrowserPool
from .scrapers.scrape_log import CaptureContext
from .scrapers.subscene import search_subtitles as subscene_search, get_subtitle_download_url as subscene_dl_url, download_and_extract_srt as subscene_extract
from .scrapers.opensubtitles import search_subtitles as opensub_search, get_subtitle_download_url as opensub_dl_url, download_and_extract_srt as opensub_extract, status as opensub_status, set_settings as opensub_set_settings, last_error as opensub_last_error

app = FastAPI(title="Diwan", version="0.3.0")
WEB = (Path(__file__).parent.parent / "web").resolve()
logger = logging.getLogger(__name__)

# Experimental features toggle — scrapers and download-from-source are off by default.
# Set EXPERIMENTAL=1 in .env to enable them.
_EXPERIMENTAL = os.getenv("EXPERIMENTAL", "0") == "1"

if _EXPERIMENTAL:
    from .scrapers import search_all, extract_servers

@app.on_event("startup")
def start_background_workers() -> None:
    ensure_analysis_worker()
    ensure_download_worker()

class LibraryIn(BaseModel):
    name: str
    root: str
    kind: str = "mixed"

class SettingsIn(BaseModel):
    language: str

class ProviderSettingsIn(BaseModel):
    omdb_api_key: str | None = None
    tmdb_token: str | None = None
    search_provider: str | None = None

class DownloadDiscoverIn(BaseModel):
    provider: str | None = None
    external_id: str | None = None
    media_type: str = "movie"
    title: str
    year: str | None = None

class DownloadIn(BaseModel):
    title: str
    source_url: str
    media_type: str = "movie"
    source_name: str = "Manual direct URL"
    provider: str | None = None
    external_id: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    adapter_id: str | None = None
    adapter_source_id: str | None = None
    adapter_server_id: str | None = None
    library_id: int | None = None

class DownloadSettingsIn(BaseModel):
    download_chunks: int = 1

class ScrapeSearchIn(BaseModel):
    query: str
    media_type: str | None = None

class ScrapeServersIn(BaseModel):
    adapter_id: str
    source_id: str
    title: str
    media_type: str = "movie"

class ScrapeExtractIn(BaseModel):
    url: str

class ScrapeBatchIn(BaseModel):
    items: list[ScrapeServersIn]

class ExploreDetailsIn(BaseModel):
    provider: str
    external_id: str
    media_type: str | None = None

class MediaConnectIn(BaseModel):
    provider: str
    external_id: str
    media_type: str | None = None

class MediaCreateIn(BaseModel):
    title: str
    media_type: str
    library_id: int | None = None

class SeasonCreateIn(BaseModel):
    season_number: int
    title: str | None = None

class EpisodeCreateIn(BaseModel):
    episode_number: int
    title: str | None = None
class ScoreIn(BaseModel):
    circle_id: int = 1
    provider: str
    external_id: str
    media_type: str
    title: str
    score: int
    year: str | None = None
    poster_url: str | None = None
    backdrop_url: str | None = None
    overview: str | None = None
    notes: str | None = None

class SuggestionSeedIn(BaseModel):
    provider: str
    external_id: str
    media_type: str
    title: str
    year: str | None = None
    poster_url: str | None = None

class CircleMemberIn(BaseModel):
    name: str

class ScoringSettingsIn(BaseModel):
    mode: str = "average"
    circle_id: int | None = None

class ContentAnalysisIn(BaseModel):
    categories: list[str] | None = None
    sample_interval: float = 1.0

class ContentFilterSettingsIn(BaseModel):
    policy: dict[str, str]
    sensitivity: str = "balanced"
    auto_analyze: bool = False

class MediaFilterOverridesIn(BaseModel):
    enabled: dict[str, bool]

class ContentSegmentOverrideIn(BaseModel):
    enabled: bool
class MediaDirectUrlIn(BaseModel):
    url: str
    label: str | None = None
class MediaVersionMetaIn(BaseModel):
    label: str | None = None
    quality: str | None = None
    language: str | None = None
    subtitles: str | None = None
    notes: str | None = None
class WatchedIn(BaseModel):
    watched: bool = True
    delete_file: bool = True
def allowed_directory(raw_path: str) -> Path:
    if not raw_path:
        raise HTTPException(400, "Select a folder")
    candidate = Path(raw_path).resolve()
    if not candidate.is_dir():
        raise HTTPException(400, "The folder does not exist inside the server container")
    for configured in MEDIA_ROOTS:
        root = configured.resolve()
        if candidate == root or root in candidate.parents:
            return candidate
    raise HTTPException(400, "The folder is outside the configured media roots")

@app.get("/api/health")
def health():
    connect().close()
    return {"status": "ok"}

@app.get("/api/libraries")
def libraries():
    conn = connect()
    rows = [dict(row) for row in conn.execute("SELECT * FROM libraries WHERE root<>'portal://library' ORDER BY name")]
    conn.close()
    return rows

@app.post("/api/libraries", status_code=201)
def create_library(body: LibraryIn):
    if body.kind not in {"movies", "series", "music", "mixed"}:
        raise HTTPException(400, "Unsupported library type")
    if not body.name.strip():
        raise HTTPException(400, "Library name is required")
    root = allowed_directory(body.root)
    conn = connect()
    try:
        cursor = conn.execute("INSERT INTO libraries(name,root,kind) VALUES (?,?,?)", (body.name.strip(), str(root), body.kind))
        conn.commit()
        return {"id": cursor.lastrowid, "name": body.name.strip(), "root": str(root), "kind": body.kind}
    except Exception as exc:
        conn.rollback()
        raise HTTPException(400, str(exc))
    finally:
        conn.close()

@app.put("/api/libraries/{library_id}")
def update_library(library_id: int, body: LibraryIn):
    if body.kind not in {"movies", "series", "music", "mixed"}:
        raise HTTPException(400, "Unsupported library type")
    root = allowed_directory(body.root)
    conn = connect()
    cursor = conn.execute("UPDATE libraries SET name=?,root=?,kind=? WHERE id=?", (body.name.strip(), str(root), body.kind, library_id))
    conn.commit()
    conn.close()
    if not cursor.rowcount:
        raise HTTPException(404, "Library not found")
    return {"id": library_id, "name": body.name.strip(), "root": str(root), "kind": body.kind}

@app.delete("/api/libraries/{library_id}", status_code=204)
def delete_library(library_id: int):
    conn = connect()
    library = conn.execute("SELECT root FROM libraries WHERE id=?", (library_id,)).fetchone()
    if library and library["root"] == "portal://library":
        conn.close()
        raise HTTPException(400, "The portal entry library is managed automatically")
    conn.execute("DELETE FROM media_items WHERE library_id=?", (library_id,))
    cursor = conn.execute("DELETE FROM libraries WHERE id=?", (library_id,))
    conn.commit()
    conn.close()
    if not cursor.rowcount:
        raise HTTPException(404, "Library not found")

@app.get("/api/folders")
def folders(path: str | None = None):
    if path is None:
        roots = []
        for configured in MEDIA_ROOTS:
            root = configured.resolve()
            if root.is_dir():
                roots.append({"name": root.name or str(root), "path": str(root)})
        return {"path": None, "parent": None, "folders": roots}
    selected = allowed_directory(path)
    parent = None
    for configured in MEDIA_ROOTS:
        root = configured.resolve()
        if selected != root and (selected.parent == root or root in selected.parent.parents):
            parent = str(selected.parent)
            break
    try:
        children = [{"name": child.name, "path": str(child)} for child in sorted(selected.iterdir(), key=lambda item: item.name.lower()) if child.is_dir() and not child.name.startswith(".")]
    except OSError:
        children = []
    return {"path": str(selected), "parent": parent, "folders": children}

@app.get("/api/settings")
def get_settings():
    conn = connect()
    row = conn.execute("SELECT value FROM settings WHERE key='language'").fetchone()
    conn.close()
    return {"language": row["value"] if row else "en"}

@app.put("/api/settings")
def save_settings(body: SettingsIn):
    if body.language not in {"en", "de"}:
        raise HTTPException(400, "Unsupported language")
    conn = connect()
    conn.execute("INSERT INTO settings(key,value) VALUES('language',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (body.language,))
    conn.commit()
    conn.close()
    return body

@app.get("/api/settings/providers")
def get_provider_settings():
    return provider_status()

@app.get("/api/status/experimental")
def experimental_status():
    return {"experimental": _EXPERIMENTAL}

@app.put("/api/settings/providers")
def save_provider_settings(body: ProviderSettingsIn):
    if body.search_provider is not None and body.search_provider not in {"tmdb", "omdb"}:
        raise HTTPException(400, "Unsupported search provider")
    set_secret("omdb_api_key", body.omdb_api_key)
    set_secret("tmdb_token", body.tmdb_token)
    set_setting("explore_search_provider", body.search_provider)
    return provider_status()

@app.get("/api/explore/home")
def explore_home_endpoint():
    try:
        return explore_home()
    except ExploreError as exc:
        raise HTTPException(502, str(exc))

@app.get("/api/explore/search")
def explore_search_endpoint(q: str, media_type: str | None = None, page: int = 1):
    if not q.strip():
        raise HTTPException(400, "Search text is required")
    try:
        return explore_search(q.strip(), media_type, page)
    except ExploreError as exc:
        raise HTTPException(502, str(exc))


@app.get("/api/explore/discover")
def discover_endpoint(media_type: str = "movie", genres: str | None = None,
                      year_min: int | None = None, year_max: int | None = None,
                      rating_min: float | None = None, sort_by: str = "popularity.desc",
                      parental: str | None = None, page: int = 1):
    try:
        return discover(media_type, genres, year_min, year_max, rating_min, sort_by, parental, page)
    except ExploreError as exc:
        raise HTTPException(502, str(exc))


def _portal_library_id(conn) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO libraries(name,root,kind) VALUES(?,?,?)",
        ("Portal entries", "portal://library", "mixed"),
    )
    row = conn.execute("SELECT id FROM libraries WHERE root=?", ("portal://library",)).fetchone()
    return int(row["id"])


def _portal_destination(conn, library_id: int | None, media_type: str) -> tuple[int, Path]:
    row = None
    if library_id is not None:
        row = conn.execute("SELECT id,root,kind FROM libraries WHERE id=? AND root<>'portal://library'", (library_id,)).fetchone()
        if not row:
            raise HTTPException(400, "Selected library was not found")
    if row is None:
        kind = "series" if media_type == "series" else "movies"
        row = conn.execute(
            "SELECT id,root,kind FROM libraries WHERE kind=? AND root<>'portal://library' ORDER BY id LIMIT 1",
            (kind,),
        ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT id,root,kind FROM libraries WHERE kind='mixed' AND root<>'portal://library' ORDER BY id LIMIT 1"
        ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT id,root,kind FROM libraries WHERE root<>'portal://library' ORDER BY id LIMIT 1"
        ).fetchone()
    if row:
        return int(row["id"]), Path(row["root"])
    return _portal_library_id(conn), DOWNLOAD_DIR


def _create_portal_media(conn, title: str, media_type: str, parent_id: int | None = None,
                         season_number: int | None = None, episode_number: int | None = None,
                         library_id: int | None = None, folder_path: Path | None = None,
                         metadata: dict | None = None) -> int:
    library_id = library_id or _portal_library_id(conn)
    path = f"portal://media/{uuid.uuid4()}"
    serialized = json.dumps(metadata, ensure_ascii=False, indent=2) if metadata else None
    cursor = conn.execute(
        """INSERT INTO media_items(library_id,title,media_type,path,size,modified,status,parent_id,
                   season_number,episode_number,entry_origin,folder_path,metadata)
           VALUES(?,?,?,?,?,?,'planned',?,?,?,'portal',?,?)""",
        (library_id, title, media_type, path, 0, time.time(), parent_id, season_number,
         episode_number, str(folder_path) if folder_path else None, serialized),
    )
    return int(cursor.lastrowid)


@app.post("/api/media", status_code=201)
def create_media_entry(body: MediaCreateIn):
    title = body.title.strip()
    media_type = body.media_type.strip().lower()
    if not title:
        raise HTTPException(400, "Title is required")
    if media_type not in {"movie", "series"}:
        raise HTTPException(400, "Create either a movie or series")
    conn = connect()
    try:
        library_id, root = _portal_destination(conn, body.library_id, media_type)
        folder = media_folder(root, title, media_type)
        folder.mkdir(parents=True, exist_ok=True)
        media_id = _create_portal_media(conn, title, media_type, library_id=library_id, folder_path=folder)
        conn.commit()
        row = conn.execute("SELECT * FROM media_items WHERE id=?", (media_id,)).fetchone()
        return dict(row)
    except OSError as exc:
        conn.rollback()
        raise HTTPException(500, f"Could not create the media folder: {exc}") from exc
    finally:
        conn.close()


@app.post("/api/media/{series_id}/seasons", status_code=201)
def create_series_season(series_id: int, body: SeasonCreateIn):
    if body.season_number < 0:
        raise HTTPException(400, "Season number cannot be negative")
    conn = connect()
    series = conn.execute("SELECT id,title,media_type,library_id,folder_path,metadata FROM media_items WHERE id=?", (series_id,)).fetchone()
    if not series or series["media_type"] != "series":
        conn.close()
        raise HTTPException(404, "Series not found")
    duplicate = conn.execute("SELECT id FROM media_items WHERE parent_id=? AND media_type='season' AND season_number=?", (series_id, body.season_number)).fetchone()
    if duplicate:
        conn.close()
        raise HTTPException(409, f"Season {body.season_number} already exists")
    title = (body.title or f"Season {body.season_number}").strip()
    folder = Path(series["folder_path"]) / str(body.season_number) if series["folder_path"] else None
    try:
        if folder:
            folder.mkdir(parents=True, exist_ok=True)
        parent_meta = json.loads(series["metadata"]) if series["metadata"] else {}
        metadata = {**parent_meta, "title": title, "series_title": series["title"], "media_type": "season", "season_number": body.season_number}
        season_id = _create_portal_media(conn, title, "season", series_id, body.season_number,
                                         library_id=int(series["library_id"]), folder_path=folder, metadata=metadata)
        conn.commit()
        return dict(conn.execute("SELECT * FROM media_items WHERE id=?", (season_id,)).fetchone())
    finally:
        conn.close()


@app.post("/api/media/{season_id}/episodes", status_code=201)
def create_season_episode(season_id: int, body: EpisodeCreateIn):
    if body.episode_number < 1:
        raise HTTPException(400, "Episode number must be at least 1")
    conn = connect()
    season = conn.execute("SELECT id,parent_id,season_number,media_type,library_id,folder_path,metadata FROM media_items WHERE id=?", (season_id,)).fetchone()
    if not season or season["media_type"] != "season":
        conn.close()
        raise HTTPException(404, "Season not found")
    duplicate = conn.execute("SELECT id FROM media_items WHERE parent_id=? AND media_type='episode' AND episode_number=?", (season_id, body.episode_number)).fetchone()
    if duplicate:
        conn.close()
        raise HTTPException(409, f"Episode {body.episode_number} already exists")
    title = (body.title or f"Episode {body.episode_number}").strip()
    folder = Path(season["folder_path"]) / str(body.episode_number) if season["folder_path"] else None
    try:
        if folder:
            folder.mkdir(parents=True, exist_ok=True)
        season_meta = json.loads(season["metadata"]) if season["metadata"] else {}
        metadata = {**season_meta, "title": title, "media_type": "episode", "season_number": int(season["season_number"]), "episode_number": body.episode_number}
        episode_id = _create_portal_media(conn, title, "episode", season_id, int(season["season_number"]), body.episode_number,
                                          int(season["library_id"]), folder, metadata)
        conn.commit()
        return dict(conn.execute("SELECT * FROM media_items WHERE id=?", (episode_id,)).fetchone())
    finally:
        conn.close()

@app.get("/api/media/{series_id}/structure")
def series_structure(series_id: int):
    conn = connect()
    series = conn.execute("SELECT * FROM media_items WHERE id=?", (series_id,)).fetchone()
    if not series or series["media_type"] != "series":
        conn.close()
        raise HTTPException(404, "Series not found")
    seasons = [dict(row) for row in conn.execute(
        "SELECT * FROM media_items WHERE parent_id=? AND media_type='season' ORDER BY season_number,id",
        (series_id,),
    )]
    for season in seasons:
        season["episodes"] = [dict(row) for row in conn.execute(
            "SELECT * FROM media_items WHERE parent_id=? AND media_type='episode' ORDER BY episode_number,id",
            (season["id"],),
        )]
    conn.close()
    return {"series": dict(series), "seasons": seasons}

@app.post("/api/explore/details")
def explore_details(body: ExploreDetailsIn):
    if not body.external_id.strip():
        raise HTTPException(400, "External ID is required")
    try:
        return fetch_details(body.provider.strip(), body.external_id.strip(), body.media_type)
    except ExploreError as exc:
        raise HTTPException(502, str(exc))


@app.post("/api/media/{media_id}/connect")
def connect_media(media_id: int, body: MediaConnectIn):
    provider = body.provider.strip().lower()
    external_id = body.external_id.strip()
    if provider not in {"tmdb", "omdb"}:
        raise HTTPException(400, "Unsupported metadata provider")
    if not external_id:
        raise HTTPException(400, "External ID is required")

    conn = connect()
    row = conn.execute(
        "SELECT id,path,title,media_type,metadata,parent_id,library_id,folder_path,entry_origin FROM media_items WHERE id=?",
        (media_id,),
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Media not found")
    if row["parent_id"] is not None or row["media_type"] not in {"movie", "series"}:
        conn.close()
        raise HTTPException(400, "Connect the parent movie or series; seasons and episodes inherit it")

    try:
        details = fetch_details(provider, external_id, body.media_type)
    except ExploreError as exc:
        conn.close()
        raise HTTPException(502, str(exc))

    existing = {}
    if row["metadata"]:
        try:
            existing = json.loads(row["metadata"])
        except (TypeError, json.JSONDecodeError):
            existing = {}
    linked = {
        **existing,
        **details,
        "provider": provider,
        "external_id": str(details.get("id") or external_id),
    }
    title = str(details.get("title") or row["title"]).strip()
    serialized = json.dumps(linked, ensure_ascii=False, indent=2)
    updated_folder = row["folder_path"]
    old_folder = Path(row["folder_path"]) if row["folder_path"] else None
    if old_folder and old_folder.exists():
        target_folder = old_folder.parent / safe_folder_name(title)
        if target_folder != old_folder and not target_folder.exists():
            old_folder.rename(target_folder)
            updated_folder = str(target_folder)
            descendants = conn.execute(
                """WITH RECURSIVE children(id,folder_path) AS (
                       SELECT id,folder_path FROM media_items WHERE parent_id=?
                       UNION ALL
                       SELECT child.id,child.folder_path FROM media_items child JOIN children parent ON child.parent_id=parent.id
                   ) SELECT id,folder_path FROM children""", (media_id,)
            ).fetchall()
            old_prefix, new_prefix = str(old_folder), str(target_folder)
            for child in descendants:
                if child["folder_path"] and str(child["folder_path"]).startswith(old_prefix):
                    conn.execute("UPDATE media_items SET folder_path=? WHERE id=?", (new_prefix + str(child["folder_path"])[len(old_prefix):], child["id"]))
    media_path = Path(row["path"])
    is_portal_entry = str(row["path"]).startswith("portal://")
    sidecar = None if is_portal_entry else Path(str(media_path) + ".meta.json")
    temporary = None if sidecar is None else sidecar.with_name(sidecar.name + ".tmp")
    try:
        if temporary is not None and sidecar is not None:
            temporary.write_text(serialized, encoding="utf-8")
            temporary.replace(sidecar)
        conn.execute(
            "UPDATE media_items SET title=?,metadata=?,folder_path=? WHERE id=?",
            (title, serialized, updated_folder, media_id),
        )
        if row["media_type"] == "series":
            seasons = conn.execute("SELECT id,title,season_number FROM media_items WHERE parent_id=? AND media_type='season'", (media_id,)).fetchall()
            for season in seasons:
                season_meta = {**linked, "title": season["title"], "series_title": title, "media_type": "season", "season_number": season["season_number"]}
                conn.execute("UPDATE media_items SET metadata=? WHERE id=?", (json.dumps(season_meta, ensure_ascii=False, indent=2), season["id"]))
                episodes = conn.execute("SELECT id,title,season_number,episode_number FROM media_items WHERE parent_id=? AND media_type='episode'", (season["id"],)).fetchall()
                for episode in episodes:
                    episode_meta = {**linked, "title": episode["title"], "series_title": title, "media_type": "episode", "season_number": episode["season_number"], "episode_number": episode["episode_number"]}
                    conn.execute("UPDATE media_items SET metadata=? WHERE id=?", (json.dumps(episode_meta, ensure_ascii=False, indent=2), episode["id"]))
        conn.commit()
    except OSError as exc:
        conn.rollback()
        raise HTTPException(500, f"Could not save metadata beside the media file: {exc}")
    finally:
        conn.close()
        if temporary is not None and temporary.exists():
            temporary.unlink(missing_ok=True)
    return {
        "media_id": media_id,
        "title": title,
        "provider": provider,
        "external_id": linked["external_id"],
        "metadata": linked,
    }


@app.post("/api/downloads/discover")
def download_discover(body: DownloadDiscoverIn):
    with CaptureContext() as cap:
        result = discover_sources(body.model_dump())
        result["logs"] = cap.logs
        return result

@app.get("/api/queue")
def queue_jobs():
    ensure_analysis_worker()
    ensure_download_worker()
    return {"filters": list_analysis_jobs(), "downloads": list_downloads()}


@app.get("/api/downloads")
def downloads(status: str | None = None):
    ensure_download_worker()
    return list_downloads(status)

@app.post("/api/downloads", status_code=201)
def create_download(body: DownloadIn):
    try:
        return enqueue_download(
            body.title, body.media_type, body.source_url, body.source_name,
            body.provider, body.external_id,
            body.season_number, body.episode_number,
            body.adapter_id, body.adapter_source_id, body.adapter_server_id,
            body.library_id,
        )
    except DownloadError as exc:
        raise HTTPException(400, str(exc))

@app.get("/api/downloads/{job_id}")
def download_detail(job_id: int):
    job = get_download(job_id)
    if not job:
        raise HTTPException(404, "Download job not found")
    return job

@app.post("/api/downloads/{job_id}/open-folder")
def open_download_folder(job_id: int):
    job = get_download(job_id)
    if not job:
        raise HTTPException(404, "Download job not found")
    import os as _os
    path = job.get("destination_path") or ""
    folder = str(Path(path).parent) if path and Path(path).is_file() else path
    if not folder or not Path(folder).exists():
        folder = str(DOWNLOAD_DIR)
        Path(folder).mkdir(parents=True, exist_ok=True)
    if _os.name == "nt":
        _os.startfile(folder)
    return {"opened": folder}

@app.post("/api/downloads/{job_id}/{command}")
def download_command(job_id: int, command: str):
    try:
        job = download_action(job_id, command)
    except DownloadError as exc:
        raise HTTPException(400, str(exc))
    if not job:
        raise HTTPException(404, "Download job not found")
    return job

@app.delete("/api/downloads/{job_id}", status_code=204)
def remove_download(job_id: int, delete_file: bool = False):
    if not delete_download(job_id, delete_file):
        raise HTTPException(404, "Download job not found")


@app.delete("/api/downloads", status_code=204)
def clear_all_downloads(delete_files: bool = False, status: str | None = None):
    conn = connect()
    if status:
        allowed = {s.strip() for s in status.split(",") if s.strip()}
        valid = {"queued", "downloading", "paused", "stopped", "completed", "failed", "pause_requested", "stop_requested"}
        allowed &= valid
        if not allowed:
            conn.close()
            raise HTTPException(400, "No valid status values provided")
        placeholders = ",".join("?" for _ in allowed)
        conn.execute(f"DELETE FROM download_jobs WHERE status IN ({placeholders})", tuple(allowed))
    else:
        conn.execute("DELETE FROM download_jobs WHERE status IN ('completed','stopped','failed','paused','queued')")
    conn.commit()
    conn.close()


def _remove_folder_contents(folder: Path) -> bool:
    if not folder.is_dir():
        return False
    removed = False
    for entry in list(folder.iterdir()):
        if entry.is_file():
            try:
                entry.unlink()
                removed = True
            except OSError:
                pass
        elif entry.is_dir():
            if _remove_folder_contents(entry):
                try:
                    entry.rmdir()
                except OSError:
                    pass
                removed = True
    return removed


def _unlink_media_file(file_path: Path) -> bool:
    if not file_path.exists():
        return False
    if not file_path.is_file():
        raise OSError(f"Media path is not a file: {file_path}")
    import gc
    last_error: OSError | None = None
    for attempt in range(12):
        try:
            file_path.unlink()
            if file_path.exists():
                raise OSError("The file still exists after deletion")
            return True
        except OSError as exc:
            last_error = exc
            if attempt < 11:
                gc.collect()
                time.sleep(0.3 * (attempt + 1))
    assert last_error is not None
    raise last_error


def _remove_media_sidecars(file_path: Path, include_artwork: bool) -> None:
    candidates: list[Path] = []
    try:
        candidates.extend(
            candidate for candidate in file_path.parent.iterdir()
            if candidate.is_file()
            and candidate.name.startswith(file_path.stem + ".")
            and candidate.suffix.lower() in {".srt", ".vtt", ".ass", ".ssa", ".sub"}
        )
        if include_artwork:
            candidates.append(file_path.with_name(file_path.stem + "-poster.jpg"))
            candidates.extend(file_path.parent.glob(file_path.name + ".meta*"))
            candidates.extend(file_path.parent.glob(file_path.stem + ".nfo"))
    except OSError:
        return
    for candidate in set(candidates):
        try:
            if candidate.is_file():
                candidate.unlink()
        except OSError:
            pass


@app.post("/api/media/{media_id}/watched")
def mark_media_watched(media_id: int, body: WatchedIn):
    conn = connect()
    row = conn.execute("SELECT id,path,file_deleted,folder_path FROM media_items WHERE id=?", (media_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Media not found")
    file_deleted = bool(row["file_deleted"])
    if body.watched and body.delete_file and not file_deleted:
        path = row["path"]
        is_portal = str(path).startswith("portal://")
        if is_portal:
            if row["folder_path"]:
                folder = Path(row["folder_path"])
                if folder.is_dir():
                    _remove_folder_contents(folder)
                    file_deleted = True
        else:
            file_path = Path(path)
            try:
                _unlink_media_file(file_path)
                file_deleted = not file_path.exists()
                if not file_deleted:
                    raise OSError("The media file still exists after deletion")
            except OSError as exc:
                conn.close()
                raise HTTPException(500, f"Could not remove the media file: {exc}") from exc
            _remove_media_sidecars(file_path, include_artwork=False)
    conn.execute("""UPDATE media_items SET watched=?, watched_at=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,
                    file_deleted=?, status=CASE WHEN ? THEN 'watched' ELSE CASE WHEN file_deleted THEN 'missing' ELSE 'ready' END END
                    WHERE id=?""",
                 (1 if body.watched else 0, 1 if body.watched else 0, 1 if file_deleted else 0, 1 if body.watched else 0, media_id))
    if body.watched:
        conn.execute("DELETE FROM playback_progress WHERE media_id=?", (media_id,))
    conn.commit()
    updated = conn.execute("SELECT * FROM media_items WHERE id=?", (media_id,)).fetchone()
    conn.close()
    return dict(updated)

@app.get("/api/progress/continue")
def continue_watching():
    conn = connect()
    rows = [dict(row) for row in conn.execute("""SELECT media_items.id,media_items.title,media_items.media_type,media_items.metadata,
               media_items.duration,media_items.watched,media_items.file_deleted,playback_progress.position_ms,
               playback_progress.duration_ms,playback_progress.updated_at
        FROM playback_progress JOIN media_items ON media_items.id=playback_progress.media_id
        WHERE playback_progress.position_ms>0 AND playback_progress.finished=0 AND media_items.watched=0 AND media_items.file_deleted=0
        ORDER BY playback_progress.updated_at DESC LIMIT 20""")]
    conn.close()
    return rows
@app.delete("/api/media/{media_id}", status_code=204)
def remove_media(media_id: int):
    conn = connect()
    root = conn.execute("SELECT id FROM media_items WHERE id=?", (media_id,)).fetchone()
    if not root:
        conn.close()
        raise HTTPException(404, "Media not found")
    rows = conn.execute(
        """WITH RECURSIVE descendants(id,path,entry_origin,folder_path) AS (
               SELECT id,path,entry_origin,folder_path FROM media_items WHERE id=?
               UNION ALL
               SELECT child.id,child.path,child.entry_origin,child.folder_path FROM media_items child
               JOIN descendants parent ON child.parent_id=parent.id
           ) SELECT * FROM descendants""",
        (media_id,),
    ).fetchall()
    for row in rows:
        is_portal = row["entry_origin"] == "portal" or str(row["path"]).startswith("portal://")
        if is_portal:
            if row["folder_path"]:
                folder = Path(row["folder_path"])
                if folder.is_dir():
                    _remove_folder_contents(folder)
                    try:
                        folder.rmdir()
                    except OSError:
                        pass
        else:
            file_path = Path(row["path"])
            try:
                _unlink_media_file(file_path)
            except OSError as exc:
                conn.close()
                raise HTTPException(500, f"Could not remove the media file: {exc}") from exc
            _remove_media_sidecars(file_path, include_artwork=True)
    ids = [int(row["id"]) for row in rows]
    for item_id in reversed(ids):
        conn.execute("DELETE FROM media_items WHERE id=?", (item_id,))
    conn.commit()
    conn.close()

@app.post("/api/downloads/{job_id}/refetch")
def refetch_download_endpoint(job_id: int):
    result = refetch_download(job_id)
    if not result:
        raise HTTPException(404, "Download job not found")
    return result

@app.get("/api/settings/download")
def get_download_settings_endpoint():
    return get_download_settings()

@app.put("/api/settings/download")
def set_download_settings_endpoint(body: DownloadSettingsIn):
    return set_download_settings(chunks=body.download_chunks)

@app.post("/api/scrape/search")
def scrape_search(body: ScrapeSearchIn):
    if not _EXPERIMENTAL:
        raise HTTPException(404, "Experimental features are not enabled")
    with CaptureContext() as cap:
        results = search_all(body.query, body.media_type)
        return {"results": results, "logs": cap.logs}

@app.post("/api/scrape/servers")
def scrape_servers(body: ScrapeServersIn):
    if not _EXPERIMENTAL:
        raise HTTPException(404, "Experimental features are not enabled")
    with CaptureContext() as cap:
        result = extract_servers(body.adapter_id, body.source_id, body.title, body.media_type)
        if "error" in result:
            raise HTTPException(400, result["error"])
        result["logs"] = cap.logs
        return result

@app.post("/api/scrape/servers-batch")
async def scrape_servers_batch(body: ScrapeBatchIn):
    if not _EXPERIMENTAL:
        raise HTTPException(404, "Experimental features are not enabled")
    from fastapi.responses import StreamingResponse
    import asyncio, json, time as time_module

    BATCH_ITEM_TIMEOUT = int(os.environ.get("SCRAPE_BATCH_ITEM_TIMEOUT", "45"))

    def _extract_one(item):
        with CaptureContext() as cap:
            try:
                result = extract_servers(item.adapter_id, item.source_id, item.title, item.media_type)
                if "error" in result:
                    return {"adapter_id": item.adapter_id, "source_id": item.source_id,
                            "title": item.title, "error": result["error"], "servers": [], "logs": cap.logs}
                return {"adapter_id": item.adapter_id, "source_id": item.source_id,
                        "title": item.title, "servers": result.get("servers", []),
                        "logs": cap.logs}
            except Exception as exc:
                return {"adapter_id": item.adapter_id, "source_id": item.source_id,
                        "title": item.title, "error": str(exc), "servers": [], "logs": cap.logs}

    async def _extract_with_timeout(item):
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_extract_one, item),
                timeout=BATCH_ITEM_TIMEOUT,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError, TimeoutError):
            return {"adapter_id": item.adapter_id, "source_id": item.source_id,
                    "title": item.title, "error": f"timed out after {BATCH_ITEM_TIMEOUT}s", "servers": [], "logs": []}

    async def generate():
        total = len(body.items)
        yield f"data: {json.dumps({'type': 'start', 'total': total, 'parallel': True})}\n\n"

        tasks = [_extract_with_timeout(item) for item in body.items]
        t0 = time_module.time()
        all_entries = await asyncio.gather(*tasks)
        elapsed = time_module.time() - t0
        logger.info("Batch extraction completed in %.1fs (%d items parallel)", elapsed, total)

        for idx, entry in enumerate(all_entries):
            yield f"data: {json.dumps({'type': 'result', 'index': idx, 'total': total, **entry}, default=str)}\n\n"

        yield f"data: {json.dumps({'type': 'done', 'results': all_entries, 'elapsed': round(elapsed, 1)}, default=str)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")

@app.post("/api/scrape/extract")
def scrape_extract(body: ScrapeExtractIn):
    if not _EXPERIMENTAL:
        raise HTTPException(404, "Experimental features are not enabled")
    if not body.url.strip():
        raise HTTPException(400, "URL is required")
    with CaptureContext() as cap:
        try:
            servers = BrowserPool.get().extract_video_urls(body.url.strip())
        except Exception as exc:
            raise HTTPException(502, str(exc))
        return {"servers": [s.to_dict() for s in servers], "logs": cap.logs}

@app.post("/api/scrape/enqueue")
def scrape_enqueue(body: ScrapeBatchIn):
    if not _EXPERIMENTAL:
        raise HTTPException(404, "Experimental features are not enabled")
    from .scrapers.extraction_queue import enqueue as enqueue_extraction
    items = [{"title": i.title, "adapter_id": i.adapter_id, "source_id": i.source_id, "media_type": i.media_type} for i in body.items]
    jobs = enqueue_extraction(items)
    return {"jobs": jobs}

@app.get("/api/scrape/extraction-jobs")
def scrape_extraction_jobs():
    if not _EXPERIMENTAL:
        raise HTTPException(404, "Experimental features are not enabled")
    from .scrapers.extraction_queue import list_jobs as list_extraction_jobs
    return {"jobs": list_extraction_jobs()}

@app.post("/api/scrape/extraction-jobs/{job_id}/cancel")
def scrape_extraction_cancel(job_id: int):
    if not _EXPERIMENTAL:
        raise HTTPException(404, "Experimental features are not enabled")
    from .scrapers.extraction_queue import cancel_job
    if cancel_job(job_id):
        return {"ok": True}
    raise HTTPException(404, "Job not found or already completed")

@app.delete("/api/scrape/extraction-jobs/{job_id}")
def scrape_extraction_delete(job_id: int):
    if not _EXPERIMENTAL:
        raise HTTPException(404, "Experimental features are not enabled")
    from .scrapers.extraction_queue import delete_job
    if delete_job(job_id):
        return {"ok": True}
    raise HTTPException(404, "Job not found")

@app.post("/api/downloads/idm")
def download_to_idm(body: DownloadIn):
    import subprocess, shutil
    paths = [
        r"C:\Program Files (x86)\Internet Download Manager\IDMan.exe",
        r"C:\Program Files\Internet Download Manager\IDMan.exe",
    ]
    idm_path = None
    for p in paths:
        if Path(p).exists():
            idm_path = p
            break
    if not idm_path:
        idm_path = shutil.which("IDMan") or shutil.which("idman")
    if not idm_path:
        raise HTTPException(404, "IDM not found. Install Internet Download Manager or set IDM_PATH.")
    try:
        subprocess.Popen([idm_path, "/d", body.source_url, "/p", str(Path.home() / "Downloads"), "/f", body.title + ".mp4"])
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(500, f"Failed to launch IDM: {exc}")

@app.get("/api/circle")
def circle_members():
    conn = connect()
    members = [dict(row) for row in conn.execute("SELECT * FROM circle_members ORDER BY id")]
    mode_row = conn.execute("SELECT value FROM settings WHERE key='score_aggregation'").fetchone()
    member_row = conn.execute("SELECT value FROM settings WHERE key='score_member_id'").fetchone()
    conn.close()
    return {"members": members, "mode": mode_row["value"] if mode_row else "average",
            "circle_id": int(member_row["value"]) if member_row and member_row["value"].isdigit() else 1}

@app.post("/api/circle", status_code=201)
def add_circle_member(body: CircleMemberIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Circle member name is required")
    conn = connect()
    try:
        cursor = conn.execute("INSERT INTO circle_members(name) VALUES(?)", (name,))
        conn.commit()
        row = conn.execute("SELECT * FROM circle_members WHERE id=?", (cursor.lastrowid,)).fetchone()
    except Exception as exc:
        conn.close()
        raise HTTPException(409, "That Circle member already exists") from exc
    conn.close()
    return dict(row)

@app.put("/api/circle/{circle_id}")
def rename_circle_member(circle_id: int, body: CircleMemberIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Circle member name is required")
    conn = connect()
    try:
        cursor = conn.execute("UPDATE circle_members SET name=? WHERE id=?", (name, circle_id))
        conn.commit()
    except Exception as exc:
        conn.close()
        raise HTTPException(409, "That Circle member already exists") from exc
    conn.close()
    if not cursor.rowcount:
        raise HTTPException(404, "Circle member not found")
    return {"id": circle_id, "name": name}

@app.delete("/api/circle/{circle_id}", status_code=204)
def delete_circle_member(circle_id: int):
    conn = connect()
    count = conn.execute("SELECT COUNT(*) AS count FROM circle_members").fetchone()["count"]
    if count <= 1:
        conn.close()
        raise HTTPException(400, "A Circle must keep at least one member")
    cursor = conn.execute("DELETE FROM circle_members WHERE id=?", (circle_id,))
    selected = conn.execute("SELECT value FROM settings WHERE key='score_member_id'").fetchone()
    if cursor.rowcount and selected and selected["value"] == str(circle_id):
        replacement = conn.execute("SELECT id FROM circle_members ORDER BY id LIMIT 1").fetchone()
        if replacement:
            conn.execute("INSERT INTO settings(key,value) VALUES('score_member_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(replacement["id"]),))
    conn.commit()
    conn.close()
    if not cursor.rowcount:
        raise HTTPException(404, "Circle member not found")

@app.put("/api/settings/scoring")
def save_scoring_settings(body: ScoringSettingsIn):
    if body.mode not in {"average", "member"}:
        raise HTTPException(400, "Scoring mode must be average or member")
    conn = connect()
    circle_id = int(body.circle_id or 1)
    if body.mode == "member" and not conn.execute("SELECT 1 FROM circle_members WHERE id=?", (circle_id,)).fetchone():
        conn.close()
        raise HTTPException(404, "Circle member not found")
    conn.execute("INSERT INTO settings(key,value) VALUES('score_aggregation',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (body.mode,))
    conn.execute("INSERT INTO settings(key,value) VALUES('score_member_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(circle_id),))
    conn.commit()
    conn.close()
    return {"mode": body.mode, "circle_id": circle_id}


def _recommendation_score_seeds(conn) -> list[dict]:
    mode_row = conn.execute("SELECT value FROM settings WHERE key='score_aggregation'").fetchone()
    mode = mode_row["value"] if mode_row else "average"
    if mode == "member":
        member_row = conn.execute("SELECT value FROM settings WHERE key='score_member_id'").fetchone()
        circle_id = int(member_row["value"]) if member_row and member_row["value"].isdigit() else 1
        return [dict(row) for row in conn.execute("SELECT provider,external_id,media_type,title,year,poster_url,score AS overall_score FROM circle_scores WHERE circle_id=? AND score>=7", (circle_id,))]
    return [dict(row) for row in conn.execute("""SELECT provider,external_id,media_type,title,year,poster_url,AVG(score) AS overall_score
                                                   FROM circle_scores GROUP BY provider,external_id HAVING AVG(score)>=7""")]
@app.get("/api/suggestions/seeds")
def suggestion_seeds():
    conn = connect()
    rows = [dict(row) for row in conn.execute("SELECT * FROM suggestion_seeds ORDER BY created_at DESC, title COLLATE NOCASE")]
    conn.close()
    return rows

@app.put("/api/suggestions/seeds")
def save_suggestion_seed(body: SuggestionSeedIn):
    provider = body.provider.strip().lower()
    external_id = body.external_id.strip()
    media_type = "series" if body.media_type in {"series", "episode", "tv"} else "movie"
    if provider not in {"tmdb", "omdb"} or not external_id or not body.title.strip():
        raise HTTPException(400, "A valid catalogue title is required")
    conn = connect()
    conn.execute(
        """INSERT INTO suggestion_seeds(provider,external_id,media_type,title,year,poster_url)
           VALUES(?,?,?,?,?,?) ON CONFLICT(provider,external_id) DO UPDATE SET
           media_type=excluded.media_type,title=excluded.title,year=excluded.year,poster_url=excluded.poster_url""",
        (provider, external_id, media_type, body.title.strip(), body.year, body.poster_url),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM suggestion_seeds WHERE provider=? AND external_id=?", (provider, external_id)).fetchone()
    conn.close()
    return dict(row)

@app.delete("/api/suggestions/seeds/{provider}/{external_id}", status_code=204)
def delete_suggestion_seed(provider: str, external_id: str):
    conn = connect()
    cursor = conn.execute("DELETE FROM suggestion_seeds WHERE provider=? AND external_id=?", (provider.lower(), external_id))
    conn.commit()
    conn.close()
    if not cursor.rowcount:
        raise HTTPException(404, "Suggestion title not found")

@app.get("/api/suggestions")
def suggestions(media_type: str = "", genres: str = "", release_date_from: str = "", release_date_to: str = ""):
    if media_type not in {"", "movie", "series"}:
        raise HTTPException(400, "Unsupported media type")
    if any(not value.strip().isdigit() for value in genres.split(",") if value.strip()):
        raise HTTPException(400, "Unsupported category")
    date_pattern = r"^\d{4}-\d{2}-\d{2}$"
    if release_date_from and not _re.match(date_pattern, release_date_from):
        raise HTTPException(400, "Invalid release start date")
    if release_date_to and not _re.match(date_pattern, release_date_to):
        raise HTTPException(400, "Invalid release end date")
    if release_date_from and release_date_to and release_date_from > release_date_to:
        raise HTTPException(400, "Release start date must be before the end date")
    conn = connect()
    seeds = [dict(row) for row in conn.execute("SELECT * FROM suggestion_seeds ORDER BY created_at DESC")]
    scored = _recommendation_score_seeds(conn)
    exclusions = [dict(row) for row in conn.execute("SELECT * FROM suggestion_exclusions")]
    conn.close()
    excluded_ids = {(row["provider"], str(row["external_id"])) for row in exclusions}
    seen = {(seed["provider"], str(seed["external_id"])) for seed in seeds}
    seeds.extend(seed for seed in scored if (seed["provider"], str(seed["external_id"])) not in seen)
    try:
        return suggestions_from_seeds(seeds, media_type, genres, release_date_from, release_date_to, excluded_ids)
    except ExploreError as exc:
        raise HTTPException(502, str(exc))
@app.get("/api/suggestions/exclusions")
def suggestion_exclusions():
    conn = connect()
    rows = [dict(row) for row in conn.execute("SELECT * FROM suggestion_exclusions ORDER BY created_at DESC, title COLLATE NOCASE")]
    conn.close()
    return rows
@app.put("/api/suggestions/exclusions")
def save_suggestion_exclusion(body: SuggestionSeedIn):
    provider = body.provider.strip().lower()
    external_id = body.external_id.strip()
    media_type = "series" if body.media_type in {"series", "episode", "tv"} else "movie"
    if provider not in {"tmdb", "omdb"} or not external_id or not body.title.strip():
        raise HTTPException(400, "A valid catalogue title is required")
    conn = connect()
    conn.execute(
        """INSERT INTO suggestion_exclusions(provider,external_id,media_type,title,year,poster_url)
           VALUES(?,?,?,?,?,?) ON CONFLICT(provider,external_id) DO UPDATE SET
           media_type=excluded.media_type,title=excluded.title,year=excluded.year,poster_url=excluded.poster_url""",
        (provider, external_id, media_type, body.title.strip(), body.year, body.poster_url),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM suggestion_exclusions WHERE provider=? AND external_id=?", (provider, external_id)).fetchone()
    conn.close()
    return dict(row)
@app.delete("/api/suggestions/exclusions/{provider}/{external_id}", status_code=204)
def delete_suggestion_exclusion(provider: str, external_id: str):
    conn = connect()
    cursor = conn.execute("DELETE FROM suggestion_exclusions WHERE provider=? AND external_id=?", (provider.lower(), external_id))
    conn.commit()
    conn.close()
    if not cursor.rowcount:
        raise HTTPException(404, "Excluded title not found")
@app.get("/api/scores")
def scores(provider: str | None = None, media_type: str | None = None, min_score: int | None = None, circle_id: int = 1):
    conn = connect()
    if not conn.execute("SELECT 1 FROM circle_members WHERE id=?", (circle_id,)).fetchone():
        conn.close()
        raise HTTPException(404, "Circle member not found")
    sql = "SELECT * FROM circle_scores WHERE circle_id=?"
    args: list = [circle_id]
    if provider:
        sql += " AND provider=?"
        args.append(provider)
    if media_type:
        sql += " AND media_type=?"
        args.append(media_type)
    if min_score is not None:
        sql += " AND score>=?"
        args.append(min_score)
    sql += " ORDER BY score DESC, updated_at DESC, title COLLATE NOCASE"
    rows = [dict(row) for row in conn.execute(sql, args)]
    conn.close()
    return rows

@app.get("/api/scores/item/{provider}/{external_id}")
def item_circle_scores(provider: str, external_id: str):
    provider = provider.strip().lower()
    if provider not in {"tmdb", "omdb"} or not external_id.strip():
        raise HTTPException(400, "A valid catalogue item is required")
    conn = connect()
    rows = [dict(row) for row in conn.execute(
        """SELECT circle_members.id AS circle_id, circle_members.name, circle_scores.score,
                  circle_scores.notes, circle_scores.updated_at
           FROM circle_members
           LEFT JOIN circle_scores ON circle_scores.circle_id=circle_members.id
             AND circle_scores.provider=? AND circle_scores.external_id=?
           ORDER BY circle_members.id""",
        (provider, external_id.strip()),
    )]
    conn.close()
    return rows

@app.get("/api/scores/overall")
def overall_scores():
    conn = connect()
    rows = _recommendation_score_seeds(conn)
    conn.close()
    return rows

@app.put("/api/scores")
def save_score(body: ScoreIn):
    provider = body.provider.strip().lower()
    external_id = body.external_id.strip()
    if provider not in {"tmdb", "omdb"}:
        raise HTTPException(400, "Unsupported score provider")
    if not external_id:
        raise HTTPException(400, "External id is required")
    if body.media_type not in {"movie", "series", "episode"}:
        raise HTTPException(400, "Unsupported media type")
    if body.score < 1 or body.score > 10:
        raise HTTPException(400, "Score must be from 1 to 10")
    if not body.title.strip():
        raise HTTPException(400, "Title is required")
    conn = connect()
    if not conn.execute("SELECT 1 FROM circle_members WHERE id=?", (body.circle_id,)).fetchone():
        conn.close()
        raise HTTPException(404, "Circle member not found")
    conn.execute(
        """INSERT INTO circle_scores(circle_id,provider,external_id,media_type,title,year,poster_url,backdrop_url,overview,score,notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(circle_id,provider,external_id) DO UPDATE SET
             media_type=excluded.media_type,title=excluded.title,year=excluded.year,
             poster_url=excluded.poster_url,backdrop_url=excluded.backdrop_url,
             overview=excluded.overview,score=excluded.score,notes=excluded.notes,
             updated_at=CURRENT_TIMESTAMP""",
        (body.circle_id, provider, external_id, body.media_type, body.title.strip(), body.year,
         body.poster_url, body.backdrop_url, body.overview, body.score, body.notes),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM circle_scores WHERE circle_id=? AND provider=? AND external_id=?", (body.circle_id, provider, external_id)).fetchone()
    conn.close()
    return dict(row)

@app.delete("/api/scores/{provider}/{external_id}", status_code=204)
def delete_score(provider: str, external_id: str, circle_id: int = 1):
    conn = connect()
    cursor = conn.execute("DELETE FROM circle_scores WHERE circle_id=? AND provider=? AND external_id=?", (circle_id, provider.lower(), external_id))
    conn.commit()
    conn.close()
    if not cursor.rowcount:
        raise HTTPException(404, "Score not found")
@app.get("/api/content-analysis/status")
def content_analysis_status():
    return {**runtime_status(), "categories": category_definitions()}

@app.get("/api/settings/content-filter")
def content_filter_settings():
    return get_filter_settings()

@app.put("/api/settings/content-filter")
def update_content_filter_settings(body: ContentFilterSettingsIn):
    try:
        return save_filter_settings(body.policy, body.sensitivity, body.auto_analyze)
    except AnalysisError as exc:
        raise HTTPException(400, str(exc)) from exc

@app.get("/api/media/{media_id}/content-analysis")
def get_content_analysis(media_id: int):
    try:
        return analysis_payload(media_id)
    except AnalysisError as exc:
        raise HTTPException(404, str(exc)) from exc

@app.put("/api/media/{media_id}/content-segments/{segment_id}")
def update_content_segment_override(media_id: int, segment_id: int, body: ContentSegmentOverrideIn):
    try:
        return save_content_segment_override(media_id, segment_id, body.enabled)
    except AnalysisError as exc:
        raise HTTPException(400, str(exc)) from exc

@app.put("/api/media/{media_id}/content-filter-overrides")
def update_media_filter_overrides(media_id: int, body: MediaFilterOverridesIn):
    try:
        return save_media_filter_overrides(media_id, body.enabled)
    except AnalysisError as exc:
        raise HTTPException(400, str(exc)) from exc

@app.post("/api/media/{media_id}/content-analysis", status_code=202)
def start_content_analysis(media_id: int, body: ContentAnalysisIn):
    try:
        return enqueue_analysis(media_id, body.categories, body.sample_interval)
    except AnalysisError as exc:
        raise HTTPException(400, str(exc)) from exc

@app.post("/api/media/{media_id}/content-analysis/cancel")
def stop_content_analysis(media_id: int):
    try:
        return cancel_analysis(media_id)
    except AnalysisError as exc:
        raise HTTPException(400, str(exc)) from exc

@app.delete("/api/media/{media_id}/content-analysis", status_code=204)
def delete_content_analysis(media_id: int):
    try:
        clear_analysis(media_id)
    except AnalysisError as exc:
        raise HTTPException(400, str(exc)) from exc
@app.delete("/api/content-analysis", status_code=204)
def clear_completed_analyses():
    conn = connect()
    conn.execute("DELETE FROM content_analysis_jobs WHERE status IN ('completed','cancelled','failed')")
    conn.commit()
    conn.close()
@app.delete("/api/scrape/extraction-jobs", status_code=204)
def scrape_extraction_clear_completed():
    conn = connect()
    conn.execute("DELETE FROM extraction_jobs WHERE status IN ('done','cancelled','failed')")
    conn.commit()
    conn.close()
@app.post("/api/scan")
def start_scan(library_id: int | None = None):
    return {"updated": scan(library_id)}


@app.post("/api/media/{media_id}/scan")
def scan_single_media(media_id: int):
    updated = scan_media_folder(media_id)
    if not updated:
        raise HTTPException(404, "No scannable folder found for this media item")
    return {"updated": updated}


@app.get("/api/media/{media_id}/versions")
def media_versions(media_id: int):
    conn = connect()
    versions = [dict(row) for row in conn.execute(
        "SELECT * FROM media_versions WHERE media_id=? ORDER BY is_default DESC, height DESC, size DESC",
        (media_id,),
    )]
    conn.close()
    return versions


@app.put("/api/media/{media_id}/versions/{version_id}")
def set_media_version(media_id: int, version_id: int):
    conn = connect()
    version = conn.execute(
        "SELECT * FROM media_versions WHERE id=? AND media_id=?", (version_id, media_id)
    ).fetchone()
    if not version:
        conn.close()
        raise HTTPException(404, "Version not found")
    # Mark all other versions as non-default
    conn.execute("UPDATE media_versions SET is_default=0 WHERE media_id=?", (media_id,))
    conn.execute("UPDATE media_versions SET is_default=1 WHERE id=?", (version_id,))
    # Update the media item's path/size/duration to match the chosen version
    conn.execute(
        """UPDATE media_items SET path=?,size=?,modified=?,duration=?,width=?,height=?
           WHERE id=?""",
        (version["path"], version["size"], version["modified"],
         version["duration"], version["width"], version["height"], media_id),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM media_items WHERE id=?", (media_id,)).fetchone()
    conn.close()
    return dict(updated)


@app.put("/api/media/{media_id}/versions/{version_id}/metadata")
def set_media_version_metadata(media_id: int, version_id: int, body: MediaVersionMetaIn):
    conn = connect()
    version = conn.execute(
        "SELECT * FROM media_versions WHERE id=? AND media_id=?", (version_id, media_id)
    ).fetchone()
    if not version:
        conn.close()
        raise HTTPException(404, "Version not found")
    existing: dict = {}
    if version["metadata"]:
        try:
            existing = json.loads(version["metadata"])
        except (TypeError, json.JSONDecodeError):
            existing = {}
    merged = {
        **existing,
        "quality": (body.quality or "").strip() or None,
        "language": (body.language or "").strip() or None,
        "subtitles": (body.subtitles or "").strip() or None,
        "notes": (body.notes or "").strip() or None,
    }
    # Keep metadata compact by dropping null values.
    merged = {k: v for k, v in merged.items() if v is not None}
    label = (body.label or "").strip() or (version["label"] or "Unknown")
    conn.execute(
        "UPDATE media_versions SET label=?, metadata=? WHERE id=?",
        (label, json.dumps(merged, ensure_ascii=False) if merged else None, version_id),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM media_versions WHERE id=?", (version_id,)).fetchone()
    conn.close()
    return dict(updated)


@app.post("/api/media/{media_id}/download", status_code=201)
def download_to_media_folder(media_id: int, body: MediaDirectUrlIn):
    url = body.url.strip()
    if not url or not url.startswith(("http://", "https://")):
        raise HTTPException(400, "A valid http or https URL is required")
    conn = connect()
    row = conn.execute(
        "SELECT id,title,media_type,folder_path,library_id FROM media_items WHERE id=? AND folder_path IS NOT NULL",
        (media_id,),
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Media item has no scannable folder")
    folder = Path(row["folder_path"])
    try:
        return enqueue_download(
            title=row["title"],
            media_type=row["media_type"],
            source_url=url,
            source_name=body.label or "Direct URL (attached to media)",
            library_id=int(row["library_id"]),
        )
    except DownloadError as exc:
        raise HTTPException(400, str(exc))
    finally:
        conn.close()


@app.get("/api/media")
def media(q: str | None = None, media_type: str | None = None):
    conn = connect()
    sql = """SELECT media_items.id,title,media_type,size,modified,duration,width,height,status,metadata,watched,file_deleted,watched_at,
             parent_id,season_number,episode_number,entry_origin,
             libraries.name AS library_name,media_items.path AS file_path FROM media_items
             JOIN libraries ON libraries.id=media_items.library_id"""
    args, clauses = [], ["parent_id IS NULL"]
    if q:
        clauses.append("title LIKE ?")
        args.append(f"%{q}%")
    if media_type == "series":
        clauses.append("media_type IN ('series','episode')")
    elif media_type:
        clauses.append("media_type=?")
        args.append(media_type)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY title"
    rows = [dict(row) for row in conn.execute(sql, args)]
    conn.close()
    return rows

@app.get("/api/media/{media_id}")
def media_detail(media_id: int):
    conn = connect()
    row = conn.execute("SELECT * FROM media_items WHERE id=?", (media_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Media not found")
    return dict(row)


@app.get("/api/media/{media_id}/stream")
def stream_media(media_id: int, request: Request):
    conn = connect()
    row = conn.execute("SELECT path, metadata FROM media_items WHERE id=?", (media_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Media not found")
    file_path = Path(row["path"])
    if not file_path.is_file():
        raise HTTPException(404, "File not found on disk")
    file_size = file_path.stat().st_size
    range_header = request.headers.get("range")
    content_type = "video/mp4" if file_path.suffix.lower() in {".mp4", ".m4v"} else "video/webm" if file_path.suffix.lower() in {".webm"} else "video/x-matroska"
    if range_header:
        m = _re.match(r"bytes=(\d+)-(\d*)", range_header)
        if m:
            start = int(m.group(1))
            end_s = m.group(2)
            end = int(end_s) if end_s else file_size - 1
            chunk_size = end - start + 1
            def _range_stream():
                with open(file_path, "rb") as f:
                    f.seek(start)
                    remaining = chunk_size
                    while remaining > 0:
                        buf = f.read(min(256 * 1024, remaining))
                        if not buf:
                            break
                        remaining -= len(buf)
                        yield buf
            return StreamingResponse(_range_stream(), status_code=206, media_type=content_type, headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(chunk_size),
            })
    return FileResponse(file_path, media_type=content_type)


@app.get("/api/media/{media_id}/poster")
def media_poster(media_id: int):
    conn = connect()
    row = conn.execute("SELECT path, metadata FROM media_items WHERE id=?", (media_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Media not found")
    file_path = Path(row["path"])
    local_poster = file_path.with_name(file_path.stem + "-poster.jpg")
    if local_poster.exists():
        return FileResponse(local_poster, media_type="image/jpeg")
    if row["metadata"]:
        try:
            import json as _json
            meta = _json.loads(row["metadata"])
            url = meta.get("poster_url")
            if url:
                from urllib.request import urlopen, Request as _Req
                req = _Req(url, headers={"User-Agent": "LocalMediaServer/0.4"})
                with urlopen(req, timeout=10) as resp:
                    from fastapi.responses import Response
                    return Response(content=resp.read(), media_type=resp.headers.get("Content-Type", "image/jpeg"))
        except Exception:
            pass
    raise HTTPException(404, "No poster available")


@app.get("/api/media/{media_id}/subtitles")
def media_subtitles(media_id: int, language: str | None = None):
    conn = connect()
    row = conn.execute("SELECT path FROM media_items WHERE id=?", (media_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Media not found")
    file_path = Path(row["path"])
    lang = (language or "en").lower().strip()[:8] or "en"
    candidates = [f".{lang}.srt", f".{lang}.vtt", ".srt", ".vtt"]
    if lang != "en":
        candidates.extend([".en.srt", ".en.vtt"])
    for ext in candidates:
        sub = file_path.with_suffix(ext)
        if sub.exists():
            return FileResponse(sub, media_type="text/plain; charset=utf-8")
        sub2 = file_path.parent / (file_path.stem + ext)
        if sub2.exists():
            return FileResponse(sub2, media_type="text/plain; charset=utf-8")
    raise HTTPException(404, "No subtitle file found")


@app.get("/api/media/{media_id}/related")
def related_media(media_id: int):
    conn = connect()
    row = conn.execute("SELECT metadata, media_type FROM media_items WHERE id=?", (media_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Media not found")
    genre = None
    if row["metadata"]:
        try:
            import json as _json
            meta = _json.loads(row["metadata"])
            genre = meta.get("genre")
        except Exception:
            pass
    if not genre:
        return {"items": []}
    try:
        provider = selected_search_provider()
        if provider == "omdb":
            import json as _json
            meta = _json.loads(row["metadata"]) if row["metadata"] else {}
            title = meta.get("title", "")
            resp = explore_search(title.split("(")[0].strip(), row["media_type"])
            return {"items": resp.get("results", [])[:8]}
        first_genre = genre.split(",")[0].strip()
        resp = explore_search(first_genre, row["media_type"])
        return {"items": resp.get("results", [])[:8]}
    except Exception:
        return {"items": []}


class SubtitleSettingsIn(BaseModel):
    username: str | None = None
    password: str | None = None
    api_key: str | None = None
class SubtitleSearchIn(BaseModel):
    query: str
    language: str = "en"

class PlaybackProgressIn(BaseModel):
    position_ms: int = 0
    duration_ms: int | None = None
    finished: bool = False



@app.get("/api/settings/subtitles")
def get_subtitle_settings():
    return opensub_status()

@app.put("/api/settings/subtitles")
def save_subtitle_settings(body: SubtitleSettingsIn):
    return opensub_set_settings(body.username, body.password, body.api_key)
@app.post("/api/subtitles/search")
def subtitle_search(body: SubtitleSearchIn):
    query = body.query.strip()
    attempted = ["opensubtitles"]
    errors = {}
    results = opensub_search(query, body.language)
    opensub_error = opensub_last_error()
    if opensub_error:
        errors["opensubtitles"] = opensub_error
    source = "opensubtitles" if results else "none"
    if not results:
        attempted.append("subscene")
        results = subscene_search(query, body.language)
        source = "subscene" if results else "none"
    return {"results": results, "source": source, "attempted_sources": attempted, "errors": errors}


class SubtitleDownloadIn(BaseModel):
    url: str


@app.post("/api/subtitles/download")
def subtitle_download(body: SubtitleDownloadIn):
    dl_url = subscene_dl_url(body.url.strip())
    if not dl_url:
        raise HTTPException(404, "Download link not found")
    srt = subscene_extract(dl_url)
    if not srt:
        raise HTTPException(404, "Could not extract subtitle file")
    return {"content": srt}




class MediaSubtitleDownloadIn(BaseModel):
    file_id: int | None = None
    url: str | None = None
    language: str = "en"
    title: str | None = None
    provider: str | None = None
    subtitle_id: str | None = None
    download_count: int | None = None

class SubtitleActiveIn(BaseModel):
    enabled: bool


def _subtitle_state_path(media_path: Path) -> Path:
    return media_path.with_name(media_path.stem + ".subtitle.json")


def _read_subtitle_state(media_path: Path) -> dict:
    state_path = _subtitle_state_path(media_path)
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_subtitle_state(media_path: Path, data: dict) -> None:
    _subtitle_state_path(media_path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _media_path_for_subtitles(media_id: int) -> Path:
    conn = connect()
    row = conn.execute("SELECT path FROM media_items WHERE id=?", (media_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Media not found")
    media_path = Path(row["path"])
    if not media_path.is_file():
        raise HTTPException(404, "Media file not found")
    return media_path


@app.get("/api/media/{media_id}/subtitles/active")
def active_media_subtitle(media_id: int):
    media_path = _media_path_for_subtitles(media_id)
    state = _read_subtitle_state(media_path)
    if state:
        filename = Path(str(state.get("filename") or "")).name
        subtitle_path = media_path.parent / filename
        enabled = bool(state.get("enabled", True))
        return {**state, "enabled": enabled, "available": subtitle_path.is_file(),
                "track_url": f"/api/media/{media_id}/subtitles/active/file" if enabled and subtitle_path.is_file() else None}
    for ext in (".en.srt", ".en.vtt", ".srt", ".vtt"):
        subtitle_path = media_path.with_name(media_path.stem + ext)
        if subtitle_path.is_file():
            state = {"enabled": True, "available": True, "title": subtitle_path.name, "provider": "local",
                     "language": "en", "filename": subtitle_path.name,
                     "selected_at": datetime.now(timezone.utc).isoformat()}
            _write_subtitle_state(media_path, state)
            return {**state, "track_url": f"/api/media/{media_id}/subtitles/active/file"}
    return {"enabled": False, "available": False, "title": None, "track_url": None}


@app.get("/api/media/{media_id}/subtitles/active/file")
def active_media_subtitle_file(media_id: int, filename: str = ""):
    media_path = _media_path_for_subtitles(media_id)
    state = _read_subtitle_state(media_path)
    chosen = Path(str(state.get("filename") or filename)).name
    if state and not state.get("enabled", True):
        raise HTTPException(404, "Subtitles are disabled")
    subtitle_path = media_path.parent / chosen
    if not chosen or not subtitle_path.is_file() or subtitle_path.parent.resolve() != media_path.parent.resolve():
        raise HTTPException(404, "Saved subtitle not found")
    return FileResponse(subtitle_path, media_type="text/plain; charset=utf-8")


@app.put("/api/media/{media_id}/subtitles/active")
def set_active_media_subtitle(media_id: int, body: SubtitleActiveIn):
    media_path = _media_path_for_subtitles(media_id)
    state = _read_subtitle_state(media_path)
    if not state:
        if body.enabled:
            raise HTTPException(404, "No saved subtitle is available")
        state = {"title": None, "provider": "local", "filename": None}
    state["enabled"] = bool(body.enabled)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write_subtitle_state(media_path, state)
    return state


@app.post("/api/media/{media_id}/subtitles/download")
def download_media_subtitle(media_id: int, body: MediaSubtitleDownloadIn):
    media_path = _media_path_for_subtitles(media_id)
    srt = None
    provider = (body.provider or ("opensubtitles" if body.file_id else "subscene")).strip().lower()
    if body.file_id:
        dl_url = opensub_dl_url(body.file_id)
        if dl_url:
            srt = opensub_extract(dl_url)
    elif body.url:
        dl_url = subscene_dl_url(body.url.strip())
        if dl_url:
            srt = subscene_extract(dl_url)
    if not srt:
        raise HTTPException(404, "Could not download that subtitle from the provider")
    language = (body.language or "en").lower().strip()[:8] or "en"
    target = media_path.with_name(media_path.stem + f".{language}.srt")
    target.write_text(srt, encoding="utf-8")
    state = {
        "enabled": True,
        "available": True,
        "title": (body.title or target.name).strip(),
        "provider": provider,
        "subtitle_id": body.subtitle_id or (str(body.file_id) if body.file_id else body.url),
        "language": language,
        "download_count": body.download_count,
        "filename": target.name,
        "selected_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_subtitle_state(media_path, state)
    return {**state, "saved": str(target), "track_url": f"/api/media/{media_id}/subtitles/active/file"}
@app.get("/api/media/{media_id}/progress")
def get_playback_progress(media_id: int):
    conn = connect()
    media = conn.execute("SELECT id, duration FROM media_items WHERE id=?", (media_id,)).fetchone()
    if not media:
        conn.close()
        raise HTTPException(404, "Media not found")
    row = conn.execute("SELECT media_id, position_ms, duration_ms, finished, updated_at FROM playback_progress WHERE media_id=?", (media_id,)).fetchone()
    conn.close()
    if not row:
        duration_ms = int((media["duration"] or 0) * 1000) if media["duration"] else None
        return {"media_id": media_id, "position_ms": 0, "duration_ms": duration_ms, "finished": False, "updated_at": None}
    data = dict(row)
    data["finished"] = bool(data.get("finished"))
    return data

@app.put("/api/media/{media_id}/progress")
def save_playback_progress(media_id: int, body: PlaybackProgressIn):
    position_ms = max(0, int(body.position_ms or 0))
    duration_ms = int(body.duration_ms) if body.duration_ms else None
    finished = 1 if body.finished else 0
    if duration_ms and duration_ms > 0 and duration_ms - position_ms < 30000:
        finished = 1
    if finished:
        position_ms = 0
    conn = connect()
    media = conn.execute("SELECT id FROM media_items WHERE id=?", (media_id,)).fetchone()
    if not media:
        conn.close()
        raise HTTPException(404, "Media not found")
    conn.execute(
        """
        INSERT INTO playback_progress(media_id, position_ms, duration_ms, finished, updated_at)
        VALUES(?,?,?,?,CURRENT_TIMESTAMP)
        ON CONFLICT(media_id) DO UPDATE SET
            position_ms=excluded.position_ms,
            duration_ms=excluded.duration_ms,
            finished=excluded.finished,
            updated_at=CURRENT_TIMESTAMP
        """,
        (media_id, position_ms, duration_ms, finished),
    )
    conn.commit()
    row = conn.execute("SELECT media_id, position_ms, duration_ms, finished, updated_at FROM playback_progress WHERE media_id=?", (media_id,)).fetchone()
    conn.close()
    data = dict(row)
    data["finished"] = bool(data.get("finished"))
    return data
@app.get("/api/media/{media_id}/subtitles/custom")
def media_custom_subtitle(media_id: int, url: str = "", file_id: int = 0):
    srt = None
    if file_id:
        dl_url = opensub_dl_url(file_id)
        if dl_url:
            srt = opensub_extract(dl_url)
    elif url:
        try:
            dl_url = subscene_dl_url(url)
            if dl_url:
                srt = subscene_extract(dl_url)
        except Exception:
            pass
    if not srt:
        raise HTTPException(404, "Could not fetch or parse that subtitle from the provider")
    from fastapi.responses import Response
    return Response(content=srt, media_type="text/plain; charset=utf-8")

@app.get("/{path:path}")
def frontend(path: str = ""):
    requested = (WEB / path).resolve() if path else WEB / "index.html"
    if requested.is_file() and WEB in requested.parents:
        mt = "text/html; charset=utf-8" if requested.suffix.lower() == ".html" else None
        return FileResponse(requested, media_type=mt)
    return FileResponse(WEB / "index.html", media_type="text/html; charset=utf-8")














