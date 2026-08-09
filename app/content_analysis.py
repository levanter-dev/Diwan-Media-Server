from __future__ import annotations

import importlib.util
import gc
import json
import math
import os
import queue
import sqlite3
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterable

from .config import DATA_DIR, FFMPEG_PATH
from .db import connect

MODEL_VERSION = "diwan-content-v8"
ANATOMY_MODEL_VERSION = "nudenet-320n-3.4.2"
CONTEXT_MODEL_VERSION = "openclip-vit-b-32-laion2b"
DEFAULT_MODEL_KEY = "nudenet_openclip"
ALL_MODELS_KEY = "all_models"
MULTI_MODELS_PREFIX = "multi:"
MODEL_OPTIONS: dict[str, dict[str, str]] = {
    "nudenet_openclip": {
        "label": "NudeNet + OpenCLIP",
        "description": "Best coverage. NudeNet detects exposed body parts; OpenCLIP adds scene context such as kissing, sexual activity, and revealing attire.",
    },
    "nudenet_only": {
        "label": "NudeNet only",
        "description": "More predictable anatomy detector. Ignores context-only categories, so it is less likely to flag normal romantic or swimwear scenes.",
    },
    "nudenet_strict": {
        "label": "NudeNet strict private-parts",
        "description": "Highest precision. General nudity only means exposed genitals, anus, or buttocks; breasts stay in the separate toplessness categories.",
    },
    "openclip_only": {
        "label": "OpenCLIP prompts only",
        "description": "Experimental scene-text matching. Useful for markers, but not recommended for automatic skipping because wording can change results.",
    },
    "gantman_nsfw": {
        "label": "GantMan NSFW model",
        "description": "Optional classifier with porn, sexy, hentai, neutral, and drawings labels. Requires nsfw_detector/TensorFlow and NSFW_MODEL_PATH.",
    },
    "haramblur_style": {
        "label": "HaramBlur-style NSFWJS",
        "description": "Uses the same NSFWJS/GantMan-style classifier family that HaramBlur documents, but without HaramBlur's browser-only Human face detector.",
    },
}

CATEGORY_INFO: dict[str, dict[str, str]] = {
    "sexual_activity": {"label": "Sexual activity", "color": "#ef4444", "detector": "context"},
    "female_toplessness": {"label": "Female toplessness", "color": "#f97316", "detector": "anatomy"},
    "male_toplessness": {"label": "Male toplessness", "color": "#eab308", "detector": "anatomy"},
    "kissing": {"label": "Kissing", "color": "#ec4899", "detector": "context"},
    "revealing_attire": {"label": "Revealing attire / swimwear", "color": "#a855f7", "detector": "context"},
    "nudity": {"label": "General nudity", "color": "#dc2626", "detector": "anatomy"},
}

DEFAULT_POLICY = {
    "sexual_activity": "skip",
    "female_toplessness": "skip",
    "male_toplessness": "skip",
    "kissing": "marker",
    "revealing_attire": "marker",
    "nudity": "skip",
}
VALID_ACTIONS = {"off", "marker", "warn", "skip"}
DEFAULT_CONFIRMATION = {
    "min_models": 1,
    "high_confidence": 0.95,
    "window_seconds": 3.0,
    "require_confirmation_for_skip": True,
}
SENSITIVITY_THRESHOLDS = {
    # These thresholds deliberately favour precision. A false automatic skip is
    # much more disruptive than a missed marker, so skips are corroborated again
    # in _merge_hits before they become playback segments.
    "low": {"anatomy": 0.82, "context": 0.84},
    "balanced": {"anatomy": 0.72, "context": 0.76},
    "high": {"anatomy": 0.62, "context": 0.68},
}
BLOCKING_CATEGORIES = {"sexual_activity", "female_toplessness", "male_toplessness", "nudity"}
BLOCKING_SCENE_PADDING_SECONDS = 1.0
BLOCKING_THRESHOLD_FACTORS = {"anatomy": 1.08, "context": 1.06}

FEMALE_TOPLESS_CLASSES = {"FEMALE_BREAST_EXPOSED"}
MALE_TOPLESS_CLASSES = {"MALE_BREAST_EXPOSED"}
NUDITY_CLASSES = {
    "FEMALE_BREAST_EXPOSED", "FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED", "BUTTOCKS_EXPOSED",
}

CONTEXT_PROMPTS = {
    "female_toplessness": (
        "a topless woman with clearly visible bare breasts",
        "a woman whose chest is fully covered by clothing",
    ),
    "male_toplessness": (
        "a shirtless or topless man with a clearly visible bare chest",
        "a man whose chest is fully covered by clothing",
    ),
    "nudity": (
        "a movie scene with clearly visible nudity, bare breasts, buttocks, or genitals",
        "a movie scene with fully clothed people and no visible nudity",
    ),
    "sexual_activity": (
        "an intimate sexual activity scene between adults",
        "an ordinary non-sexual scene with people",
    ),
    "kissing": (
        "two adults kissing romantically",
        "people together but not kissing",
    ),
    "revealing_attire": (
        "adults wearing revealing swimwear lingerie or nightclub clothing",
        "people wearing ordinary fully covering clothing",
    ),
}

_model_lock = threading.Lock()
_nude_detector: Any | None = None
_context_classifier: Any | None = None
_nsfw_classifier: Any | None = None
_worker_lock = threading.Lock()
_worker_thread: threading.Thread | None = None
_worker_recovered = False
_job_queue: queue.Queue[int] = queue.Queue()
_queued_ids: set[int] = set()
_cancel_events: dict[int, threading.Event] = {}


class AnalysisError(RuntimeError):
    pass


class AnalysisCancelled(AnalysisError):
    pass


def category_definitions() -> list[dict[str, str]]:
    return [{"key": key, **value} for key, value in CATEGORY_INFO.items()]


def model_definitions() -> list[dict[str, Any]]:
    nude_available = importlib.util.find_spec("nudenet") is not None and importlib.util.find_spec("onnxruntime") is not None
    clip_available = importlib.util.find_spec("open_clip") is not None and importlib.util.find_spec("torch") is not None
    nsfw_available = importlib.util.find_spec("nsfw_detector") is not None and bool(os.getenv("NSFW_MODEL_PATH", "").strip())
    result: list[dict[str, Any]] = []
    for key, value in MODEL_OPTIONS.items():
        available = True
        reason = ""
        if key == "nudenet_openclip" and not (nude_available and clip_available):
            available = False
            reason = "Install NudeNet/ONNX Runtime and OpenCLIP/Torch to enable this combined option."
        elif key in {"nudenet_only", "nudenet_strict"} and not nude_available:
            available = False
            reason = "Install NudeNet and ONNX Runtime to enable this option."
        elif key == "openclip_only" and not clip_available:
            available = False
            reason = "Install OpenCLIP and Torch to enable this option."
        elif key in {"gantman_nsfw", "haramblur_style"} and not nsfw_available:
            available = False
            reason = "Install nsfw_detector/TensorFlow and set NSFW_MODEL_PATH to enable this option."
        result.append({"key": key, **value, "available": available, "unavailable_reason": reason})
    return result


