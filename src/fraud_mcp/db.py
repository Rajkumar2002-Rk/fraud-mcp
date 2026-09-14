"""SQLite access layer: schema, connection handling, and the evaluation clock."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

DEFAULT_DB_FILENAME = "fraud.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    account_id     TEXT PRIMARY KEY,
    customer_name  TEXT NOT NULL,
    home_country   TEXT NOT NULL,
    opened_at      TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('active', 'dormant', 'restricted'))
);

CREATE TABLE IF NOT EXISTS devices (
    device_id   TEXT PRIMARY KEY,
    platform    TEXT NOT NULL,
    user_agent  TEXT NOT NULL,
    first_seen  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS device_events (
    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id  TEXT NOT NULL REFERENCES devices(device_id),
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    event_type TEXT NOT NULL CHECK (event_type IN ('login', 'login_failed', 'password_reset')),
    ts         TEXT NOT NULL,
    ip         TEXT NOT NULL,
    country    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    txn_id     TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    ts         TEXT NOT NULL,
    amount     REAL NOT NULL,
    currency   TEXT NOT NULL,
    merchant   TEXT NOT NULL,
    mcc        TEXT NOT NULL,
    country    TEXT NOT NULL,
    channel    TEXT NOT NULL CHECK (channel IN ('card_present', 'ecommerce', 'transfer', 'atm')),
    device_id  TEXT REFERENCES devices(device_id),
    status     TEXT NOT NULL CHECK (status IN ('settled', 'pending', 'declined'))
);

CREATE TABLE IF NOT EXISTS cases (
    case_id       TEXT PRIMARY KEY,
    account_id    TEXT NOT NULL REFERENCES accounts(account_id),
    reason        TEXT NOT NULL,
    severity      TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    rule_ids      TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    created_by    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_txn_account_ts ON transactions(account_id, ts);
CREATE INDEX IF NOT EXISTS idx_devevent_device_ts ON device_events(device_id, ts);
CREATE INDEX IF NOT EXISTS idx_devevent_account ON device_events(account_id);
CREATE INDEX IF NOT EXISTS idx_cases_account ON cases(account_id);
"""


def db_path() -> Path:
    """Resolve the database location.

    `FRAUD_MCP_DB` overrides everything; otherwise the dataset lives in a
    per-user data directory.

    Two earlier attempts were wrong in instructive ways. Deriving the path from
    `__file__` put the database inside site-packages once the package was
    installed non-editable. Writing it to `<repo>/data/` instead put a file that
    changes on every run inside the build tree, which made uv treat the project
    as modified and reinstall the editable package on each `uv run` - producing
    an intermittent `ModuleNotFoundError` as the console script raced the
    reinstall.

    The dataset is generated, reproducible from a fixed seed, and not source. It
    belongs outside the repository.
    """
    override = os.environ.get("FRAUD_MCP_DB")
    if override:
        return Path(override).expanduser()

    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
    return root / "fraud-mcp" / DEFAULT_DB_FILENAME


def connect(path: Path | str | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    target = Path(path) if path is not None else db_path()
    if read_only:
        if not target.exists():
            raise FileNotFoundError(
                f"No dataset at {target}. Run `uv run python scripts/serve.py seed` first."
            )
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def as_of(conn: sqlite3.Connection) -> datetime:
    """The evaluation clock.

    Rules are evaluated against the dataset's frozen `as_of` timestamp, not
    against wall-clock now. Without this, "5 transactions in 10 minutes" would
    silently stop firing as the seeded data aged, and the test suite would rot.
    Every tool response echoes this value so a reviewer can reproduce a verdict
    months later.
    """
    raw = get_meta(conn, "as_of")
    if raw is None:
        raise RuntimeError(
            "Database has no 'as_of' marker. Run `uv run python scripts/serve.py seed` to build it."
        )
    return datetime.fromisoformat(raw)
