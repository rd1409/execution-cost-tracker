"""SQLite storage for reference rates, quotes, and failed quote attempts."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
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


def connect(path: str | Path = "fxtracker.db") -> sqlite3.Connection:
    """Open (and create if needed) the database."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def save_reference(conn: sqlite3.Connection, run_id: str, ref: ReferenceRate) -> None:
    conn.execute(
        "INSERT INTO reference_rates VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, ref.timestamp.isoformat(), ref.pair, ref.mid, ref.source, ref.as_of),
    )


def save_quote(
    conn: sqlite3.Connection,
    run_id: str,
    quote: Quote,
    size: float,
    ref_mid: float | None = None,
    deviation_bps: float | None = None,
) -> None:
    conn.execute(
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
    conn: sqlite3.Connection,
    run_id: str,
    ts: datetime,
    chain: str,
    pair: str,
    venue: str,
    side: str,
    size: float,
    error: str,
) -> None:
    conn.execute(
        "INSERT INTO failures VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, ts.isoformat(), chain, pair, venue, side, size, error),
    )


def load_quotes(
    conn: sqlite3.Connection,
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
    rows = conn.execute(f"SELECT * FROM quotes {where} ORDER BY ts", args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["meta"] = json.loads(d["meta"]) if d["meta"] else {}
        out.append(d)
    return out


def load_table(conn: sqlite3.Connection, table: str, run_id: str | None = None) -> list[dict]:
    """All rows of ``reference_rates`` or ``failures``, optionally for one run."""
    if table not in {"reference_rates", "failures"}:
        raise ValueError(f"unknown table {table!r}")
    if run_id is None:
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY ts").fetchall()
    else:
        rows = conn.execute(f"SELECT * FROM {table} WHERE run_id = ? ORDER BY ts", (run_id,)).fetchall()
    return [dict(r) for r in rows]
