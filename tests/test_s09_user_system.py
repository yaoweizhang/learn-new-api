import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from s09_user_system.code import app  # noqa: E402
from s09_user_system.users import reset_db  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    reset_db()
    yield
    reset_db()


def test_signup_and_login_roundtrip():
    with TestClient(app) as c:
        r = c.post("/auth/signup", json={"email": "a@b.com", "password": "secret123"})
        assert r.status_code == 201, r.text
        r = c.post("/auth/login", json={"email": "a@b.com", "password": "secret123"})
        assert r.status_code == 200
        token = r.json()["access_token"]
        assert token.count(".") == 2  # JWT shape


def test_login_with_wrong_password_fails():
    with TestClient(app) as c:
        c.post("/auth/signup", json={"email": "a@b.com", "password": "secret123"})
        r = c.post("/auth/login", json={"email": "a@b.com", "password": "wrong"})
    assert r.status_code == 401


def test_me_requires_token():
    with TestClient(app) as c:
        r = c.get("/me")
    assert r.status_code == 401


def test_me_returns_user_with_token():
    with TestClient(app) as c:
        c.post("/auth/signup", json={"email": "a@b.com", "password": "secret123"})
        r = c.post("/auth/login", json={"email": "a@b.com", "password": "secret123"})
        token = r.json()["access_token"]
        me = c.get("/me", headers={"authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "a@b.com"
