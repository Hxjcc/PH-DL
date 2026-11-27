"""Unified SQLite database schema and versioned migrations."""

SCHEMA_VERSION = 11

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    target_type TEXT NOT NULL DEFAULT 'model',
    enabled INTEGER NOT NULL DEFAULT 1,
    preferred_quality TEXT NOT NULL DEFAULT '',
    last_checked_at TEXT,
    last_new_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(username, target_type)
);

CREATE TABLE IF NOT EXISTS videos (
    viewkey TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    uploader TEXT NOT NULL DEFAULT '',
    target_type TEXT NOT NULL DEFAULT 'model',
    quality TEXT NOT NULL DEFAULT '',
    video_page_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'baseline',
    local_path TEXT NOT NULL DEFAULT '',
    remote_path TEXT NOT NULL DEFAULT '',
    file_size INTEGER NOT NULL DEFAULT 0,
    downloaded_at TEXT,
    uploaded_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_videos_uploader ON videos(uploader);
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
CREATE INDEX IF NOT EXISTS idx_videos_downloaded_at ON videos(downloaded_at DESC);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    trigger TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL DEFAULT 'queued',
    label TEXT NOT NULL DEFAULT '',
    params_json TEXT NOT NULL DEFAULT '{}',
    total_items INTEGER NOT NULL DEFAULT 0,
    done_items INTEGER NOT NULL DEFAULT 0,
    uploaded_items INTEGER NOT NULL DEFAULT 0,
    failed_items INTEGER NOT NULL DEFAULT 0,
    skipped_items INTEGER NOT NULL DEFAULT 0,
    error_message TEXT NOT NULL DEFAULT '',
    export_text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);

CREATE TABLE IF NOT EXISTS job_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    viewkey TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    uploader TEXT NOT NULL DEFAULT '',
    target_type TEXT NOT NULL DEFAULT 'model',
    page_url TEXT NOT NULL DEFAULT '',
    download_url TEXT NOT NULL DEFAULT '',
    referer_url TEXT NOT NULL DEFAULT '',
    quality TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    temp_path TEXT NOT NULL DEFAULT '',
    local_path TEXT NOT NULL DEFAULT '',
    bytes_downloaded INTEGER NOT NULL DEFAULT 0,
    bytes_total INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_job_items_job_id ON job_items(job_id);
