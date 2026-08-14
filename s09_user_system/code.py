"""s09: user signup/login + JWT.

Uses s08's chat endpoint unchanged. Adds:
    POST /auth/signup      {email, password}
    POST /auth/login       {email, password} -> {access_token}
    GET  /me               Bearer JWT -> {id, email, is_admin}
"""
from __future__ import annotations

import bcrypt
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from s09_user_system import jwt_util, users
from s08_rate_limiting.code import app as s08_app  # reuse whole s08 app

app = FastAPI(title="learn-new-api s09")
app.mount("/v1", s08_app)


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


def _current_user(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing token")
    try:
        claims = jwt_util.decode(auth.removeprefix("Bearer ").strip())
    except Exception:
        raise HTTPException(status_code=401, detail="invalid token")
    return claims


@app.get("/me")
def me(claims: dict = Depends(_current_user)):
    return {"id": int(claims["sub"]), "email": claims["email"], "is_admin": claims.get("is_admin", False)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(__import__("os").getenv("PORT", "8009")))
