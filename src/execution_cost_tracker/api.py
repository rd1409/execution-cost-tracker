"""Web API (FastAPI). Served on Vercel via the root ``app.py``.

Endpoints:
    GET  /api/health   liveness check
    GET  /api/pairs    pairs, tokens and venues the front end can offer
    POST /api/quote    on-demand quote for a pair and notional (not stored)
    GET  /api/cron     scheduled run: quote everything and store it
                       (requires ``Authorization: Bearer <CRON_SECRET>``)

FastAPI also serves interactive docs at /docs for trying requests by hand.
All logic lives in ``service.py``; this module only maps HTTP to it.
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import service

app = FastAPI(title="FX Tracker", version="0.3.0")


class QuoteRequest(BaseModel):
    pair: str = Field("EURC/USDC", description="Pair name from /api/pairs")
    notional: float = Field(10_000, gt=0, description="Size in base-token units, e.g. 10000 EURC")
    venues: list[str] | None = Field(None, description="Venue names; omit for all")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/pairs")
def pairs() -> list[dict]:
    return service.list_pairs()


@app.post("/api/quote")
def quote(req: QuoteRequest) -> dict:
    kwargs = {"pair": req.pair, "notional": req.notional}
    if req.venues is not None:
        kwargs["venues"] = req.venues
    try:
        params = service.QuoteParams(**kwargs)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return service.quote(params)


@app.get("/api/cron")
def cron(authorization: str | None = Header(None)) -> dict:
    if not service.check_cron_auth(authorization, os.environ.get("CRON_SECRET")):
        raise HTTPException(status_code=401, detail="unauthorized")
    return service.cron_cycle()