def _normalized_model_key(value: str | None) -> str:
    key = str(value or DEFAULT_MODEL_KEY).strip()
    return key if key in MODEL_OPTIONS else DEFAULT_MODEL_KEY


def _normalized_run_model_key(value: str | None) -> str:
    key = str(value or DEFAULT_MODEL_KEY).strip()
    return key if key == ALL_MODELS_KEY or key in MODEL_OPTIONS else DEFAULT_MODEL_KEY


def _normalized_run_model_keys(values: list[str] | None) -> list[str]:
    available = set(_available_run_models())
    keys: list[str] = []
    for value in values or []:
        key = str(value or "").strip()
        if key in MODEL_OPTIONS and key in available and key not in keys:
            keys.append(key)
    return keys


def _run_models_version_key(keys: list[str]) -> str:
    available = _available_run_models()
    if keys and set(keys) == set(available):
        return ALL_MODELS_KEY
    if len(keys) == 1:
        return keys[0]
    return MULTI_MODELS_PREFIX + ",".join(keys)


def _run_models_from_version(value: str | None) -> tuple[str, list[str]]:
    raw = str(value or "")
    key = raw.split(":", 1)[1] if ":" in raw else raw
    if key == ALL_MODELS_KEY:
        models = _available_run_models()
        return ALL_MODELS_KEY, models
    if key.startswith(MULTI_MODELS_PREFIX):
        models = _normalized_run_model_keys(key[len(MULTI_MODELS_PREFIX):].split(","))
        return _run_models_version_key(models), models
    single = _normalized_run_model_key(key)
    return single, [single] if single in _available_run_models() else []


def _model_key_from_version(value: str | None) -> str:
    raw = str(value or "")
    key = raw.split(":", 1)[1] if ":" in raw else raw
    return key if key in MODEL_OPTIONS or key == ALL_MODELS_KEY else DEFAULT_MODEL_KEY


def _analysis_model_version(model_key: str) -> str:
    return f"{MODEL_VERSION}:{model_key if str(model_key).startswith(MULTI_MODELS_PREFIX) else _normalized_run_model_key(model_key)}"


def _model_label(model_key: str) -> str:
    return MODEL_OPTIONS.get(model_key, {}).get("label", model_key)


