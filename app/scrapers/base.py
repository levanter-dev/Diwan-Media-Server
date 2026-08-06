from __future__ import annotations

from abc import ABC, abstractmethod

from .models import SourceMedia, ServerSource


class BaseAdapter(ABC):
    @property
    @abstractmethod
    def adapter_id(self) -> str:
        ...

    @property
    @abstractmethod
    def adapter_name(self) -> str:
        ...

    @abstractmethod
    def search(self, query: str, media_type: str | None = None) -> list[SourceMedia]:
        ...

    @abstractmethod
    def extract_servers(self, media: SourceMedia) -> list[ServerSource]:
        ...

    def supports_media_type(self, media_type: str | None) -> bool:
        return True
