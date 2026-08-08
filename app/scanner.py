import json
import re
import subprocess
import uuid
from pathlib import Path
from .config import AUDIO_EXTENSIONS, FFPROBE_PATH, SUPPORTED_EXTENSIONS
from .db import connect
from .media_paths import series_parts

EPISODE = re.compile(r"S(?P<season>\d{1,2})E(?P<episode>\d{1,3})", re.I)

def title_for(path: Path, library_kind: str) -> tuple[str, str]:
    if library_kind == "music" or path.suffix.lower() in AUDIO_EXTENSIONS:
        return (re.sub(r"[._]", " ", path.stem).strip(), "music")
    match = EPISODE.search(path.stem)
    if match or library_kind == "series":
        episode = f"S{int(match['season']):02d}E{int(match['episode']):02d}" if match else path.stem
        show = path.parent.parent.name if match else path.parent.name
        return (f"{show} - {episode}", "episode")
    kind = "movie" if library_kind == "movies" else "video"
    return (re.sub(r"[._]", " ", path.stem).strip(), kind)

def probe(path: Path) -> dict:
    try:
        probe_command = FFPROBE_PATH if Path(FFPROBE_PATH).exists() else "ffprobe"
        raw = subprocess.check_output([probe_command, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)], timeout=30)
        data = json.loads(raw)
        fmt = data.get("format", {})
        video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
        return {"duration": float(fmt.get("duration", 0) or 0), "width": video.get("width"), "height": video.get("height")}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}

