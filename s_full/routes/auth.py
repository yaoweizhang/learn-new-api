"""/auth/signup, /auth/login, /me — same shape as s09."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from s_full.middleware.auth import Principal, issue_token, require_api_key
from s_full.models.user import create_user, find_by_email, verify_password

router = APIRouter()


class Credentials(BaseModel):
    email: str
    password: str


@router.post("/auth/signup", status_code=201)
def signup(creds: Credentials):
    if find_by_email(creds.email):
        raise HTTPException(status_code=409, detail="email already registered")
    uid = create_user(creds.email, creds.password)
    token = issue_token(uid, creds.email, is_admin=False)
    return {"id": uid, "email": creds.email, "access_token": token}


@router.post("/auth/login")
def login(creds: Credentials):
    u = verify_password(creds.email, creds.password)
    if not u:
        raise HTTPException(status_code=401, detail="invalid credentials")
    token = issue_token(u["id"], u["email"], bool(u["is_admin"]))
    return {"access_token": token, "token_type": "bearer"}


@router.get("/me")
def me(p: Principal = Depends(require_api_key)):
    return {"id": p.user_id, "email": p.email, "is_admin": p.is_admin}
