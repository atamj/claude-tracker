import sqlite3
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "tracker.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    cwd           TEXT,
    project_name  TEXT,
    transcript    TEXT,
    started_at    TEXT NOT NULL,
    ended_at      TEXT,
    last_activity TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',
    last_prompt   TEXT,
    prompt_count  INTEGER DEFAULT 0,
    tmux_target   TEXT,
    last_question TEXT,
    terminal_view TEXT,
    first_prompt  TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    payload     TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_activity ON sessions(last_activity);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint    TEXT NOT NULL UNIQUE,
    p256dh      TEXT NOT NULL,
    auth        TEXT NOT NULL,
    user_agent  TEXT,
    created_at  TEXT NOT NULL
);
"""

MIGRATIONS = [
    "ALTER TABLE sessions ADD COLUMN tmux_target TEXT",
    "ALTER TABLE sessions ADD COLUMN last_question TEXT",
    "ALTER TABLE sessions ADD COLUMN terminal_view TEXT",
    "ALTER TABLE sessions ADD COLUMN first_prompt TEXT",
]


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
        for sql in MIGRATIONS:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # column already exists
        conn.commit()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def purge_old(days: int = 30) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM events WHERE created_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        deleted_events = cur.rowcount
        conn.execute(
            "DELETE FROM sessions WHERE ended_at IS NOT NULL AND ended_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        return deleted_events