CREATE INDEX IF NOT EXISTS idx_job_items_viewkey ON job_items(viewkey);
CREATE INDEX IF NOT EXISTS idx_job_items_status ON job_items(status);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# Version 1 is the baseline schema above. Add future changes as
# (target_version, [SQL statements]) entries. The runner applies them in order
# and records progress in SQLite's PRAGMA user_version.
MIGRATIONS: list[tuple[int, list[str]]] = [
    (
        2,
        [
            "CREATE INDEX IF NOT EXISTS idx_videos_status_local_path "
            "ON videos(status, local_path);",
        ],
    ),
    (
        3,
        [
            "ALTER TABLE targets ADD COLUMN avatar_url TEXT NOT NULL DEFAULT '';",
        ],
    ),
    (
        4,
        [
            "ALTER TABLE targets ADD COLUMN display_name TEXT NOT NULL DEFAULT '';",
        ],
    ),
    (
        5,
        [
            # Old files are not implicitly opted in: the old ledger did not distinguish
            # failed uploads from files intentionally kept on disk.
            "ALTER TABLE videos ADD COLUMN upload_requested INTEGER NOT NULL DEFAULT 0;",
            "ALTER TABLE job_items ADD COLUMN upload_requested INTEGER;",
            "CREATE INDEX IF NOT EXISTS idx_videos_pending_upload "
            "ON videos(status, upload_requested) WHERE local_path != '';",
        ],
    ),
    (
        6,
        [
            "CREATE INDEX IF NOT EXISTS idx_videos_activity_sort "
            "ON videos(COALESCE(downloaded_at, uploaded_at, created_at) DESC, viewkey DESC);",
            "CREATE INDEX IF NOT EXISTS idx_videos_uploader_lower "
            "ON videos(LOWER(uploader));",
        ],
    ),
    (
        7,
        [
            "CREATE INDEX IF NOT EXISTS idx_videos_status_activity "
            "ON videos(status, COALESCE(downloaded_at, uploaded_at, created_at) DESC, viewkey DESC);",
            "CREATE INDEX IF NOT EXISTS idx_videos_uploader_activity "
            "ON videos(LOWER(uploader), COALESCE(downloaded_at, uploaded_at, created_at) DESC, viewkey DESC);",
            "CREATE INDEX IF NOT EXISTS idx_videos_quality_activity "
            "ON videos(quality, COALESCE(downloaded_at, uploaded_at, created_at) DESC, viewkey DESC);",
        ],
    ),
    (
        8,
        [
            "ALTER TABLE videos ADD COLUMN published_at TEXT NOT NULL DEFAULT '';",
            "ALTER TABLE videos ADD COLUMN duration_seconds INTEGER NOT NULL DEFAULT 0;",
            "ALTER TABLE job_items ADD COLUMN published_at TEXT NOT NULL DEFAULT '';",
            "ALTER TABLE job_items ADD COLUMN duration_seconds INTEGER NOT NULL DEFAULT 0;",
            "CREATE INDEX IF NOT EXISTS idx_videos_published_sort "
            "ON videos(published_at DESC, COALESCE(downloaded_at, uploaded_at, created_at) DESC, viewkey DESC);",
        ],
    ),
    (
        9,
        [
            """CREATE TABLE targets_v9 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site TEXT NOT NULL DEFAULT 'pornhub',
                username TEXT NOT NULL,
                target_type TEXT NOT NULL DEFAULT 'model',
                enabled INTEGER NOT NULL DEFAULT 1,
                preferred_quality TEXT NOT NULL DEFAULT '',
                avatar_url TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT '',
                last_checked_at TEXT,
                last_new_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(site, username, target_type)
            );""",
            """INSERT INTO targets_v9 (
                id, site, username, target_type, enabled, preferred_quality,
                avatar_url, display_name, last_checked_at, last_new_count, last_error, created_at
            )
            SELECT
                id, 'pornhub', username, target_type, enabled, preferred_quality,
                COALESCE(avatar_url, ''), COALESCE(display_name, ''),
                last_checked_at, last_new_count, last_error, created_at
            FROM targets;""",
            "DROP TABLE targets;",
            "ALTER TABLE targets_v9 RENAME TO targets;",
            "CREATE INDEX IF NOT EXISTS idx_targets_site ON targets(site);",
            "ALTER TABLE videos ADD COLUMN site TEXT NOT NULL DEFAULT 'pornhub';",
            "ALTER TABLE job_items ADD COLUMN site TEXT NOT NULL DEFAULT 'pornhub';",
            "CREATE INDEX IF NOT EXISTS idx_videos_site_activity "
            "ON videos(site, COALESCE(downloaded_at, uploaded_at, created_at) DESC, viewkey DESC);",
        ],
    ),
    (
        10,
        [
            "ALTER TABLE targets ADD COLUMN today_new_date TEXT NOT NULL DEFAULT '';",
            "ALTER TABLE targets ADD COLUMN today_new_count INTEGER NOT NULL DEFAULT 0;",
            "ALTER TABLE targets ADD COLUMN today_new_viewkeys TEXT NOT NULL DEFAULT '[]';",
        ],
    ),
    (
        11,
        [
            "ALTER TABLE jobs ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;",
            """CREATE TRIGGER jobs_revision AFTER UPDATE ON jobs
                WHEN NEW.revision = OLD.revision
                BEGIN
                    UPDATE jobs SET revision = OLD.revision + 1 WHERE id = OLD.id;
                END;""",
            "ALTER TABLE job_items ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;",
            """CREATE TRIGGER job_items_revision AFTER UPDATE ON job_items
                WHEN NEW.revision = OLD.revision
                BEGIN
                    UPDATE job_items SET revision = OLD.revision + 1 WHERE id = OLD.id;
                END;""",
            "CREATE INDEX IF NOT EXISTS idx_job_items_job_viewkey ON job_items(job_id, viewkey, status);",
        ],
    ),
]
