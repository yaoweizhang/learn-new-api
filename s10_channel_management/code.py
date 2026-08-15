"""s10: admin-managed channel registry.

Adds admin-only CRUD on top of s09. `pick_channel_for(model_prefix)` selects
by (priority asc, weight desc, healthy=True).
"""
from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from s09_user_system import jwt_util
from s09_user_system.code import app as s09_app

from s10_channel_management import channels  # noqa: E402

app = FastAPI(title="learn-new-api s10")


def _require_admin(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing token")
    try:
        claims = jwt_util.decode(auth.removeprefix("Bearer ").strip())
    except Exception:
        raise HTTPException(status_code=401, detail="invalid token")
    if not claims.get("is_admin"):
        raise HTTPException(status_code=403, detail="admin only")
    return claims


class ChannelIn(BaseModel):
    name: str
    provider: str
    base_url: str
    weight: int = 100
    priority: int = 0


@app.post("/admin/channels", status_code=201, dependencies=[Depends(_require_admin)])
def create_channel(body: ChannelIn):
    ch = channels.create_channel(
        name=body.name, provider=body.provider, base_url=body.base_url,
        weight=body.weight, priority=body.priority,
    )
    return {"id": ch.id, "name": ch.name}


@app.get("/admin/channels", dependencies=[Depends(_require_admin)])
def list_channels():
    return channels.list_channels()


# Mount s09 LAST so admin routes are matched first (Starlette iterates routes
# in registration order; a `Mount("/")` would otherwise absorb /admin/channels).
app.mount("/", s09_app)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8010")))