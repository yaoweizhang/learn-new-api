"""Minimal HS256 JWT helper."""
from __future__ import annotations

import os
import time

import jwt

SECRET = os.getenv("JWT_SECRET", "change-me-in-production")


def issue(user_id: int, email: str, is_admin: bool, ttl_seconds: int = 3600) -> str:
    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "email": email,
        "is_admin": is_admin,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


def decode(token: str) -> dict:
    return jwt.decode(token, SECRET, algorithms=["HS256"])
