from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Any

from .base import BaseAdapter
from .models import SourceMedia, ServerSource, SourceResult

logger = logging.getLogger(__name__)

_registry: list[BaseAdapter] = []

ADAPTER_TIMEOUT = int(os.environ.get("SCRAPE_ADAPTER_TIMEOUT", "20"))

# Scrapers are optional  -  they ship as local-only files not tracked in git.
# See LOCAL_SETUP.md for how to restore them.
try:
    from .ramoflix import RamoflixAdapter
    from .aether import AetherAdapter
    from .soap2day import Soap2dayAdapter
    _SCRAPERS_AVAILABLE = True
except ImportError:
    _SCRAPERS_AVAILABLE = False
    logger.info("Scraper adapters not found  -  download/redownload features will be unavailable.")


def _ensure_registry() -> None:
    if _registry:
        return
    if not _SCRAPERS_AVAILABLE:
        return
    from .ramoflix import RAMOFLIX_BASE as _RAMOFLIX_BASE
    adapter = RamoflixAdapter()
    _registry.append(adapter)
    logger.info("Registered scraper adapter: %s (id=%s, base=%s)", adapter.adapter_name, adapter.adapter_id, _RAMOFLIX_BASE)

    from .aether import AETHER_BASE as _AETHER_BASE
    adapter2 = AetherAdapter()
    _registry.append(adapter2)
    logger.info("Registered scraper adapter: %s (id=%s, base=%s)", adapter2.adapter_name, adapter2.adapter_id, _AETHER_BASE)

    from .soap2day import SOAP2DAY_BASE as _SOAP2DAY_BASE
    adapter3 = Soap2dayAdapter()
    _registry.append(adapter3)
    logger.info("Registered scraper adapter: %s (id=%s, base=%s)", adapter3.adapter_name, adapter3.adapter_id, _SOAP2DAY_BASE)


def get_adapters() -> list[BaseAdapter]:
    _ensure_registry()
    return list(_registry)


def _search_one(adapter: BaseAdapter, query: str, media_type: str | None) -> dict[str, Any]:
    logger.info("--- Searching adapter '%s' (%s) for: %s ---", adapter.adapter_name, adapter.adapter_id, query)
    try:
        media_list = adapter.search(query, media_type)
        logger.info("Adapter '%s' returned %d results", adapter.adapter_name, len(media_list))
        return {
            "adapter_id": adapter.adapter_id,
            "adapter_name": adapter.adapter_name,
            "media_list": [m.to_dict() for m in media_list] if media_list else [],
        }
    except Exception as exc:
        logger.error("Adapter '%s' search FAILED: %s", adapter.adapter_name, exc)
        return {
            "adapter_id": adapter.adapter_id,
            "adapter_name": adapter.adapter_name,
            "media_list": [],
            "error": str(exc),
        }


def search_all(query: str, media_type: str | None = None, parallel: bool = True) -> list[dict[str, Any]]:
    _ensure_registry()
    if not parallel or len(_registry) <= 1:
        results: list[dict[str, Any]] = []
        for adapter in _registry:
            results.append(_search_one(adapter, query, media_type))
        return results

    logger.info("=== Parallel search on %d adapters (timeout=%ss) ===", len(_registry), ADAPTER_TIMEOUT)
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=len(_registry), thread_name_prefix="scrape-search") as executor:
        future_map = {
            executor.submit(_search_one, adapter, query, media_type): adapter
            for adapter in _registry
        }
        for future, adapter in future_map.items():
            try:
                result = future.result(timeout=ADAPTER_TIMEOUT)
                results.append(result)
            except FutureTimeoutError:
                logger.error("Adapter '%s' TIMED OUT after %ss", adapter.adapter_name, ADAPTER_TIMEOUT)
                results.append({
                    "adapter_id": adapter.adapter_id,
                    "adapter_name": adapter.adapter_name,
                    "media_list": [],
                    "error": f"timed out after {ADAPTER_TIMEOUT}s",
                })
            except Exception as exc:
                logger.error("Adapter '%s' future failed: %s", adapter.adapter_name, exc)
                results.append({
                    "adapter_id": adapter.adapter_id,
                    "adapter_name": adapter.adapter_name,
                    "media_list": [],
                    "error": str(exc),
                })

    _order = {a.adapter_id: i for i, a in enumerate(_registry)}
    results.sort(key=lambda r: _order.get(r.get("adapter_id", ""), 999))
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