def _confirmation_settings(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = raw or {}
    try:
        min_models = max(1, min(6, int(raw.get("min_models", DEFAULT_CONFIRMATION["min_models"]))))
    except (TypeError, ValueError):
        min_models = DEFAULT_CONFIRMATION["min_models"]
    try:
        high_confidence = max(0.5, min(0.999, float(raw.get("high_confidence", DEFAULT_CONFIRMATION["high_confidence"]))))
    except (TypeError, ValueError):
        high_confidence = DEFAULT_CONFIRMATION["high_confidence"]
    try:
        window_seconds = max(0.5, min(30.0, float(raw.get("window_seconds", DEFAULT_CONFIRMATION["window_seconds"]))))
    except (TypeError, ValueError):
        window_seconds = DEFAULT_CONFIRMATION["window_seconds"]
    return {
        "min_models": min_models,
        "high_confidence": high_confidence,
        "window_seconds": window_seconds,
        "require_confirmation_for_skip": bool(raw.get("require_confirmation_for_skip", DEFAULT_CONFIRMATION["require_confirmation_for_skip"])),
    }


def _setting(conn, key: str, default: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def get_filter_settings() -> dict[str, Any]:
    conn = connect()
    try:
        try:
            policy = json.loads(_setting(conn, "content_filter_policy", json.dumps(DEFAULT_POLICY)))
        except (TypeError, json.JSONDecodeError):
            policy = dict(DEFAULT_POLICY)
        normalized = {key: policy.get(key, DEFAULT_POLICY[key]) for key in CATEGORY_INFO}
        for key, action in list(normalized.items()):
            if action not in VALID_ACTIONS:
                normalized[key] = DEFAULT_POLICY[key]
        sensitivity = _setting(conn, "content_filter_sensitivity", "balanced")
        if sensitivity not in SENSITIVITY_THRESHOLDS:
            sensitivity = "balanced"
        auto_analyze = _setting(conn, "content_filter_auto_analyze", "0") == "1"
        model_key = _normalized_model_key(_setting(conn, "content_filter_model", DEFAULT_MODEL_KEY))
        try:
            confirmation = _confirmation_settings(json.loads(_setting(conn, "content_filter_confirmation", "{}")))
        except (TypeError, json.JSONDecodeError):
            confirmation = _confirmation_settings()
        try:
            controls_timeout_seconds = max(1, min(30, int(_setting(conn, "player_controls_timeout_seconds", "3"))))
        except ValueError:
            controls_timeout_seconds = 3
        try:
            revision = int(_setting(conn, "content_filter_revision", "1"))
        except ValueError:
            revision = 1
        return {"policy": normalized, "sensitivity": sensitivity, "auto_analyze": auto_analyze,
                "model_key": model_key, "models": model_definitions(),
                "confirmation": confirmation,
                "controls_timeout_seconds": controls_timeout_seconds,
                "revision": revision, "categories": category_definitions()}
    finally:
        conn.close()


def save_filter_settings(policy: dict[str, str], sensitivity: str, auto_analyze: bool,
                         model_key: str = DEFAULT_MODEL_KEY,
                         confirmation: dict[str, Any] | None = None,
                         controls_timeout_seconds: int = 3) -> dict[str, Any]:
    if sensitivity not in SENSITIVITY_THRESHOLDS:
        raise AnalysisError("Unsupported sensitivity")
    model_key = _normalized_model_key(model_key)
    confirmation = _confirmation_settings(confirmation)
    normalized: dict[str, str] = {}
    controls_timeout_seconds = max(1, min(30, int(controls_timeout_seconds)))
    for category in CATEGORY_INFO:
        action = str(policy.get(category, DEFAULT_POLICY[category]))
        if action not in VALID_ACTIONS:
            raise AnalysisError(f"Unsupported action for {category}")
        normalized[category] = action
    conn = connect()
    try:
        try:
            previous_policy = json.loads(_setting(conn, "content_filter_policy", json.dumps(DEFAULT_POLICY)))
        except (TypeError, json.JSONDecodeError):
            previous_policy = dict(DEFAULT_POLICY)
        previous_policy = {key: previous_policy.get(key, DEFAULT_POLICY[key]) for key in CATEGORY_INFO}
        previous_sensitivity = _setting(conn, "content_filter_sensitivity", "balanced")
        previous_model = _normalized_model_key(_setting(conn, "content_filter_model", DEFAULT_MODEL_KEY))
        try:
            previous_confirmation = _confirmation_settings(json.loads(_setting(conn, "content_filter_confirmation", "{}")))
        except (TypeError, json.JSONDecodeError):
            previous_confirmation = _confirmation_settings()
        if (previous_policy != normalized or previous_sensitivity != sensitivity or
                previous_model != model_key or previous_confirmation != confirmation):
            try:
                revision = int(_setting(conn, "content_filter_revision", "1")) + 1
            except ValueError:
                revision = 2
            conn.execute("INSERT INTO settings(key,value) VALUES('content_filter_revision',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(revision),))
        conn.execute("INSERT INTO settings(key,value) VALUES('content_filter_policy',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (json.dumps(normalized, separators=(",", ":")),))
        conn.execute("INSERT INTO settings(key,value) VALUES('content_filter_sensitivity',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (sensitivity,))
        conn.execute("INSERT INTO settings(key,value) VALUES('content_filter_auto_analyze',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     ("1" if auto_analyze else "0",))
        conn.execute("INSERT INTO settings(key,value) VALUES('content_filter_model',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (model_key,))
        conn.execute("INSERT INTO settings(key,value) VALUES('content_filter_confirmation',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (json.dumps(confirmation, separators=(",", ":")),))
        conn.execute("INSERT INTO settings(key,value) VALUES('player_controls_timeout_seconds',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (str(controls_timeout_seconds),))
        conn.commit()
    finally:
        conn.close()
    return get_filter_settings()


def save_content_segment_override(media_id: int, segment_id: int, enabled: bool) -> dict[str, Any]:
    conn = connect()
    try:
        segment = conn.execute(
            "SELECT 1 FROM content_segments WHERE id=? AND media_id=?", (segment_id, media_id)
        ).fetchone()
        if not segment:
            raise AnalysisError("Detected scene not found")
        conn.execute(
            """INSERT INTO content_segment_overrides(segment_id,enabled) VALUES(?,?)
               ON CONFLICT(segment_id) DO UPDATE SET
               enabled=excluded.enabled,updated_at=CURRENT_TIMESTAMP""",
            (segment_id, 1 if enabled else 0),
        )
        conn.commit()
    finally:
        conn.close()
    return analysis_payload(media_id)


def save_content_range_review(media_id: int, category: str, start_ms: int, end_ms: int,
                              enabled: bool, note: str | None = None) -> dict[str, Any]:
    if category not in CATEGORY_INFO:
        raise AnalysisError("Unsupported category")
    start_ms = max(0, int(start_ms))
    end_ms = max(start_ms + 1, int(end_ms))
    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM media_items WHERE id=?", (media_id,)).fetchone():
            raise AnalysisError("Media not found")
        conn.execute(
            """INSERT INTO content_segment_reviews(media_id,category,start_ms,end_ms,enabled,note)
               VALUES(?,?,?,?,?,?)""",
            (media_id, category, start_ms, end_ms, 1 if enabled else 0, (note or "").strip() or None),
        )
        conn.commit()
    finally:
        conn.close()
    return analysis_payload(media_id)


def _segment_model_key(segment: dict[str, Any]) -> str:
    return _model_key_from_version(segment.get("model_version"))


def _merge_confirmed_segments(raw_segments: list[dict[str, Any]], settings: dict[str, Any],
                              reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    confirmation = settings.get("confirmation") or _confirmation_settings()
    window_ms = int(float(confirmation["window_seconds"]) * 1000)
    min_models = int(confirmation["min_models"])
    high_confidence = float(confirmation["high_confidence"])
    result: list[dict[str, Any]] = []
    by_category: dict[str, list[dict[str, Any]]] = {}
    for segment in raw_segments:
        by_category.setdefault(str(segment["category"]), []).append(segment)
    synthetic_id = -1
    for category, segments in by_category.items():
        segments.sort(key=lambda item: (int(item["start_ms"]), int(item["end_ms"])))
        groups: list[list[dict[str, Any]]] = []
        for segment in segments:
            center = (int(segment["start_ms"]) + int(segment["end_ms"])) // 2
            if groups:
                prev = groups[-1]
                prev_center = max((int(s["start_ms"]) + int(s["end_ms"])) // 2 for s in prev)
                if center - prev_center <= window_ms:
                    prev.append(segment)
                    continue
            groups.append([segment])
        for group in groups:
            models = sorted({_segment_model_key(segment) for segment in group})
            max_conf = max(float(segment.get("confidence") or 0.0) for segment in group)
            confirmed = len(models) >= min_models or max_conf >= high_confidence
            action = (settings.get("policy") or {}).get(category, DEFAULT_POLICY.get(category))
            if not confirmed and confirmation.get("require_confirmation_for_skip") and action != "skip":
                confirmed = True
            if not confirmed:
                continue
            start_ms = min(int(segment["start_ms"]) for segment in group)
            end_ms = max(int(segment["end_ms"]) for segment in group)
            enabled = True
            for review in reviews:
                if review["category"] != category:
                    continue
                overlaps = int(review["start_ms"]) < end_ms and int(review["end_ms"]) > start_ms
                if overlaps:
                    enabled = bool(review["enabled"])
            result.append({
                "id": synthetic_id,
                "category": category,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "confidence": round(sum(float(segment.get("confidence") or 0.0) for segment in group) / len(group), 4),
                "detector": "+".join(sorted({str(segment.get("detector") or "") for segment in group if segment.get("detector")})),
                "model_version": "confirmed:" + ",".join(models),
                "models": models,
                "votes": len(models),
                "source_segments": [int(segment["id"]) for segment in group if segment.get("id") is not None],
                "enabled": enabled,
                "confirmed": True,
            })
            synthetic_id -= 1
    for review in reviews:
        if bool(review["enabled"]) and not any(
            item["category"] == review["category"] and int(review["start_ms"]) < item["end_ms"] and int(review["end_ms"]) > item["start_ms"]
            for item in result
        ):
            result.append({
                "id": synthetic_id,
                "category": review["category"],
                "start_ms": int(review["start_ms"]),
                "end_ms": int(review["end_ms"]),
                "confidence": 1.0,
                "detector": "manual",
                "model_version": "manual-review",
                "models": ["manual"],
                "votes": max(min_models, 1),
                "source_segments": [],
                "enabled": True,
                "confirmed": True,
            })
            synthetic_id -= 1
    result.sort(key=lambda item: (item["start_ms"], item["category"]))
    return result


def save_media_filter_overrides(media_id: int, enabled: dict[str, bool]) -> dict[str, Any]:
    invalid = [category for category in enabled if category not in CATEGORY_INFO]
    if invalid:
        raise AnalysisError("Unsupported categories: " + ", ".join(invalid))
    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM media_items WHERE id=?", (media_id,)).fetchone():
            raise AnalysisError("Media not found")
        for category, is_enabled in enabled.items():
            conn.execute(
                """INSERT INTO media_filter_overrides(media_id,category,enabled)
                   VALUES(?,?,?) ON CONFLICT(media_id,category) DO UPDATE SET
                   enabled=excluded.enabled,updated_at=CURRENT_TIMESTAMP""",
                (media_id, category, 1 if is_enabled else 0),
            )
        conn.commit()
    finally:
        conn.close()
    return analysis_payload(media_id)


def media_filter_model(media_id: int, settings: dict[str, Any] | None = None) -> str:
    settings = settings or get_filter_settings()
    conn = connect()
    try:
        row = conn.execute("SELECT model_key FROM media_filter_model_overrides WHERE media_id=?", (media_id,)).fetchone()
        return _normalized_model_key(row["model_key"] if row else settings.get("model_key"))
    finally:
        conn.close()


def _media_completed_model_keys(conn: sqlite3.Connection, media_id: int) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT model_version FROM content_segments WHERE media_id=?",
        (media_id,),
    ).fetchall()
    keys = {_segment_model_key(dict(row)) for row in rows}
    job = conn.execute(
        "SELECT model_version FROM content_analysis_jobs WHERE media_id=? AND status='completed'",
        (media_id,),
    ).fetchone()
    if job:
        _run_key, run_models = _run_models_from_version(job["model_version"])
        keys.update(run_models)
    return keys


def save_media_filter_model(media_id: int, model_key: str | None) -> dict[str, Any]:
    normalized = _normalized_model_key(model_key)
    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM media_items WHERE id=?", (media_id,)).fetchone():
            raise AnalysisError("Media not found")
        if model_key:
            completed_models = _media_completed_model_keys(conn, media_id)
            if normalized not in completed_models:
                raise AnalysisError("Run this model on the media before setting it active")
        if not model_key or normalized == get_filter_settings()["model_key"]:
            conn.execute("DELETE FROM media_filter_model_overrides WHERE media_id=?", (media_id,))
        else:
            conn.execute(
                """INSERT INTO media_filter_model_overrides(media_id,model_key)
                   VALUES(?,?) ON CONFLICT(media_id) DO UPDATE SET
                   model_key=excluded.model_key,updated_at=CURRENT_TIMESTAMP""",
                (media_id, normalized),
            )
        conn.commit()
    finally:
        conn.close()
    return analysis_payload(media_id)


def runtime_status() -> dict[str, Any]:
    nude_installed = importlib.util.find_spec("nudenet") is not None
    clip_installed = importlib.util.find_spec("open_clip") is not None and importlib.util.find_spec("torch") is not None
    cuda = False
    torch_version = None
    if clip_installed:
        try:
            import torch
            cuda = bool(torch.cuda.is_available())
            torch_version = torch.__version__
        except Exception:
            pass
    providers: list[str] = []
    if importlib.util.find_spec("onnxruntime") is not None:
        try:
            import onnxruntime as ort
            providers = list(ort.get_available_providers())
        except Exception:
            pass
    return {
        "nudenet_installed": nude_installed,
        "context_model_installed": clip_installed,
        "cuda_available": cuda,
        "torch_version": torch_version,
        "onnx_providers": providers,
        "anatomy_model_loaded": _nude_detector is not None,
        "context_model_loaded": _context_classifier is not None,
        "model_version": MODEL_VERSION,
        "models": model_definitions(),
    }


def _update_job(job_id: int, **values: Any) -> None:
    allowed = {"status", "progress", "message", "model_version", "error", "completed_at", "checkpoint_seconds"}
    pairs = [(key, value) for key, value in values.items() if key in allowed]
    if not pairs:
        return
    assignments = ",".join(f"{key}=?" for key, _value in pairs) + ",updated_at=CURRENT_TIMESTAMP"
    conn = connect()
    try:
        conn.execute(f"UPDATE content_analysis_jobs SET {assignments} WHERE id=?",
                     tuple(value for _key, value in pairs) + (job_id,))
        conn.commit()
    finally:
        conn.close()


def _job_row(media_id: int) -> dict[str, Any] | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM content_analysis_jobs WHERE media_id=?", (media_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_analysis_jobs() -> list[dict[str, Any]]:
    ensure_worker()
    conn = connect()
    try:
        rows = [dict(row) for row in conn.execute(
            """SELECT content_analysis_jobs.*,media_items.title,media_items.path,media_items.duration
               FROM content_analysis_jobs JOIN media_items ON media_items.id=content_analysis_jobs.media_id
               ORDER BY content_analysis_jobs.created_at DESC"""
        )]
    finally:
        conn.close()
    for row in rows:
        try:
            row["categories"] = json.loads(row.get("categories") or "[]")
        except json.JSONDecodeError:
            row["categories"] = []
        _run_key, run_models = _run_models_from_version(row.get("model_version"))
        row["model_keys"] = run_models
        row["model_labels"] = [_model_label(key) for key in run_models]
    return rows


def analysis_payload(media_id: int) -> dict[str, Any]:
    ensure_worker()
    conn = connect()
    try:
        media = conn.execute("SELECT id,title,duration,file_deleted FROM media_items WHERE id=?", (media_id,)).fetchone()
        if not media:
            raise AnalysisError("Media not found")
        job = conn.execute("SELECT * FROM content_analysis_jobs WHERE media_id=?", (media_id,)).fetchone()
        segments = [dict(row) for row in conn.execute(
            """SELECT content_segments.*,COALESCE(content_segment_overrides.enabled,1) AS enabled
               FROM content_segments LEFT JOIN content_segment_overrides
                 ON content_segment_overrides.segment_id=content_segments.id
               WHERE content_segments.media_id=? ORDER BY content_segments.start_ms,content_segments.category""",
            (media_id,),
        )]
        reviews = [dict(row) for row in conn.execute(
            "SELECT * FROM content_segment_reviews WHERE media_id=? ORDER BY start_ms,category", (media_id,)
        )]
        overrides = {
            row["category"]: bool(row["enabled"])
            for row in conn.execute(
                "SELECT category,enabled FROM media_filter_overrides WHERE media_id=?", (media_id,)
            )
        }
    finally:
        conn.close()
    job_dict = dict(job) if job else None
    if job_dict:
        try:
            job_dict["categories"] = json.loads(job_dict.get("categories") or "[]")
        except json.JSONDecodeError:
            job_dict["categories"] = []
    settings = get_filter_settings()
    model_key = media_filter_model(media_id, settings)
    base_policy = dict(settings["policy"])
    media_filter_enabled = {category: overrides.get(category, True) for category in CATEGORY_INFO}
    settings["base_policy"] = base_policy
    settings["policy"] = {
        category: action if media_filter_enabled[category] else "off"
        for category, action in base_policy.items()
    }
    active = bool(job_dict and job_dict.get("status") in {"queued", "running", "cancel_requested"})
    settings_match = bool(job_dict and
                          int(job_dict.get("settings_revision") or 0) == int(settings["revision"]))
    expected_model_version = _analysis_model_version(model_key)
    completed_run_key, completed_run_models = _run_models_from_version(job_dict.get("model_version") if job_dict else None)
    current_ready = bool(job_dict and job_dict.get("status") == "completed" and
                         settings_match and (
                             job_dict.get("model_version") == expected_model_version or
                             completed_run_key == ALL_MODELS_KEY or
                             model_key in completed_run_models
                         ))
    fallback_ready = bool(active and segments and settings_match)
    ready = current_ready or fallback_ready
    stale = bool(job_dict and job_dict.get("status") == "completed" and not ready)
    if active:
        filter_state = "processing"
    elif ready:
        filter_state = "ready"
    elif stale:
        filter_state = "needs_refilter"
    elif job_dict and job_dict.get("status") == "failed":
        filter_state = "failed"
    else:
        filter_state = "not_filtered"
    model_comparison: dict[str, dict[str, Any]] = {}
    for segment in segments:
        key = _segment_model_key(segment)
        bucket = model_comparison.setdefault(key, {"model_key": key, "segments": [], "count": 0})
        bucket["segments"].append(segment)
        bucket["count"] += 1
    completed_model_keys = set(model_comparison)
    if job_dict and job_dict.get("status") == "completed" and settings_match:
        completed_model_keys.update(completed_run_models)
    active_model_segments = [segment for segment in segments if _segment_model_key(segment) == model_key]
    merge_settings = settings
    if active_model_segments:
        merge_settings = dict(settings)
        active_confirmation = dict(settings.get("confirmation") or _confirmation_settings())
        active_confirmation["min_models"] = 1
        merge_settings["confirmation"] = active_confirmation
    confirmed_segments = _merge_confirmed_segments(active_model_segments if active_model_segments else segments, merge_settings, reviews)
    return {"media": dict(media), "job": job_dict, "segments": confirmed_segments,
            "raw_segments": segments, "model_comparison": list(model_comparison.values()),
            "reviews": reviews,
            "settings": settings, "runtime": runtime_status(), "ready": ready,
            "stale": stale, "filter_state": filter_state,
            "model_key": model_key, "global_model_key": settings["model_key"],
            "completed_model_keys": sorted(completed_model_keys),
            "media_model_overridden": model_key != settings["model_key"],
            "media_filter_enabled": media_filter_enabled}


def enqueue_analysis(media_id: int, categories: list[str] | None = None,
                     model_key: str | None = None,
                     model_keys: list[str] | None = None,
                     sample_interval: float = 1.0) -> dict[str, Any]:
    selected = list(dict.fromkeys(categories or list(CATEGORY_INFO)))
    invalid = [category for category in selected if category not in CATEGORY_INFO]
    if invalid:
        raise AnalysisError("Unsupported categories: " + ", ".join(invalid))
    if not selected:
        raise AnalysisError("Select at least one analysis category")
    if sample_interval < 0.5 or sample_interval > 10:
        raise AnalysisError("Sample interval must be between 0.5 and 10 seconds")
    settings = get_filter_settings()
    selected_models = _normalized_run_model_keys(model_keys)
    if selected_models:
        selected_model = _run_models_version_key(selected_models)
    else:
        selected_model = _normalized_run_model_key(model_key) if model_key else media_filter_model(media_id, settings)
        selected_models = _available_run_models() if selected_model == ALL_MODELS_KEY else [selected_model]
    if any(settings["policy"].get(category) == "skip" for category in selected):
        sample_interval = min(sample_interval, 1.0)
    conn = connect()
    try:
        media = conn.execute("SELECT id,path,file_deleted FROM media_items WHERE id=?", (media_id,)).fetchone()
        if not media:
            raise AnalysisError("Media not found")
        if media["file_deleted"] or not Path(media["path"]).is_file():
            raise AnalysisError("The media file is not available")
        existing = conn.execute("SELECT id,status FROM content_analysis_jobs WHERE media_id=?", (media_id,)).fetchone()
        settings_revision = int(_setting(conn, "content_filter_revision", "1"))
        if existing and existing["status"] in {"queued", "running", "cancel_requested"}:
            return analysis_payload(media_id)
        if not selected_models:
            raise AnalysisError("No available analysis models are installed")
        analysis_version = _analysis_model_version(selected_model)
        conn.execute(
            """INSERT INTO content_analysis_jobs(media_id,status,progress,message,categories,sample_interval,model_version,settings_revision,error,completed_at)
               VALUES(?,?,?,?,?,?,?,?,?,NULL)
               ON CONFLICT(media_id) DO UPDATE SET status=excluded.status,progress=excluded.progress,
                 message=excluded.message,categories=excluded.categories,sample_interval=excluded.sample_interval,
                 model_version=excluded.model_version,settings_revision=excluded.settings_revision,checkpoint_seconds=0,error=NULL,completed_at=NULL,updated_at=CURRENT_TIMESTAMP""",
            (media_id, "queued", 0.0, "Waiting for the analyzer", json.dumps(selected), float(sample_interval), analysis_version, settings_revision, None),
        )
        row = conn.execute("SELECT id FROM content_analysis_jobs WHERE media_id=?", (media_id,)).fetchone()
        job_id = int(row["id"])
        conn.execute("DELETE FROM content_analysis_hits WHERE job_id=?", (job_id,))
        conn.commit()
    finally:
        conn.close()
    _queue_job(job_id)
    ensure_worker()
    return analysis_payload(media_id)


def cancel_analysis(media_id: int) -> dict[str, Any]:
    job = _job_row(media_id)
    if not job or job["status"] not in {"queued", "running", "cancel_requested"}:
        raise AnalysisError("No active analysis job")
    event = _cancel_events.setdefault(int(job["id"]), threading.Event())
    event.set()
    _update_job(int(job["id"]), status="cancel_requested", message="Stopping analysis...")
    return analysis_payload(media_id)


def clear_analysis(media_id: int) -> None:
    job = _job_row(media_id)
    if job and job["status"] in {"queued", "running", "cancel_requested"}:
        raise AnalysisError("Stop the active analysis before clearing it")
    conn = connect()
    try:
        conn.execute("DELETE FROM content_segments WHERE media_id=?", (media_id,))
        conn.execute("DELETE FROM content_analysis_jobs WHERE media_id=?", (media_id,))
        conn.commit()
    finally:
        conn.close()


def _queue_job(job_id: int) -> None:
    with _worker_lock:
        if job_id in _queued_ids:
            return
        _queued_ids.add(job_id)
        _job_queue.put(job_id)


def ensure_worker() -> None:
    global _worker_thread, _worker_recovered
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        conn = connect()
        try:
            if not _worker_recovered:
                conn.execute("UPDATE content_analysis_jobs SET status='queued',message='Waiting for the analyzer' WHERE status IN ('running','cancel_requested')")
                conn.commit()
                _worker_recovered = True
            queued = [int(row["id"]) for row in conn.execute(
                "SELECT id FROM content_analysis_jobs WHERE status='queued'")]
        finally:
            conn.close()
        for job_id in queued:
            if job_id not in _queued_ids:
                _queued_ids.add(job_id)
                _job_queue.put(job_id)
        _worker_thread = threading.Thread(target=_worker_loop, name="content-analysis", daemon=True)
        _worker_thread.start()


def _worker_loop() -> None:
    while True:
        job_id = _job_queue.get()
        try:
            try:
                _run_job(job_id)
            except Exception as exc:
                _update_job(job_id, status="failed", message="Analysis failed", error=str(exc))
        finally:
            with _worker_lock:
                _queued_ids.discard(job_id)
                idle = not _queued_ids
            _cancel_events.pop(job_id, None)
            _job_queue.task_done()
            if idle:
                _release_models()


def _release_models() -> None:
    global _nude_detector, _context_classifier, _nsfw_classifier
    with _model_lock:
        _nude_detector = None
        _context_classifier = None
        _nsfw_classifier = None
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass


def _read_exact(stream: Any, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _iter_frames(path: Path, interval: float, cancel: threading.Event, start_at: float = 0.0) -> Iterable[tuple[float, Any]]:
    try:
        import numpy as np
    except ImportError as exc:
        raise AnalysisError("NumPy is required for video analysis") from exc
    ffmpeg = FFMPEG_PATH if Path(FFMPEG_PATH).exists() else "ffmpeg"
    width, height = 640, 360
    frame_size = width * height * 3
    vf = f"fps={1.0 / interval:.8f},scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
    seek = ["-ss", f"{start_at:.3f}"] if start_at > 0 else []
    command = [ffmpeg, "-nostdin", "-v", "error", *seek, "-i", str(path), "-an", "-sn",
               "-vf", vf, "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"]
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        raise AnalysisError(f"Could not start FFmpeg: {exc}") from exc
    assert process.stdout is not None
    index = 0
    try:
        while True:
            if cancel.is_set():
                raise AnalysisCancelled("Analysis cancelled")
            raw = _read_exact(process.stdout, frame_size)
            if len(raw) != frame_size:
                break
            frame = np.frombuffer(raw, dtype=np.uint8).reshape((height, width, 3)).copy()
            yield start_at + index * interval, frame
            index += 1
        code = process.wait(timeout=10)
        if code and not cancel.is_set():
            error = process.stderr.read().decode("utf-8", "replace")[-1000:] if process.stderr else ""
            raise AnalysisError("FFmpeg frame extraction failed" + (f": {error}" if error else ""))
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()


def _get_nude_detector() -> Any:
    global _nude_detector
    if _nude_detector is not None:
        return _nude_detector
    with _model_lock:
        if _nude_detector is not None:
            return _nude_detector
        try:
            import onnxruntime as ort
            from nudenet import NudeDetector
        except ImportError as exc:
            raise AnalysisError("NudeNet/ONNX Runtime is not installed") from exc
        available = ort.get_available_providers()
        providers = [provider for provider in ("CUDAExecutionProvider", "CPUExecutionProvider") if provider in available]
        _nude_detector = NudeDetector(providers=providers or None, inference_resolution=320)
        return _nude_detector


class _ContextClassifier:
    def __init__(self) -> None:
        try:
            import open_clip
            import torch
        except ImportError as exc:
            raise AnalysisError("OpenCLIP is not installed") from exc
        self.torch = torch
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        cache_dir = DATA_DIR / "models" / "openclip"
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.model, _train, self.preprocess = open_clip.create_model_and_transforms(
            "ViT-B-32", pretrained="laion2b_s34b_b79k", device=self.device, cache_dir=str(cache_dir)
        )
        self.model.eval()
        self.tokenizer = open_clip.get_tokenizer("ViT-B-32")
        self.categories = list(CONTEXT_PROMPTS)
        prompts = [prompt for category in self.categories for prompt in CONTEXT_PROMPTS[category]]
        with torch.no_grad():
            tokens = self.tokenizer(prompts).to(self.device)
            features = self.model.encode_text(tokens)
            self.text_features = features / features.norm(dim=-1, keepdim=True)

    def classify(self, frame: Any) -> dict[str, float]:
        import cv2
        from PIL import Image
        image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        torch = self.torch
        tensor = self.preprocess(image).unsqueeze(0).to(self.device)
        with torch.no_grad():
            features = self.model.encode_image(tensor)
            features = features / features.norm(dim=-1, keepdim=True)
            similarities = (100.0 * features @ self.text_features.T).reshape(len(self.categories), 2)
            probabilities = similarities.softmax(dim=-1)[:, 0].detach().cpu().tolist()
        return dict(zip(self.categories, (float(value) for value in probabilities)))


def _get_context_classifier() -> _ContextClassifier:
    global _context_classifier
    if _context_classifier is not None:
        return _context_classifier
    with _model_lock:
        if _context_classifier is None:
            _context_classifier = _ContextClassifier()
        return _context_classifier


class _NsfwClassifier:
    def __init__(self) -> None:
        model_path = os.getenv("NSFW_MODEL_PATH", "").strip()
        if not model_path:
            raise AnalysisError("Set NSFW_MODEL_PATH to a GantMan/nsfw_model Keras model file")
        try:
            from nsfw_detector import predict
        except ImportError as exc:
            raise AnalysisError("nsfw_detector/TensorFlow is not installed") from exc
        self.predict = predict
        self.model = predict.load_model(model_path)

    def classify(self, image_path: str) -> dict[str, float]:
        result = self.predict.classify(self.model, image_path)
        scores = result.get(image_path) or next(iter(result.values()), {})
        porn = float(scores.get("porn") or 0.0)
        sexy = float(scores.get("sexy") or 0.0)
        hentai = float(scores.get("hentai") or 0.0)
        return {
            "sexual_activity": max(porn, hentai),
            "revealing_attire": sexy,
            "nudity": max(porn, hentai, sexy * 0.85),
        }


def _get_nsfw_classifier() -> _NsfwClassifier:
    global _nsfw_classifier
    if _nsfw_classifier is not None:
        return _nsfw_classifier
    with _model_lock:
        if _nsfw_classifier is None:
            _nsfw_classifier = _NsfwClassifier()
        return _nsfw_classifier


def _model_uses_anatomy(model_key: str) -> bool:
    return model_key in {"nudenet_openclip", "nudenet_only", "nudenet_strict"}


def _model_uses_context(model_key: str) -> bool:
    return model_key in {"nudenet_openclip", "openclip_only"}


def _model_uses_nsfw(model_key: str) -> bool:
    return model_key in {"gantman_nsfw", "haramblur_style"}


def _available_run_models() -> list[str]:
    return [model["key"] for model in model_definitions() if model.get("available") and model["key"] in MODEL_OPTIONS]


def _anatomy_scores(detections: list[dict[str, Any]]) -> dict[str, float]:
    by_class: dict[str, float] = {}
    for detection in detections:
        label = str(detection.get("class") or "").upper()
        by_class[label] = max(by_class.get(label, 0.0), float(detection.get("score") or 0.0))
    return {
        "female_toplessness": max((by_class.get(label, 0.0) for label in FEMALE_TOPLESS_CLASSES), default=0.0),
        "male_toplessness": max((by_class.get(label, 0.0) for label in MALE_TOPLESS_CLASSES), default=0.0),
        "nudity": max((by_class.get(label, 0.0) for label in NUDITY_CLASSES), default=0.0),
        "nudity_strict": max((by_class.get(label, 0.0) for label in NUDITY_CLASSES - FEMALE_TOPLESS_CLASSES), default=0.0),
    }


def _merge_hits(category: str, hits: list[tuple[float, float, str]], interval: float,
                model_version: str,
                duration: float) -> list[dict[str, Any]]:
    if not hits:
        return []
    hits.sort(key=lambda item: item[0])
    scene_lock_categories = BLOCKING_CATEGORIES
    contextual = CATEGORY_INFO[category]["detector"] == "context"
    # Tighter merging window for contextual categories so that isolated
    # false positives (e.g. credits montage mistaken for kissing) do not
    # chain together with distant legitimate hits.
    if category in scene_lock_categories:
        max_gap = max(3.0, interval * 3.0)
    elif contextual:
        max_gap = max(2.5, interval * 1.8)
    else:
        max_gap = max(5.0, interval * 2.25)
    groups: list[list[tuple[float, float, str]]] = [[hits[0]]]
    for hit in hits[1:]:
        if hit[0] - groups[-1][-1][0] <= max_gap:
            groups[-1].append(hit)
        else:
            groups.append([hit])
    segments: list[dict[str, Any]] = []
    for group in groups:
        if category in scene_lock_categories:
            strongest = max(hit[1] for hit in group)
            corroborated = len(group) >= 3 or (len(group) >= 2 and strongest >= 0.82)
            if strongest < 0.94 and not corroborated:
                continue
        elif contextual:
            # Require at least 2 corroborating hits for contextual categories
            # (kissing, revealing_attire).  A single frame must be extremely
            # confident (>= 0.94) to create a segment on its own  -  this guards
            # against credits montages and similar false positives.
            if len(group) < 2 and group[0][1] < 0.94:
                continue
        padding = (
            BLOCKING_SCENE_PADDING_SECONDS
            if category in scene_lock_categories
            else (1.5 if contextual else 0.75)
        )
        start = max(0.0, group[0][0] - interval / 2.0 - padding)
        end = min(duration or group[-1][0] + interval, group[-1][0] + interval / 2.0 + padding)
        if end <= start:
            end = start + max(1.0, interval)
        segments.append({
            "category": category,
            "start_ms": int(start * 1000),
            "end_ms": int(end * 1000),
            "confidence": round(sum(hit[1] for hit in group) / len(group), 4),
            "detector": "+".join(sorted({hit[2] for hit in group})),
            "model_version": model_version,
        })
    return segments


def _save_checkpoint(job_id: int, pending: list[tuple[str, float, float, str]],
                     checkpoint_seconds: float, progress: float, message: str) -> None:
    conn = connect()
    try:
        if pending:
            conn.executemany(
                """INSERT INTO content_analysis_hits(job_id,category,timestamp,confidence,detector)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(job_id,category,timestamp,detector)
                   DO UPDATE SET confidence=MAX(confidence,excluded.confidence)""",
                [(job_id, category, timestamp, confidence, detector)
                 for category, timestamp, confidence, detector in pending],
            )
        conn.execute(
            """UPDATE content_analysis_jobs SET checkpoint_seconds=?,progress=?,message=?,
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (checkpoint_seconds, progress, message, job_id),
        )
        conn.commit()
    finally:
        conn.close()


def _run_job(job_id: int) -> None:
    conn = connect()
    try:
        job = conn.execute("""SELECT content_analysis_jobs.*,media_items.path,media_items.duration,media_items.title
                              FROM content_analysis_jobs JOIN media_items ON media_items.id=content_analysis_jobs.media_id
                              WHERE content_analysis_jobs.id=?""", (job_id,)).fetchone()
        stored_hits = [dict(row) for row in conn.execute(
            "SELECT category,timestamp,confidence,detector FROM content_analysis_hits WHERE job_id=? ORDER BY timestamp",
            (job_id,),
        )]
    finally:
        conn.close()
    if not job:
        return
    media_id = int(job["media_id"])
    path = Path(job["path"])
    duration = float(job["duration"] or 0)
    interval = float(job["sample_interval"] or 2.0)
    checkpoint = max(0.0, float(job["checkpoint_seconds"] or 0.0))
    if duration:
        checkpoint = min(checkpoint, duration)
    try:
        categories = [category for category in json.loads(job["categories"] or "[]") if category in CATEGORY_INFO]
    except json.JSONDecodeError:
        categories = list(CATEGORY_INFO)
    cancel = _cancel_events.setdefault(job_id, threading.Event())
    if cancel.is_set():
        _update_job(job_id, status="cancelled", message="Analysis cancelled", error=None)
        return
    settings = get_filter_settings()
    raw_job_model = str(job["model_version"] or "")
    run_model_key, run_model_keys = _run_models_from_version(raw_job_model)
    if not run_model_keys:
        raise AnalysisError("No available analysis models are installed")
    analysis_version = _analysis_model_version(run_model_key)
    thresholds = SENSITIVITY_THRESHOLDS[settings["sensitivity"]]
    anatomy_categories = [category for category in categories if CATEGORY_INFO[category]["detector"] == "anatomy"]
    context_categories = [
        category for category in categories
        if category in CONTEXT_PROMPTS and CATEGORY_INFO[category]["detector"] == "context"
    ]
    nsfw_categories = [category for category in categories if category in {"sexual_activity", "revealing_attire", "nudity"}]
    initial_progress = min(0.99, checkpoint / duration) if duration else 0.0
    initial_message = (
        f"Resuming {job['title']} at {int(checkpoint // 60)}:{int(checkpoint % 60):02d}"
        if checkpoint else "Loading local analysis models"
    )
    _update_job(job_id, status="running", progress=round(initial_progress, 4),
                message=initial_message, model_version=analysis_version, error=None)
    pending: list[tuple[str, float, float, str]] = []
    try:
        hits: dict[str, list[tuple[float, float, str]]] = {category: [] for category in categories}
        for hit in stored_hits:
            if hit["category"] in hits:
                hits[hit["category"]].append(
                    (float(hit["timestamp"]), float(hit["confidence"]), str(hit["detector"]))
                )
        temp_root = DATA_DIR / "tmp"
        temp_root.mkdir(parents=True, exist_ok=True)
        last_checkpoint = 0.0
        with tempfile.TemporaryDirectory(prefix="diwan-content-", dir=str(temp_root)) as temp_dir:
            frame_path = str(Path(temp_dir) / "frame.jpg")
            total_models = max(1, len(run_model_keys))
            for model_index, active_model_key in enumerate(run_model_keys):
                if cancel.is_set():
                    raise AnalysisCancelled("Analysis cancelled")
                model_label = _model_label(active_model_key)
                model_prefix = f"{active_model_key}:"
                model_start = checkpoint if total_models == 1 else 0.0
                model_anatomy_categories = anatomy_categories if _model_uses_anatomy(active_model_key) else []
                model_context_categories = context_categories if _model_uses_context(active_model_key) else []
                model_nsfw_categories = nsfw_categories if _model_uses_nsfw(active_model_key) else []
                nude = _get_nude_detector() if model_anatomy_categories else None
                context = _get_context_classifier() if model_context_categories else None
                nsfw = _get_nsfw_classifier() if model_nsfw_categories else None
                _save_checkpoint(
                    job_id, [], model_start, round(model_index / total_models, 4),
                    f"Starting {_model_label(active_model_key)} ({model_index + 1}/{total_models})",
                )
                for index, (timestamp, frame) in enumerate(_iter_frames(path, interval, cancel, model_start)):
                    if cancel.is_set():
                        raise AnalysisCancelled("Analysis cancelled")
                    if nude is not None or nsfw is not None:
                        import cv2
                        if not cv2.imwrite(frame_path, frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88]):
                            raise AnalysisError("Could not prepare an analysis frame")
                    anatomy_scores = _anatomy_scores(nude.detect(frame_path)) if nude is not None else {}
                    nsfw_scores = nsfw.classify(frame_path) if nsfw is not None else {}
                    context_scores = context.classify(frame) if context is not None else {}
                    for category in model_anatomy_categories:
                        score = anatomy_scores.get("nudity_strict" if category == "nudity" and active_model_key == "nudenet_strict" else category, 0.0)
                        threshold = thresholds["anatomy"]
                        if settings["policy"].get(category) == "skip" and category in BLOCKING_CATEGORIES:
                            threshold *= BLOCKING_THRESHOLD_FACTORS["anatomy"]
                        if score >= threshold:
                            hit = (timestamp, score, model_prefix + "nudenet")
                            hits[category].append(hit)
                            pending.append((category, *hit))
                    for category in model_nsfw_categories:
                        score = nsfw_scores.get(category, 0.0)
                        threshold = thresholds["context"]
                        if settings["policy"].get(category) == "skip" and category in BLOCKING_CATEGORIES:
                            threshold *= BLOCKING_THRESHOLD_FACTORS["context"]
                        if score >= threshold:
                            hit = (timestamp, score, model_prefix + ("nsfwjs" if active_model_key == "haramblur_style" else "gantman"))
                            hits[category].append(hit)
                            pending.append((category, *hit))
                    for category in model_context_categories:
                        score = context_scores.get(category, 0.0)
                        threshold = thresholds["context"]
                        if settings["policy"].get(category) == "skip" and category in BLOCKING_CATEGORIES:
                            threshold *= BLOCKING_THRESHOLD_FACTORS["context"]
                        if score >= threshold:
                            hit = (timestamp, score, model_prefix + "openclip")
                            hits[category].append(hit)
                            pending.append((category, *hit))
                    last_checkpoint = min(duration, timestamp + interval) if duration else timestamp + interval
                    if index % 5 == 4:
                        model_progress = min(1.0, last_checkpoint / duration) if duration else 0.0
                        progress = min(0.99, (model_index + model_progress) / total_models)
                        _save_checkpoint(
                            job_id, pending, last_checkpoint, round(progress, 4),
                            f"Analyzing with {model_label} ({model_index + 1}/{total_models}) - {int(model_progress * 100)}%",
                        )
                        pending.clear()
                _save_checkpoint(
                    job_id, pending, duration or last_checkpoint,
                    round(min(0.99, (model_index + 1) / total_models), 4),
                    f"Completed {model_label} ({model_index + 1}/{total_models})",
                )
                pending.clear()
        final_progress = min(0.99, last_checkpoint / duration) if duration else 0.99
        _save_checkpoint(job_id, pending, last_checkpoint, round(final_progress, 4), "Finalizing detected scenes")
        pending.clear()
        segments = []
        for category in categories:
            per_model_hits: dict[str, list[tuple[float, float, str]]] = {}
            for timestamp, confidence, detector in hits[category]:
                model_part = detector.split(":", 1)[0] if ":" in detector else _model_key_from_version(analysis_version)
                per_model_hits.setdefault(model_part, []).append((timestamp, confidence, detector))
            for model_part, model_hits in per_model_hits.items():
                segments.extend(_merge_hits(category, model_hits, interval, _analysis_model_version(model_part), duration))
        segments.sort(key=lambda segment: (segment["start_ms"], segment["category"]))
        conn = connect()
        try:
            if run_model_key == ALL_MODELS_KEY:
                conn.execute("DELETE FROM content_segments WHERE media_id=?", (media_id,))
            else:
                for model_part in run_model_keys:
                    conn.execute(
                        "DELETE FROM content_segments WHERE media_id=? AND model_version=?",
                        (media_id, _analysis_model_version(model_part)),
                    )
            conn.executemany(
                """INSERT INTO content_segments(media_id,category,start_ms,end_ms,confidence,detector,model_version)
                   VALUES(?,?,?,?,?,?,?)""",
                [(media_id, segment["category"], segment["start_ms"], segment["end_ms"],
                  segment["confidence"], segment["detector"], segment["model_version"]) for segment in segments],
            )
            conn.execute("DELETE FROM content_analysis_hits WHERE job_id=?", (job_id,))
            conn.execute("""UPDATE content_analysis_jobs SET status='completed',progress=1,checkpoint_seconds=?,
                            message=?,model_version=?,error=NULL,completed_at=CURRENT_TIMESTAMP,
                            updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                         (duration or last_checkpoint,
                          f"Analysis complete - {len(segments)} content segments", analysis_version, job_id))
            conn.commit()
        finally:
            conn.close()
    except AnalysisCancelled:
        _update_job(job_id, status="cancelled", message="Analysis cancelled", error=None)
    except Exception as exc:
        _update_job(job_id, status="failed", message="Analysis failed", error=str(exc)[:2000])
