"""Storage for reference rates, quotes, and failed quote attempts.

Uses a hosted Turso database when TURSO_DATABASE_URL is set (needed on Vercel,
where local files don't persist), otherwise a local SQLite file. Both speak the
standard Python DB-API with SQLite SQL, so the rest of this module is shared.
All access goes through cursors so it works with either driver.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import Quote, ReferenceRate

SCHEMA = """
CREATE TABLE IF NOT EXISTS reference_rates (
    run_id    TEXT NOT NULL,
    ts        TEXT NOT NULL,
    pair      TEXT NOT NULL,
    mid       REAL NOT NULL,
    source    TEXT NOT NULL,
    as_of     TEXT
);
CREATE TABLE IF NOT EXISTS quotes (
    run_id        TEXT NOT NULL,
    ts            TEXT NOT NULL,
    chain         TEXT NOT NULL,
    pair          TEXT NOT NULL,
    venue         TEXT NOT NULL,
    side          TEXT NOT NULL,
    size          REAL NOT NULL,
    base_amount   REAL NOT NULL,
    quote_amount  REAL NOT NULL,
    price         REAL NOT NULL,
    ref_mid       REAL,
    deviation_bps REAL,
    gas_estimate  INTEGER,
    meta          TEXT
);
CREATE TABLE IF NOT EXISTS failures (
    run_id  TEXT NOT NULL,
    ts      TEXT NOT NULL,
    chain   TEXT NOT NULL,
    pair    TEXT NOT NULL,
    venue   TEXT NOT NULL,
    side    TEXT NOT NULL,
    size    REAL NOT NULL,
    error   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS quotes_pair_ts ON quotes (pair, ts);
CREATE INDEX IF NOT EXISTS quotes_venue_ts ON quotes (venue, ts);
"""

Connection = Any  # sqlite3.Connection or a turso_serverless connection


def connect(path: str | Path = "fxtracker.db") -> Connection:
    """Open (and create tables in) the database.

    TURSO_DATABASE_URL + TURSO_AUTH_TOKEN -> hosted Turso database.
    Otherwise -> local SQLite file at ``path``. On Vercel without Turso this
    raises, because a local file there would silently disappear.
    """
    url = os.environ.get("TURSO_DATABASE_URL")
    if url:
        import turso_serverless  # third-party; only needed for hosted storage

        conn = turso_serverless.connect(url, auth_token=os.environ.get("TURSO_AUTH_TOKEN"))
    elif os.environ.get("VERCEL"):
        raise RuntimeError("Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN: local files don't persist on Vercel")
    else:
        conn = sqlite3.connect(str(path))

    cur = conn.cursor()
    for stmt in SCHEMA.split(";"):
        if stmt.strip():
            cur.execute(stmt)
    conn.commit()
    return conn


def _execute(conn: Connection, sql: str, args: tuple | list = ()):
    cur = conn.cursor()
    cur.execute(sql, tuple(args))
    return cur


def _dicts(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def save_reference(conn: Connection, run_id: str, ref: ReferenceRate) -> None:
    _execute(
        conn,
        "INSERT INTO reference_rates VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, ref.timestamp.isoformat(), ref.pair, ref.mid, ref.source, ref.as_of),
    )


def save_quote(
    conn: Connection,
    run_id: str,
    quote: Quote,
    size: float,
    ref_mid: float | None = None,
    deviation_bps: float | None = None,
) -> None:
    _execute(
        conn,
        "INSERT INTO quotes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            quote.timestamp.isoformat(),
            quote.chain,
            quote.pair,
            quote.venue,
            quote.side.value,
            size,
            quote.base_amount,
            quote.quote_amount,
            quote.price,
            ref_mid,
            deviation_bps,
            quote.gas_estimate,
            json.dumps(quote.meta, default=str),
        ),
    )


def save_failure(
    conn: Connection,
    run_id: str,
    ts: datetime,
    chain: str,
    pair: str,
    venue: str,
    side: str,
    size: float,
    error: str,
) -> None:
    _execute(
        conn,
        "INSERT INTO failures VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, ts.isoformat(), chain, pair, venue, side, size, error),
    )


def load_quotes(
    conn: Connection,
    *,
    pair: str | None = None,
    venue: str | None = None,
    since: datetime | None = None,
    run_id: str | None = None,
) -> list[dict]:
    """Stored quotes as dicts, oldest first. ``meta`` is decoded from JSON."""
    clauses, args = [], []
    for col, val in (("pair", pair), ("venue", venue), ("run_id", run_id)):
        if val is not None:
            clauses.append(f"{col} = ?")
            args.append(val)
    if since is not None:
        clauses.append("ts >= ?")
        args.append(since.isoformat())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = _dicts(_execute(conn, f"SELECT * FROM quotes {where} ORDER BY ts", args))
    for d in rows:
        d["meta"] = json.loads(d["meta"]) if d["meta"] else {}
    return rows


def load_table(conn: Connection, table: str, run_id: str | None = None) -> list[dict]:
    """All rows of ``reference_rates`` or ``failures``, optionally for one run."""
    if table not in {"reference_rates", "failures"}:
        raise ValueError(f"unknown table {table!r}")
    if run_id is None:
        return _dicts(_execute(conn, f"SELECT * FROM {table} ORDER BY ts"))
    return _dicts(_execute(conn, f"SELECT * FROM {table} WHERE run_id = ? ORDER BY ts", (run_id,)))
