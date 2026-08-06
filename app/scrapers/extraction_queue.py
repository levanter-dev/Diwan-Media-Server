from __future__ import annotations

import json
import logging
import threading
import time

from ..db import connect

logger = logging.getLogger(__name__)

_worker_started = False
_worker_lock = threading.Lock()
_extraction_lock = threading.Lock()
_delay_between = 4


def ensure_schema():
    conn = connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS extraction_jobs (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            adapter_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            media_type TEXT NOT NULL DEFAULT 'movie',
            status TEXT NOT NULL DEFAULT 'queued',
            servers TEXT,
            error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def enqueue(items: list[dict]) -> list[dict]:
    ensure_schema()
    conn = connect()
    jobs = []
    for item in items:
        cur = conn.execute(
            "INSERT INTO extraction_jobs(title,adapter_id,source_id,media_type) VALUES(?,?,?,?)",
            (item["title"], item["adapter_id"], item["source_id"], item.get("media_type", "movie")),
        )
        jobs.append({"id": cur.lastrowid, "status": "queued", **item})
    conn.commit()
    conn.close()
    ensure_worker()
    return jobs


def list_jobs() -> list[dict]:
    ensure_schema()
    conn = connect()
    rows = [dict(r) for r in conn.execute("SELECT * FROM extraction_jobs ORDER BY created_at DESC")]
    conn.close()
    for r in rows:
        if r.get("servers"):
            try:
                r["servers"] = json.loads(r["servers"])
            except Exception:
                pass
    return rows


def cancel_job(job_id: int) -> bool:
    conn = connect()
    conn.execute(
        "UPDATE extraction_jobs SET status='cancelled',completed_at=CURRENT_TIMESTAMP WHERE id=? AND status IN ('queued','extracting')",
        (job_id,),
    )
    changed = conn.total_changes > 0
    conn.commit()
    conn.close()
    return changed


def delete_job(job_id: int) -> bool:
    conn = connect()
    conn.execute("DELETE FROM extraction_jobs WHERE id=?", (job_id,))
    changed = conn.total_changes > 0
    conn.commit()
    conn.close()
    return changed


def ensure_worker():
    global _worker_started
    if _worker_started:
        return
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
        t = threading.Thread(target=_worker_loop, daemon=True)
        t.start()
        logger.info("Extraction worker started")


def _worker_loop():
    import app.scrapers

    extract_fn = app.scrapers.extract_servers

    while True:
        try:
            conn = connect()
            row = conn.execute(
                "SELECT * FROM extraction_jobs WHERE status='queued' ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            conn.close()
        except Exception as exc:
            logger.warning("Worker DB error: %s", exc)
            time.sleep(5)
            continue

        if not row:
            time.sleep(3)
            continue

        job = dict(row)
        job_id = job["id"]
        logger.info("Worker: job %d (%s via %s) waiting...", job_id, job["title"], job["adapter_id"])

        conn = connect()
        conn.execute("UPDATE extraction_jobs SET status='extracting' WHERE id=?", (job_id,))
        conn.commit()
        conn.close()

        time.sleep(_delay_between)

        with _extraction_lock:
            logger.info("Worker: running job %d", job_id)
            try:
                result = extract_fn(
                    job["adapter_id"], job["source_id"], job["title"],
                    job.get("media_type", "movie"),
                )
            except Exception as exc:
                result = {"error": str(exc), "servers": []}
                logger.warning("Worker: job %d error: %s", job_id, exc)

        servers_raw = result.get("servers", [])
        servers_list = []
        for s in servers_raw:
            try:
                servers_list.append(s.to_dict() if hasattr(s, "to_dict") else dict(s))
            except Exception:
                pass

        error = result.get("error")
        try:
            conn = connect()
            conn.execute(
                "UPDATE extraction_jobs SET status=?,servers=?,error=?,completed_at=CURRENT_TIMESTAMP WHERE id=?",
                ("done" if not error else "failed", json.dumps(servers_list, default=str), error, job_id),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.warning("Worker: DB update failed: %s", exc)

        logger.info("Worker: job %d done (%d servers)", job_id, len(servers_list))
        time.sleep(1)
