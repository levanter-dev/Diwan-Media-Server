from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SourceMedia:
    source_id: str
    title: str
    media_type: str = "movie"
    year: str | None = None
    thumbnail_url: str | None = None
    quality: str | None = None
    details: str | None = None
    season: int | None = None
    episode: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "source_id": self.source_id,
            "title": self.title,
            "media_type": self.media_type,
            "year": self.year,
            "thumbnail_url": self.thumbnail_url,
            "quality": self.quality,
            "details": self.details,
            "season": self.season,
            "episode": self.episode,
        }
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class ServerSource:
    server_id: str
    server_name: str
    video_url: str | None = None
    quality: str | None = None
    headers: dict[str, str] | None = None
    direct: bool = False
    language: str | None = None
    subtitles: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "server_id": self.server_id,
            "server_name": self.server_name,
            "video_url": self.video_url,
            "quality": self.quality,
            "direct": self.direct,
            "language": self.language,
            "subtitles": self.subtitles,
        }


@dataclass
class SourceResult:
    adapter_id: str
    adapter_name: str
    media_list: list[SourceMedia] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "adapter_name": self.adapter_name,
            "media_list": [m.to_dict() for m in self.media_list],
            "error": self.error,
        }
