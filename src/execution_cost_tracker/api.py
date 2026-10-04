"""Web API (FastAPI). Served on Vercel via the root ``app.py``.

Endpoints:
    GET  /             the front-end page (public/index.html)
    GET  /api/health   liveness check
    GET  /api/config   chains, tokens, pairs, venues and limits for the form
    GET  /api/pairs    one entry per (pair, chain) with token details
    POST /api/quote    on-demand quote (not stored); rate limited and cached
    GET  /api/cron     scheduled run: quote everything and store it
                       (requires ``Authorization: Bearer <CRON_SECRET>``)
    POST /api/tradingview/webhook
                       TradingView alert pushes the live market mid
                       (secret in the JSON body; TradingView IPs only)

FastAPI also serves interactive docs at /docs for trying requests by hand.
All logic lives in ``service.py``; this module only maps HTTP to it.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import guardrails, service

app = FastAPI(title="FX Tracker", version="0.4.0")

INDEX_HTML = Path(__file__).resolve().parents[2] / "public" / "index.html"


class QuoteRequest(BaseModel):
    pair: str = Field("EURC/USDC", description="Pair name, e.g. EURC/USDC (see /api/config)")
    notional: float = Field(10_000, gt=0, description="Size in base-token units, e.g. 10000 EURC")
    side: str = Field("both", description="buy, sell, or both")
    source_chain: str = Field("base", description="Chain the trade starts on")
    destination_chain: str | None = Field(None, description="Chain it ends on; defaults to source_chain")
    venues: list[str] | None = Field(None, description="Venue names; omit for every venue on the chain")


@app.get("/", include_in_schema=False)
def index():
    # On Vercel the CDN usually serves public/index.html itself; this covers
    # local runs and the case where "/" reaches the function.
    if INDEX_HTML.exists():
        return HTMLResponse(INDEX_HTML.read_text())
    return RedirectResponse("/index.html")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/config")
def config() -> dict:
    return service.config_view()


@app.get("/api/pairs")
def pairs() -> list[dict]:
    return service.list_pairs()


@app.post("/api/quote")
def quote(req: QuoteRequest, request: Request):
    try:
        params = service.QuoteParams(
            pair=req.pair,
            notional=req.notional,
            side=req.side,
            source_chain=req.source_chain,
            destination_chain=req.destination_chain,
            venues=req.venues,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    client = guardrails.client_key(dict(request.headers), request.client.host if request.client else None)
    try:
        return service.guarded_quote(params, client)
    except guardrails.RateLimited as e:
        return JSONResponse(
            status_code=429,
            content={"detail": str(e), "retry_after": e.retry_after},
            headers={"Retry-After": str(e.retry_after)},
        )


@app.post("/api/tradingview/webhook", include_in_schema=False)
async def tradingview_webhook(request: Request):
    body = await request.body()
    client = guardrails.client_key(dict(request.headers), request.client.host if request.client else None)
    try:
        return service.handle_tradingview_webhook(body, client, os.environ.get("TRADINGVIEW_WEBHOOK_SECRET"))
    except service.WebhookError as e:
        return JSONResponse(status_code=e.status, content={"detail": str(e)})


@app.get("/api/cron")
def cron(authorization: str | None = Header(None)) -> dict:
    if not service.check_cron_auth(authorization, os.environ.get("CRON_SECRET")):
        raise HTTPException(status_code=401, detail="unauthorized")
    return service.cron_cycle()
