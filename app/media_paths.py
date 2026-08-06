from __future__ import annotations

import re
from pathlib import Path


def safe_folder_name(value: str, fallback: str = "Untitled") -> str:
    """Return a portable title folder while retaining Unicode letters and numbers."""
    cleaned = re.sub(r"[_]+", " ", str(value or ""))
    cleaned = re.sub(r"[^\w\s-]+", " ", cleaned, flags=re.UNICODE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-")
    if not cleaned:
        cleaned = fallback
    # Avoid Windows device names and keep paths within practical limits.
    if cleaned.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        cleaned = f"{cleaned} Media"
    return cleaned[:120].rstrip(" .")


def media_folder(root: Path, title: str, media_type: str,
                 season_number: int | None = None, episode_number: int | None = None) -> Path:
    folder = Path(root) / safe_folder_name(title)
    if media_type in {"series", "episode"} and season_number is not None:
        folder /= str(int(season_number))
        if episode_number is not None:
            folder /= str(int(episode_number))
    return folder


def series_parts(path: Path, library_root: Path) -> tuple[Path, int, int] | None:
    """Parse Series/Season/Episode/file layouts beneath a library root."""
    try:
        relative = path.resolve().relative_to(library_root.resolve())
    except (OSError, ValueError):
        return None
    parts = relative.parts
    if len(parts) < 4:
        return None
    try:
        season_number = int(parts[-3])
        episode_number = int(parts[-2])
    except ValueError:
        return None
    if season_number < 0 or episode_number < 1:
        return None
    series_relative = Path(*parts[:-3])
    if not series_relative.parts:
        return None
    return library_root / series_relative, season_number, episode_number