"""Auth middleware: require_api_key dependency + issue_token helper.

require_api_key reads the Bearer token, decodes the JWT, and returns a
Principal. Use as a typed handler parameter: `p: Principal = Depends(require_api_key)`
— NOT via `dependencies=[Depends(...)]`, because dependencies declared that
way don't inject their return value into the handler's signature.

issue_token mints a JWT for the given user. Used by /auth/login and the
smoke test fixture.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

import jwt
from fastapi import HTTPException, Request

SECRET = os.getenv("JWT_SECRET", "change-me-in-production")


@dataclass
class Principal:
    user_id: int
    email: str
    is_admin: bool


def require_api_key(request: Request) -> Principal:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = auth.removeprefix("Bearer ").strip()
    try:
        claims = jwt.decode(token, SECRET, algorithms=["HS256"])
    except Exception:
        raise HTTPException(status_code=401, detail="invalid token")
    return Principal(
        user_id=int(claims["sub"]),
        email=claims.get("email", ""),
        is_admin=bool(claims.get("is_admin", False)),
    )


def issue_token(user_id: int, email: str, is_admin: bool, ttl_seconds: int = 3600) -> str:
    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "email": email,
        "is_admin": is_admin,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")
