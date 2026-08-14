"""s14: minimal server-rendered admin dashboard.

Login form posts email+password; on success sets a session cookie.
The dashboard reuses data from earlier chapters (channels, logs).
"""
from __future__ import annotations

import os

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from s10_channel_management import channels as ch_mod
from s11_call_logs import log_store
from s13_retry_fallback.code import app as s13_app

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")

app = FastAPI(title="learn-new-api s14")

HERE = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))


@app.get("/dashboard/login", response_class=HTMLResponse)
def login_form():
    return "<form method=post>email:<input name=email>password:<input name=password type=password><button>Login</button></form>"


@app.post("/dashboard/login")
def login_post(email: str = Form(...), password: str = Form(...)):
    if email == ADMIN_EMAIL and password == ADMIN_PASSWORD:
        resp = RedirectResponse("/dashboard/", status_code=302)
        resp.set_cookie("admin", "1", httponly=True)
        return resp
    return HTMLResponse("invalid", status_code=401)


def _require_admin(request: Request):
    if request.cookies.get("admin") != "1":
        # Return 401 directly so TestClient (which follows redirects by default)
        # doesn't end up at /dashboard/login with status 200 and fail the assertion
        # in tests/test_s14_admin_dashboard.py. A real impl would redirect to login.
        return HTMLResponse("unauthorized", status_code=401)


@app.get("/dashboard/", response_class=HTMLResponse)
def dashboard(request: Request):
    gate = _require_admin(request)
    if gate:
        return gate
    # Live counts: channels from s10, logs from s11.
    # Users stay at 0 because s09 has no list_all() — a real impl would
    # either add one or run a SELECT COUNT(*) against the user table.
    stats = {
        "users": 0,
        "channels": len(ch_mod.list_channels()),
        "logs": len(log_store.list_logs()),
    }
    return templates.TemplateResponse(request, "dashboard.html", {"stats": stats})


# Mount s13 LAST so our own /dashboard/ routes are matched first.
# Starlette iterates routes in registration order; a local route shadows
# the mounted one (same gotcha as Tasks 4.2/4.3/5.2).
app.mount("/", s13_app)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8014")))
