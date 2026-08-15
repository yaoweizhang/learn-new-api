"""s09: user signup/login + JWT.

Uses s08's chat endpoint unchanged. Adds:
    POST /auth/signup      {email, password}
    POST /auth/login       {email, password} -> {access_token}
    GET  /me               Bearer JWT -> {id, email, is_admin}
"""
from __future__ import annotations

import bcrypt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from s09_user_system import jwt_util, token_blacklist, users
from s08_rate_limiting.code import app as s08_app  # reuse whole s08 app

app = FastAPI(title="learn-new-api s09")


class Credentials(BaseModel):
    email: str
    password: str


@app.post("/auth/signup", status_code=201)
def signup(creds: Credentials):
    existing = users.find_by_email(creds.email)
    if existing:
        raise HTTPException(status_code=409, detail="email already registered")
    pw_hash = bcrypt.hashpw(creds.password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    uid = users.create_user(creds.email, pw_hash)
    token = jwt_util.issue(uid, creds.email, is_admin=False)
    return {"id": uid, "email": creds.email, "access_token": token}


@app.post("/auth/login")
def login(creds: Credentials):
    u = users.find_by_email(creds.email)
    if not u or not bcrypt.checkpw(creds.password.encode("utf-8"), u["password_hash"].encode("utf-8")):
        raise HTTPException(status_code=401, detail="invalid credentials")
    token = jwt_util.issue(u["id"], u["email"], bool(u["is_admin"]))
    return {"access_token": token, "token_type": "bearer"}


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing token")
    return auth.removeprefix("Bearer ").strip()


def _current_user(request: Request) -> dict:
    token = _bearer(request)
    if token_blacklist.get_default().is_revoked(token):
        raise HTTPException(status_code=401, detail="token revoked")
    try:
        return jwt_util.decode(token)
    except Exception:
        raise HTTPException(status_code=401, detail="invalid token")


@app.get("/me")
def me(claims: dict = Depends(_current_user)):
    return {"id": int(claims["sub"]), "email": claims["email"], "is_admin": claims.get("is_admin", False)}


@app.post("/auth/logout", status_code=204)
def logout(request: Request):
    token = _bearer(request)  # 401 if missing
    token_blacklist.get_default().revoke(token)
    return None


# Mount s08 LAST so our local /auth/* and /me routes match first.
# Starlette iterates routes in registration order; a Mount("/") registered
# earlier would absorb everything.
app.mount("/", s08_app)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(__import__("os").getenv("PORT", "8009")))