def scan(library_id: int | None = None) -> int:
    conn = connect()
    count = 0
    analysis_ids: list[int] = []
    sql, args = "SELECT id, root, kind FROM libraries", ()
    if library_id is not None:
        sql, args = sql + " WHERE id=?", (library_id,)
    for library in conn.execute(sql, args).fetchall():
        root = Path(library["root"])
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            stat = path.stat()
            existing = conn.execute("SELECT size, modified, file_deleted FROM media_items WHERE path=?", (str(path),)).fetchone()
            if existing and not existing["file_deleted"] and existing["size"] == stat.st_size and existing["modified"] == stat.st_mtime:
                continue
            hierarchy = series_parts(path, root) if library["kind"] == "series" else None
            if hierarchy:
                series_folder, season_number, episode_number = hierarchy
                series = conn.execute(
                    "SELECT id,title,metadata FROM media_items WHERE media_type='series' AND folder_path=? ORDER BY id LIMIT 1",
                    (str(series_folder),),
                ).fetchone()
                if series:
                    season_folder = series_folder / str(season_number)
                    season = conn.execute(
                        "SELECT id,metadata FROM media_items WHERE parent_id=? AND media_type='season' AND season_number=?",
                        (series["id"], season_number),
                    ).fetchone()
                    parent_meta = json.loads(series["metadata"]) if series["metadata"] else {}
                    if not season:
                        season_meta = {**parent_meta, "title": f"Season {season_number}", "series_title": series["title"],
                                       "media_type": "season", "season_number": season_number}
                        cursor = conn.execute(
                            """INSERT INTO media_items(library_id,title,media_type,path,size,modified,status,parent_id,season_number,
                                       entry_origin,folder_path,metadata)
                               VALUES(?,?,'season',?,0,?,'planned',?,?,'portal',?,?)""",
                            (library["id"], f"Season {season_number}", f"portal://media/{uuid.uuid4()}", stat.st_mtime,
                             series["id"], season_number, str(season_folder), json.dumps(season_meta, ensure_ascii=False)),
                        )
                        season_id = int(cursor.lastrowid)
                    else:
                        season_id = int(season["id"])
                    episode = conn.execute(
                        "SELECT id,title FROM media_items WHERE parent_id=? AND media_type='episode' AND episode_number=?",
                        (season_id, episode_number),
                    ).fetchone()
                    episode_title = episode["title"] if episode else f"Episode {episode_number}"
                    media_meta = probe(path)
                    sidecar_meta = _read_sidecar_metadata(path) or {}
                    episode_meta = {**parent_meta, **sidecar_meta, "title": episode_title, "series_title": series["title"],
                                    "media_type": "episode", "season_number": season_number, "episode_number": episode_number}
                    if episode:
                        conn.execute(
                            """UPDATE media_items SET library_id=?,path=?,size=?,modified=?,duration=?,width=?,height=?,status='ready',
                                       file_deleted=0,entry_origin='local',folder_path=?,metadata=? WHERE id=?""",
                            (library["id"], str(path), stat.st_size, stat.st_mtime, media_meta.get("duration"),
                             media_meta.get("width"), media_meta.get("height"), str(path.parent),
                             json.dumps(episode_meta, ensure_ascii=False), episode["id"]),
                        )
                        episode_id = int(episode["id"])
                    else:
                        cursor = conn.execute(
                            """INSERT INTO media_items(library_id,title,media_type,path,size,modified,duration,width,height,status,metadata,
                                       parent_id,season_number,episode_number,entry_origin,folder_path)
                               VALUES(?,?,'episode',?,?,?,?,?,?,'ready',?,?,?,?,'local',?)""",
                            (library["id"], episode_title, str(path), stat.st_size, stat.st_mtime, media_meta.get("duration"),
                             media_meta.get("width"), media_meta.get("height"), json.dumps(episode_meta, ensure_ascii=False),
                             season_id, season_number, episode_number, str(path.parent)),
                        )
                        episode_id = int(cursor.lastrowid)
                    count += 1
                    analysis_ids.append(episode_id)
                    continue
            title, media_type = title_for(path, library["kind"])
            meta = probe(path)
            sidecar_meta = _read_sidecar_metadata(path)
            metadata_json = json.dumps(sidecar_meta) if sidecar_meta else None
            if sidecar_meta and sidecar_meta.get("title"):
                title = sidecar_meta["title"]
            # If this file sits inside a portal entry's folder, upgrade the portal entry
            # instead of creating a duplicate scanned entry.
            portal_entry = conn.execute(
                "SELECT id,metadata,title FROM media_items WHERE entry_origin='portal' AND folder_path=? AND status='planned' AND parent_id IS NULL ORDER BY id LIMIT 1",
                (str(path.parent),),
            ).fetchone()
            if portal_entry:
                existing_meta = json.loads(portal_entry["metadata"]) if portal_entry["metadata"] else {}
                merged = {**existing_meta, **(sidecar_meta or {})}
                if sidecar_meta and sidecar_meta.get("title"):
                    title = sidecar_meta["title"]
                # Sync all media files in this folder as versions
                count += _sync_versions(
                    conn, path.parent, int(portal_entry["id"]),
                    library["id"], title, merged,
                )
                # Content analysis on the default version
                mid_row = conn.execute("SELECT id,media_type FROM media_items WHERE id=?", (portal_entry["id"],)).fetchone()
                if mid_row and mid_row["media_type"] != "music":
                    analysis_ids.append(int(mid_row["id"]))
            else:
                conn.execute("""INSERT INTO media_items(library_id,title,media_type,path,size,modified,duration,width,height,metadata)
                    VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
                    library_id=excluded.library_id,title=excluded.title,media_type=excluded.media_type,size=excluded.size,
                    modified=excluded.modified,duration=excluded.duration,width=excluded.width,height=excluded.height,
                    metadata=excluded.metadata,status='ready',file_deleted=0""",
                    (library["id"], title, media_type, str(path), stat.st_size, stat.st_mtime,
                     meta.get("duration"), meta.get("width"), meta.get("height"), metadata_json))
                count += 1
                if media_type != "music":
                    row = conn.execute("SELECT id FROM media_items WHERE path=?", (str(path),)).fetchone()
                    if row:
                        analysis_ids.append(int(row["id"]))
    auto_row = conn.execute("SELECT value FROM settings WHERE key='content_filter_auto_analyze'").fetchone()
    auto_analyze = bool(auto_row and auto_row["value"] == "1")
    conn.commit()
    conn.close()
    if auto_analyze and analysis_ids:
        from .content_analysis import enqueue_analysis
        for media_id in analysis_ids:
            try:
                enqueue_analysis(media_id)
            except Exception:
                pass
    return count


