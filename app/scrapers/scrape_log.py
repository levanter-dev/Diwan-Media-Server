from __future__ import annotations

import logging
import threading

_SCRAPER_LOGGER_NAMES = [
    "app.scrapers",
]
_lock = threading.Lock()


class _CaptureHandler(logging.Handler):
    def __init__(self, level: int) -> None:
        super().__init__(level)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self.format(record))


class CaptureContext:
    """Context manager that captures scraper log messages.

    Usage::

        with CaptureContext() as cap:
            ...  # do scraping work
            return {"result": result, "logs": cap.logs}
    """

    def __init__(self, level: int = logging.INFO) -> None:
        self._level = level
        self._logs: list[str] = []
        self._handler: _CaptureHandler | None = None

    def __enter__(self) -> CaptureContext:
        handler = _CaptureHandler(self._level)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        with _lock:
            for name in _SCRAPER_LOGGER_NAMES:
                logging.getLogger(name).addHandler(handler)
        self._handler = handler
        logging.getLogger("app.scrapers").info("Scraper trace started")
        return self

    def __exit__(self, *args: object) -> None:
        handler = self._handler
        self._handler = None
        if handler is None:
            return
        with _lock:
            for name in _SCRAPER_LOGGER_NAMES:
                try:
                    logging.getLogger(name).removeHandler(handler)
                except ValueError:
                    pass
        self._logs = handler.records

    @property
    def logs(self) -> list[str]:
        return list(self._logs)
