from __future__ import annotations

import logging
from typing import Any

from .base import BaseAdapter
from .models import SourceMedia, ServerSource, SourceResult

logger = logging.getLogger(__name__)

_registry: list[BaseAdapter] = []

# Scrapers are optional — they ship as local-only files not tracked in git.
# See LOCAL_SETUP.md for how to restore them.
try:
    from .larroza import LarrozaAdapter
    from .shahid import ShahidAdapter
    from .ramoflix import RamoflixAdapter
    from .shuttletv import ShuttleTVAdapter
    from .aether import AetherAdapter
    from .soap2day import Soap2dayAdapter
    from .hdtoday import HDTodayAdapter
    _SCRAPERS_AVAILABLE = True
except ImportError:
    _SCRAPERS_AVAILABLE = False
    logger.info("Scraper adapters not found — download/redownload features will be unavailable.")


def _ensure_registry() -> None:
    if _registry:
        return
    if not _SCRAPERS_AVAILABLE:
        return
    from .larroza import LARROZA_BASE
    adapter = LarrozaAdapter()
    _registry.append(adapter)
    logger.info("Registered scraper adapter: %s (id=%s, base=%s)", adapter.adapter_name, adapter.adapter_id, LARROZA_BASE)

    from .shahid import SHAHID_BASE as _SHAHID_BASE
    adapter2 = ShahidAdapter()
    _registry.append(adapter2)
    logger.info("Registered scraper adapter: %s (id=%s, base=%s)", adapter2.adapter_name, adapter2.adapter_id, _SHAHID_BASE)

    from .ramoflix import RAMOFLIX_BASE as _RAMOFLIX_BASE
    adapter3 = RamoflixAdapter()
    _registry.append(adapter3)
    logger.info("Registered scraper adapter: %s (id=%s, base=%s)", adapter3.adapter_name, adapter3.adapter_id, _RAMOFLIX_BASE)

    from .shuttletv import SHUTTLETV_BASE as _SHUTTLETV_BASE
    adapter4 = ShuttleTVAdapter()
    _registry.append(adapter4)
    logger.info("Registered scraper adapter: %s (id=%s, base=%s)", adapter4.adapter_name, adapter4.adapter_id, _SHUTTLETV_BASE)

    from .aether import AETHER_BASE as _AETHER_BASE
    adapter5 = AetherAdapter()
    _registry.append(adapter5)
    logger.info("Registered scraper adapter: %s (id=%s, base=%s)", adapter5.adapter_name, adapter5.adapter_id, _AETHER_BASE)

    from .soap2day import SOAP2DAY_BASE as _SOAP2DAY_BASE
    adapter6 = Soap2dayAdapter()
    _registry.append(adapter6)
    logger.info("Registered scraper adapter: %s (id=%s, base=%s)", adapter6.adapter_name, adapter6.adapter_id, _SOAP2DAY_BASE)

    from .hdtoday import HDTODAY_BASE as _HDTODAY_BASE
    adapter7 = HDTodayAdapter()
    _registry.append(adapter7)
    logger.info("Registered scraper adapter: %s (id=%s, base=%s)", adapter7.adapter_name, adapter7.adapter_id, _HDTODAY_BASE)


def get_adapters() -> list[BaseAdapter]:
    _ensure_registry()
    return list(_registry)


def search_all(query: str, media_type: str | None = None) -> list[dict[str, Any]]:
    _ensure_registry()
    results: list[dict[str, Any]] = []
    for adapter in _registry:
        logger.info("--- Searching adapter '%s' (%s) for: %s ---", adapter.adapter_name, adapter.adapter_id, query)
        try:
            media_list = adapter.search(query, media_type)
            logger.info("Adapter '%s' returned %d results", adapter.adapter_name, len(media_list))
            if media_list:
                results.append({
                    "adapter_id": adapter.adapter_id,
                    "adapter_name": adapter.adapter_name,
                    "media_list": [m.to_dict() for m in media_list],
                })
        except Exception as exc:
            logger.error("Adapter '%s' search FAILED: %s", adapter.adapter_name, exc)
            results.append({
                "adapter_id": adapter.adapter_id,
                "adapter_name": adapter.adapter_name,
                "media_list": [],
                "error": str(exc),
            })
    return results


def extract_servers(adapter_id: str, source_id: str, title: str, media_type: str = "movie") -> dict[str, Any]:
    _ensure_registry()
    logger.info("--- Extracting servers via adapter '%s' for source=%s title=%s ---", adapter_id, source_id, title)
    for adapter in _registry:
        if adapter.adapter_id == adapter_id:
            media = SourceMedia(
                source_id=source_id,
                title=title,
                media_type=media_type,
            )
            servers = adapter.extract_servers(media)
            logger.info("Adapter '%s' returned %d server(s)", adapter.adapter_name, len(servers))
            return {
                "adapter_id": adapter.adapter_id,
                "adapter_name": adapter.adapter_name,
                "media": media.to_dict(),
                "servers": [s.to_dict() for s in servers],
            }
    logger.error("Adapter '%s' not found in registry", adapter_id)
    return {"error": f"Adapter '{adapter_id}' not found"}
