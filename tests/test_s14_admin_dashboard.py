import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from s14_admin_dashboard.code import app  # noqa: E402


def test_dashboard_home_requires_login():
    with TestClient(app) as c:
        r = c.get("/dashboard/")
    assert r.status_code in (302, 401)


def test_dashboard_login_flow():
    from s14_admin_dashboard.code import ADMIN_EMAIL, ADMIN_PASSWORD
    with TestClient(app) as c:
        r = c.post("/dashboard/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        assert r.status_code == 200
        r = c.get("/dashboard/")
    assert r.status_code == 200
    assert "learn-new-api" in r.text
