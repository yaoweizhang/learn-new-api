"""/admin/channels, /admin/logs, /admin/stats — combines s10 and s11 admin surface."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from s_full.middleware.auth import Principal, require_api_key
from s_full.models import channel
from s_full.models.log import list_logs

router = APIRouter()


def _require_admin(p: Principal = Depends(require_api_key)) -> Principal:
    if not p.is_admin:
        raise HTTPException(status_code=403, detail="admin only")
    return p


class ChannelIn(BaseModel):
    name: str
    provider: str
    base_url: str
    weight: int = 100
    priority: int = 0


@router.post("/admin/channels", status_code=201)
def create_channel(body: ChannelIn, _: Principal = Depends(_require_admin)):
    ch = channel.create_channel(
        name=body.name, provider=body.provider, base_url=body.base_url,
        weight=body.weight, priority=body.priority,
    )
    return {"id": ch.id, "name": ch.name}


@router.get("/admin/channels")
def list_channels_route(_: Principal = Depends(_require_admin)):
    return channel.list_channels()


@router.get("/admin/logs")
def logs_route(_: Principal = Depends(_require_admin)):
    return list_logs()


@router.get("/admin/stats")
def stats_route(_: Principal = Depends(_require_admin)):
    logs = list_logs()
    by_model: dict[str, int] = {}
    for entry in logs:
        by_model[entry.get("model", "?")] = by_model.get(entry.get("model", "?"), 0) + 1
    return {"total": len(logs), "by_model": by_model}
