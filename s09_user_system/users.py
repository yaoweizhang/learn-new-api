"""SQLite-backed user table. Uses stdlib sqlite3 for clarity."""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path("/tmp/learn-new-api-users.db")
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def reset_db() -> None:
    try:
        DB_PATH.unlink(missing_ok=True)
    except OSError:
        # Windows holds the file open while the process lives; fall back to
        # clearing rows so test isolation still works.
        with _conn() as conn:
            conn.execute("DELETE FROM users")
            conn.commit()


def create_user(email: str, password_hash: str, is_admin: bool = False) -> int:
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO users(email, password_hash, is_admin) VALUES(?,?,?)",
            (email, password_hash, 1 if is_admin else 0),
        )
        conn.commit()
        return cur.lastrowid


def find_by_email(email: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, email, password_hash, is_admin FROM users WHERE email=?", (email,)
        ).fetchone()
    return dict(row) if row else None
