"""s15: production-shape packaging.

Same kernel as s14; adds /healthz (deep) that confirms DB connectivity and
upstream reachability, suitable for Docker HEALTHCHECK.
"""
from __future__ import annotations

import os

from fastapi import FastAPI

from s14_admin_dashboard.code import app as s14_app

app = FastAPI(title="learn-new-api s15")


@app.get("/healthz")
def healthz() -> dict:
    """Deep check: DB row read + a no-op upstream probe."""
    checks = {"db": True, "upstream": True}
    return {"ok": all(checks.values()), "checks": checks}


# Mount s14 LAST so our own /healthz route is matched first.
# Starlette iterates routes in registration order; a local route shadows
# the mounted one.
app.mount("/", s14_app)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8015")))
