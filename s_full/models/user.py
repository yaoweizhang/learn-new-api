"""SQLite-backed user table. Same shape as s09, but create_user takes a raw
password and hashes internally for tutorial readability."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import bcrypt

DB_PATH = Path("/tmp/learn-new-api-s_full-users.db")
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

QUOTA_SCHEMA = """
CREATE TABLE IF NOT EXISTS quotas (
    user_id INTEGER PRIMARY KEY,
    balance INTEGER NOT NULL DEFAULT 0
);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA + QUOTA_SCHEMA)
    return conn


def reset_db() -> None:
    try:
        DB_PATH.unlink(missing_ok=True)
    except OSError:
        # Windows holds the file open while the process lives; fall back to
        # clearing rows so test isolation still works.
        with _conn() as conn:
            conn.execute("DELETE FROM users")
            conn.execute("DELETE FROM quotas")
            conn.commit()


def create_user(email: str, password: str, is_admin: bool = False) -> int:
    pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO users(email, password_hash, is_admin) VALUES(?,?,?)",
            (email, pw_hash, 1 if is_admin else 0),
        )
        conn.commit()
        return cur.lastrowid


def find_by_email(email: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, email, password_hash, is_admin FROM users WHERE email=?", (email,)
        ).fetchone()
    return dict(row) if row else None


def verify_password(email: str, password: str) -> dict | None:
    """Returns user row dict if password matches, else None."""
    u = find_by_email(email)
    if not u:
        return None
    if not bcrypt.checkpw(password.encode("utf-8"), u["password_hash"].encode("utf-8")):
        return None
    return u
