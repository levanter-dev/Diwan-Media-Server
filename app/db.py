import sqlite3
from .config import DATABASE_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS libraries (id INTEGER PRIMARY KEY, name TEXT NOT NULL, root TEXT NOT NULL UNIQUE, kind TEXT NOT NULL DEFAULT 'mixed');
CREATE TABLE IF NOT EXISTS media_items (id INTEGER PRIMARY KEY, library_id INTEGER NOT NULL, title TEXT NOT NULL, media_type TEXT NOT NULL, path TEXT NOT NULL UNIQUE, size INTEGER NOT NULL, modified REAL NOT NULL, duration REAL, width INTEGER, height INTEGER, status TEXT NOT NULL DEFAULT 'ready', FOREIGN KEY(library_id) REFERENCES libraries(id));
CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0, message TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scores (id INTEGER PRIMARY KEY, provider TEXT NOT NULL, external_id TEXT NOT NULL, media_type TEXT NOT NULL, title TEXT NOT NULL, year TEXT, poster_url TEXT, backdrop_url TEXT, overview TEXT, score INTEGER NOT NULL, notes TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(provider, external_id));
CREATE TABLE IF NOT EXISTS download_jobs (id INTEGER PRIMARY KEY, title TEXT NOT NULL, media_type TEXT NOT NULL DEFAULT 'movie', provider TEXT, external_id TEXT, season_number INTEGER, episode_number INTEGER, adapter_id TEXT, adapter_source_id TEXT, adapter_server_id TEXT, source_name TEXT NOT NULL, source_url TEXT NOT NULL, destination_path TEXT, status TEXT NOT NULL DEFAULT 'queued', progress REAL NOT NULL DEFAULT 0, bytes_downloaded INTEGER NOT NULL DEFAULT 0, bytes_total INTEGER, error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, completed_at TEXT);
CREATE TABLE IF NOT EXISTS playback_progress (media_id INTEGER PRIMARY KEY, position_ms INTEGER NOT NULL DEFAULT 0, duration_ms INTEGER, finished INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(media_id) REFERENCES media_items(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS suggestion_seeds (id INTEGER PRIMARY KEY, provider TEXT NOT NULL, external_id TEXT NOT NULL, media_type TEXT NOT NULL, title TEXT NOT NULL, year TEXT, poster_url TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(provider, external_id));
CREATE TABLE IF NOT EXISTS circle_members (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS circle_scores (id INTEGER PRIMARY KEY, circle_id INTEGER NOT NULL, provider TEXT NOT NULL, external_id TEXT NOT NULL, media_type TEXT NOT NULL, title TEXT NOT NULL, year TEXT, poster_url TEXT, backdrop_url TEXT, overview TEXT, score INTEGER NOT NULL, notes TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(circle_id, provider, external_id), FOREIGN KEY(circle_id) REFERENCES circle_members(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS circle_watched (circle_id INTEGER NOT NULL, media_id INTEGER NOT NULL, watched INTEGER NOT NULL DEFAULT 1, watched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(circle_id,media_id), FOREIGN KEY(circle_id) REFERENCES circle_members(id) ON DELETE CASCADE, FOREIGN KEY(media_id) REFERENCES media_items(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS circle_playback_progress (circle_id INTEGER NOT NULL, media_id INTEGER NOT NULL, position_ms INTEGER NOT NULL DEFAULT 0, duration_ms INTEGER, finished INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(circle_id,media_id), FOREIGN KEY(circle_id) REFERENCES circle_members(id) ON DELETE CASCADE, FOREIGN KEY(media_id) REFERENCES media_items(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS content_analysis_jobs (id INTEGER PRIMARY KEY, media_id INTEGER NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'queued', progress REAL NOT NULL DEFAULT 0, message TEXT, categories TEXT NOT NULL, sample_interval REAL NOT NULL DEFAULT 2.0, model_version TEXT, settings_revision INTEGER NOT NULL DEFAULT 1, error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, completed_at TEXT, FOREIGN KEY(media_id) REFERENCES media_items(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS content_segments (id INTEGER PRIMARY KEY, media_id INTEGER NOT NULL, category TEXT NOT NULL, start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, confidence REAL NOT NULL, detector TEXT NOT NULL, model_version TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(media_id) REFERENCES media_items(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS content_analysis_hits (job_id INTEGER NOT NULL, category TEXT NOT NULL, timestamp REAL NOT NULL, confidence REAL NOT NULL, detector TEXT NOT NULL, PRIMARY KEY(job_id,category,timestamp,detector), FOREIGN KEY(job_id) REFERENCES content_analysis_jobs(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS media_filter_overrides (media_id INTEGER NOT NULL, category TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(media_id,category), FOREIGN KEY(media_id) REFERENCES media_items(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS media_filter_model_overrides (media_id INTEGER PRIMARY KEY, model_key TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(media_id) REFERENCES media_items(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS content_segment_overrides (segment_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(segment_id) REFERENCES content_segments(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS content_segment_reviews (id INTEGER PRIMARY KEY, media_id INTEGER NOT NULL, category TEXT NOT NULL, start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, enabled INTEGER NOT NULL, note TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(media_id) REFERENCES media_items(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS suggestion_exclusions (id INTEGER PRIMARY KEY, provider TEXT NOT NULL, external_id TEXT NOT NULL, media_type TEXT NOT NULL, title TEXT NOT NULL, year TEXT, poster_url TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(provider, external_id));
CREATE TABLE IF NOT EXISTS media_versions (id INTEGER PRIMARY KEY, media_id INTEGER NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL, modified REAL NOT NULL, duration REAL, width INTEGER, height INTEGER, label TEXT, metadata TEXT, is_default INTEGER NOT NULL DEFAULT 0, FOREIGN KEY(media_id) REFERENCES media_items(id) ON DELETE CASCADE);
CREATE UNIQUE INDEX IF NOT EXISTS idx_media_versions_path ON media_versions(media_id, path);
CREATE INDEX IF NOT EXISTS idx_content_segments_media_time ON content_segments(media_id,start_ms,end_ms);
CREATE INDEX IF NOT EXISTS idx_content_segment_reviews_media_time ON content_segment_reviews(media_id,start_ms,end_ms);
"""

MIGRATIONS = [
    "ALTER TABLE download_jobs ADD COLUMN season_number INTEGER",
    "ALTER TABLE download_jobs ADD COLUMN episode_number INTEGER",
    "ALTER TABLE download_jobs ADD COLUMN adapter_id TEXT",
    "ALTER TABLE download_jobs ADD COLUMN adapter_source_id TEXT",
    "ALTER TABLE download_jobs ADD COLUMN adapter_server_id TEXT",
    "ALTER TABLE download_jobs ADD COLUMN library_id INTEGER",
    "ALTER TABLE download_jobs ADD COLUMN destination_root TEXT",
    "ALTER TABLE media_items ADD COLUMN metadata TEXT",
    "ALTER TABLE media_items ADD COLUMN watched INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE media_items ADD COLUMN file_deleted INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE media_items ADD COLUMN watched_at TEXT",
    "ALTER TABLE media_items ADD COLUMN parent_id INTEGER",
    "ALTER TABLE media_items ADD COLUMN season_number INTEGER",
    "ALTER TABLE media_items ADD COLUMN episode_number INTEGER",
    "ALTER TABLE media_items ADD COLUMN entry_origin TEXT NOT NULL DEFAULT 'scan'",
    "ALTER TABLE media_items ADD COLUMN folder_path TEXT",
    "ALTER TABLE content_analysis_jobs ADD COLUMN settings_revision INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE content_analysis_jobs ADD COLUMN checkpoint_seconds REAL NOT NULL DEFAULT 0",
    "CREATE TABLE IF NOT EXISTS suggestion_exclusions (id INTEGER PRIMARY KEY, provider TEXT NOT NULL, external_id TEXT NOT NULL, media_type TEXT NOT NULL, title TEXT NOT NULL, year TEXT, poster_url TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(provider, external_id))",
    "CREATE TABLE IF NOT EXISTS media_filter_model_overrides (media_id INTEGER PRIMARY KEY, model_key TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(media_id) REFERENCES media_items(id) ON DELETE CASCADE)",
    "CREATE TABLE IF NOT EXISTS content_segment_reviews (id INTEGER PRIMARY KEY, media_id INTEGER NOT NULL, category TEXT NOT NULL, start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, enabled INTEGER NOT NULL, note TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(media_id) REFERENCES media_items(id) ON DELETE CASCADE)",
    "CREATE INDEX IF NOT EXISTS idx_content_segment_reviews_media_time ON content_segment_reviews(media_id,start_ms,end_ms)",
    "CREATE TABLE IF NOT EXISTS circle_watched (circle_id INTEGER NOT NULL, media_id INTEGER NOT NULL, watched INTEGER NOT NULL DEFAULT 1, watched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(circle_id,media_id), FOREIGN KEY(circle_id) REFERENCES circle_members(id) ON DELETE CASCADE, FOREIGN KEY(media_id) REFERENCES media_items(id) ON DELETE CASCADE)",
    "CREATE TABLE IF NOT EXISTS circle_playback_progress (circle_id INTEGER NOT NULL, media_id INTEGER NOT NULL, position_ms INTEGER NOT NULL DEFAULT 0, duration_ms INTEGER, finished INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(circle_id,media_id), FOREIGN KEY(circle_id) REFERENCES circle_members(id) ON DELETE CASCADE, FOREIGN KEY(media_id) REFERENCES media_items(id) ON DELETE CASCADE)",
]

def connect() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    for migration in MIGRATIONS:
        try:
            conn.execute(migration)
        except sqlite3.OperationalError:
            pass
    if not conn.execute("SELECT 1 FROM circle_members LIMIT 1").fetchone():
        conn.execute("INSERT INTO circle_members(id,name) VALUES(1,'Me')")
    if not conn.execute("SELECT 1 FROM settings WHERE key='legacy_scores_migrated'").fetchone():
        conn.execute("""INSERT OR IGNORE INTO circle_scores(circle_id,provider,external_id,media_type,title,year,poster_url,backdrop_url,overview,score,notes,created_at,updated_at)
                        SELECT 1,provider,external_id,media_type,title,year,poster_url,backdrop_url,overview,score,notes,created_at,updated_at FROM scores""")
        conn.execute("INSERT INTO settings(key,value) VALUES('legacy_scores_migrated','1')")
    if not conn.execute("SELECT 1 FROM settings WHERE key='legacy_profile_state_migrated'").fetchone():
        conn.execute("""INSERT OR IGNORE INTO circle_watched(circle_id,media_id,watched,watched_at)
                        SELECT 1,id,1,COALESCE(watched_at,CURRENT_TIMESTAMP) FROM media_items WHERE watched=1""")
        conn.execute("""INSERT OR IGNORE INTO circle_playback_progress(circle_id,media_id,position_ms,duration_ms,finished,updated_at)
                        SELECT 1,media_id,position_ms,duration_ms,finished,updated_at FROM playback_progress""")
        conn.execute("INSERT INTO settings(key,value) VALUES('legacy_profile_state_migrated','1')")
    conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('score_aggregation','average')")
    conn.execute("""INSERT OR IGNORE INTO settings(key,value) VALUES('content_filter_policy',
                 '{"sexual_activity":"skip","female_toplessness":"skip","male_toplessness":"skip","kissing":"marker","revealing_attire":"marker","nudity":"skip"}')""")
    conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('content_filter_sensitivity','balanced')")
    conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('content_filter_model','nudenet_openclip')")
    conn.execute("""INSERT OR IGNORE INTO settings(key,value) VALUES('content_filter_confirmation',
                 '{"min_models":1,"high_confidence":0.95,"window_seconds":3.0,"require_confirmation_for_skip":true}')""")
    conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('content_filter_revision','1')")
    conn.commit()
    return conn
