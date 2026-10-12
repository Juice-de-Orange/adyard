"""SQLite access layer with a tiny, explicit migration runner.

One connection per request/worker loop; SQLite in WAL mode handles the
concurrency between the web process and the media worker.
"""
import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config

log = logging.getLogger("maxads.db")

# The columns that point at files in the data directory.
MEDIA_COLUMNS = ("raw_path", "media_path", "thumb_path")

# Each migration runs exactly once, in order. Never edit an applied one -- append.
MIGRATIONS: list[tuple[str, str]] = [
    (
        "0001_initial",
        """
        CREATE TABLE users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT NOT NULL UNIQUE,
            password    TEXT NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            created_by  INTEGER REFERENCES users(id)
        );

        CREATE TABLE ads (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            slug          TEXT NOT NULL UNIQUE,
            title         TEXT NOT NULL,
            target_url    TEXT NOT NULL DEFAULT '',
            kind          TEXT NOT NULL,              -- 'image' | 'video'
            status        TEXT NOT NULL DEFAULT 'processing',
            raw_path      TEXT NOT NULL,
            media_path    TEXT,
            thumb_path    TEXT,
            width         INTEGER,
            height        INTEGER,
            active        INTEGER NOT NULL DEFAULT 1,
            weight        INTEGER NOT NULL DEFAULT 1,
            error         TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            created_by    INTEGER REFERENCES users(id)
        );

        CREATE TABLE jobs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ad_id       INTEGER NOT NULL REFERENCES ads(id) ON DELETE CASCADE,
            state       TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|failed
            attempts    INTEGER NOT NULL DEFAULT 0,
            last_error  TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE events (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ad_id     INTEGER NOT NULL REFERENCES ads(id) ON DELETE CASCADE,
            kind      TEXT NOT NULL,        -- 'impression' | 'click'
            day       TEXT NOT NULL,        -- YYYY-MM-DD, for cheap grouping
            visitor   TEXT NOT NULL,        -- daily-rotating hash, never a raw IP
            referer   TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        -- One impression per ad, visitor and day. The unique index IS the
        -- deduplication: a reload must not inflate the numbers.
        CREATE UNIQUE INDEX idx_events_unique
            ON events(ad_id, kind, day, visitor);
        CREATE INDEX idx_events_ad ON events(ad_id, kind);
        CREATE INDEX idx_jobs_state ON jobs(state);
        CREATE INDEX idx_ads_active ON ads(active, status);
        """,
    ),
    (
        "0002_sites",
        """
        -- Which foreign pages actually run our ads. Kept apart from `events`
        -- on purpose: impressions are deduplicated per visitor and day, so
        -- counting domains from that table would miss every page a returning
        -- visitor saw second.
        CREATE TABLE sites (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ad_id       INTEGER NOT NULL REFERENCES ads(id) ON DELETE CASCADE,
            domain      TEXT NOT NULL,
            hits        INTEGER NOT NULL DEFAULT 0,
            clicks      INTEGER NOT NULL DEFAULT 0,
            first_seen  TEXT NOT NULL DEFAULT (datetime('now')),
            last_seen   TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX idx_sites_unique ON sites(ad_id, domain);
        CREATE INDEX idx_sites_domain ON sites(domain);
        """,
    ),
    (
        "0003_must_change_password",
        """
        -- A generated first-admin password is a shared secret until it is
        -- replaced, so the account is locked to the password form until then.
        ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0;
        """,
    ),
]


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def split_statements(sql: str) -> list[str]:
    """Split a migration into single statements.

    executescript() cannot be used here: it commits the open transaction
    before running, which makes the surrounding BEGIN/COMMIT a no-op and a
    half-applied migration impossible to roll back.
    """
    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if buffer.strip() and sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        statements.append(buffer.strip())
    return statements


def migrate() -> list[str]:
    """Apply pending migrations atomically. Returns the names that were applied."""
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    applied: list[str] = []
    with session() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name TEXT PRIMARY KEY,"
            " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
        for name, sql in MIGRATIONS:
            if name in done:
                continue
            conn.execute("BEGIN")
            try:
                for statement in split_statements(sql):
                    conn.execute(statement)
                conn.execute("INSERT INTO schema_migrations(name) VALUES (?)", (name,))
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise
            applied.append(name)
        relativize_media_paths(conn)
    return applied


def _relative_to_base(value: str) -> str | None:
    """`value` relative to BASE_DIR, or None when it lies outside."""
    path = Path(os.path.normpath(value))
    base = Path(os.path.abspath(config.BASE_DIR))
    for candidate in (base, base.resolve()):
        try:
            return path.relative_to(candidate).as_posix()
        except ValueError:
            continue
    return None


def relativize_media_paths(conn: sqlite3.Connection) -> int:
    """Rewrite absolute media paths under BASE_DIR as relative ones.

    Up to v0.1 the paths were stored absolute, so moving the data directory broke
    every banner. Runs on every start and is idempotent: once converted, there is
    nothing left to match. A path outside BASE_DIR is left alone and logged --
    guessing where such a file went could point a banner at the wrong one.
    Returns the number of converted paths.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            "SELECT id, slug, raw_path, media_path, thumb_path FROM ads WHERE "
            + " OR ".join(f"{col} LIKE '/%'" for col in MEDIA_COLUMNS)
        ).fetchall()
        converted = 0
        for row in rows:
            for col in MEDIA_COLUMNS:
                value = row[col]
                if not value or not os.path.isabs(value):
                    continue
                relative = _relative_to_base(value)
                if relative is None:
                    log.warning(
                        "ad %s: %s %s is outside the data directory %s and was left "
                        "as it is -- the banner breaks if that path goes away",
                        row["slug"], col, value, config.BASE_DIR,
                    )
                    continue
                conn.execute(f"UPDATE ads SET {col} = ? WHERE id = ?", (relative, row["id"]))
                converted += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    if converted:
        log.info("%d media path(s) converted to relative (data directory %s)",
                 converted, config.BASE_DIR)
    return converted