def _read_sidecar_metadata(media_path: Path) -> dict | None:
    sidecar = Path(str(media_path) + ".meta.json")
    if not sidecar.exists():
        sidecar = media_path.with_name(media_path.stem + ".meta.json")
    if not sidecar.exists():
        return None
    try:
        with open(sidecar, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _version_label(meta: dict) -> str:
    """Build a human-readable label from probe data."""
    parts: list[str] = []
    w = meta.get("width")
    h = meta.get("height")
    if w and h:
        if h >= 2160:
            parts.append("4K")
        elif h >= 1080:
            parts.append("1080p")
        elif h >= 720:
            parts.append("720p")
        elif h >= 480:
            parts.append("480p")
        else:
            parts.append(f"{h}p")
    return " - ".join(parts) if parts else "Unknown"


def _version_sort_key(entry: dict) -> tuple:
    """Sort versions so the best quality is first."""
    h = entry.get("height") or 0
    w = entry.get("width") or 0
    size = entry.get("size") or 0
    return (-h, -w, -size)


def _sync_versions(conn, folder: Path, media_id: int, library_id: int,
                   title: str, merged_meta: dict) -> int:
    """Scan *folder* for media files, sync them as versions of *media_id*,
    and pick the best one as the default (updating media_items.path etc.)."""
    # Find all media files in the folder
    files: list[Path] = sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    if not files:
        return 0

    # Remove stale scanned duplicates that point to paths in this folder
    for f in files:
        conn.execute("DELETE FROM media_items WHERE path=? AND id<>?", (str(f), media_id))

    # Probe every file and upsert version rows
    probed: list[dict] = []
    for f in files:
        stat = f.stat()
        meta = probe(f)
        probed.append({
            "path": str(f),
            "size": stat.st_size,
            "modified": stat.st_mtime,
            "duration": meta.get("duration"),
            "width": meta.get("width"),
            "height": meta.get("height"),
            "label": _version_label(meta),
        })

    # Sort best -> worst and pick the default
    probed.sort(key=_version_sort_key)
    default = probed[0]
    serialized_meta = json.dumps(merged_meta, ensure_ascii=False) if merged_meta else None

    # Upsert versions
    for entry in probed:
        conn.execute(
            """INSERT INTO media_versions(media_id,path,size,modified,duration,width,height,label,is_default)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(media_id,path) DO UPDATE SET
               size=excluded.size,modified=excluded.modified,duration=excluded.duration,
               width=excluded.width,height=excluded.height,label=excluded.label,
               is_default=excluded.is_default""",
            (media_id, entry["path"], entry["size"], entry["modified"],
             entry["duration"], entry["width"], entry["height"],
             entry["label"], 1 if entry is default else 0),
        )

    # Point the media item at the default version and mark ready
    conn.execute(
        """UPDATE media_items SET library_id=?,path=?,size=?,modified=?,duration=?,width=?,height=?,
           status='ready',file_deleted=0,entry_origin='portal',metadata=?,title=? WHERE id=?""",
        (library_id, default["path"], default["size"], default["modified"],
         default["duration"], default["width"], default["height"],
         serialized_meta, title, media_id),
    )
    return len(probed)


def scan_media_folder(media_id: int) -> int:
    """Re-scan only the folder belonging to a single media item."""
    conn = connect()
    row = conn.execute(
        "SELECT id,folder_path,library_id,title,metadata,media_type FROM media_items WHERE id=? AND folder_path IS NOT NULL",
        (media_id,),
    ).fetchone()
    if not row:
        conn.close()
        return 0
    folder = Path(row["folder_path"])
    if not folder.is_dir():
        conn.close()
        return 0
    existing_meta = json.loads(row["metadata"]) if row["metadata"] else {}
    title = row["title"]
    library_id = int(row["library_id"])
    count = _sync_versions(conn, folder, media_id, library_id, title, existing_meta)
    conn.commit()
    conn.close()
    return count



