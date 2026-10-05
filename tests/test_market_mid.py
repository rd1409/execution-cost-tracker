"""Live market mid: TradingView webhook, storage, status, and vs-mid on quotes."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from execution_cost_tracker import market_hours, registry, service, storage
from execution_cost_tracker.reference import StaticReference
from execution_cost_tracker.venues import SwapResult, Venue

TV_IP = "52.89.214.238"
SECRET = "tv-secret-value-123456"
WED = datetime(2026, 10, 7, 14, 0, tzinfo=timezone.utc)  # FX market open
SAT = datetime(2026, 10, 10, 14, 0, tzinfo=timezone.utc)  # closed


def body(**kw):
    return json.dumps({"secret": SECRET, "ticker": "EURUSD", "price": 1.1231, "time": "2026-10-07T14:00:00Z", **kw})


def db(tmp_path):
    path = tmp_path / "mid.db"
    return lambda: storage.connect(path)


# --- webhook -----------------------------------------------------------------


def test_webhook_stores_mid(tmp_path):
    connect = db(tmp_path)
    out = service.handle_tradingview_webhook(body(), TV_IP, SECRET, connect=connect, now=WED)
    assert out == {"ok": True, "pair": "EUR/USD", "mid": 1.1231, "received_at": WED.isoformat()}
    conn = connect()
    row = storage.latest_market_mid(conn, "EUR/USD")
    assert row["mid"] == 1.1231 and row["source"] == "tradingview" and row["received_at"] == WED
    assert row["ticker"] == "EURUSD" and row["bar_time"] == "2026-10-07T14:00:00Z"
    conn.close()


def test_webhook_bid_ask_midpoint_and_ticker_forms(tmp_path):
    connect = db(tmp_path)
    out = service.handle_tradingview_webhook(
        body(ticker="FX:EURUSD", bid=1.1230, ask=1.1232, price=None), TV_IP, SECRET, connect=connect, now=WED
    )
    assert out["mid"] == pytest.approx(1.1231) and out["pair"] == "EUR/USD"


def test_latest_mid_wins_and_old_rows_pruned(tmp_path):
    connect = db(tmp_path)
    service.handle_tradingview_webhook(body(price=1.10), TV_IP, SECRET, connect=connect, now=WED - timedelta(days=40))
    service.handle_tradingview_webhook(body(price=1.12), TV_IP, SECRET, connect=connect, now=WED - timedelta(minutes=1))
    service.handle_tradingview_webhook(body(price=1.13), TV_IP, SECRET, connect=connect, now=WED)
    conn = connect()
    assert storage.latest_market_mid(conn, "EUR/USD")["mid"] == 1.13
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM market_mids")
    assert cur.fetchone()[0] == 2  # the 40-day-old row was pruned
    assert storage.latest_market_mid(conn, "GBP/USD") is None
    conn.close()


@pytest.mark.parametrize(
    "raw,ip,secret,status,match",
    [
        (body(), TV_IP, None, 503, "TRADINGVIEW_WEBHOOK_SECRET"),
        (body(), "1.2.3.4", SECRET, 403, "TradingView"),
        ("not json", TV_IP, SECRET, 400, "JSON"),
        ("[1, 2]", TV_IP, SECRET, 400, "object"),
        (body(secret="wrong"), TV_IP, SECRET, 401, "secret"),
        (json.dumps({"ticker": "EURUSD", "price": 1.1}), TV_IP, SECRET, 401, "secret"),
        (body(ticker="GBPUSD"), TV_IP, SECRET, 400, "GBPUSD"),
        (body(price="abc"), TV_IP, SECRET, 400, "number"),
        (body(price=-1), TV_IP, SECRET, 400, "positive"),
        (body(price=None), TV_IP, SECRET, 400, "send price"),
        (body(bid=1.13, ask=1.12), TV_IP, SECRET, 400, "ask"),
    ],
)
def test_webhook_rejections(tmp_path, raw, ip, secret, status, match):
    with pytest.raises(service.WebhookError, match=match) as e:
        service.handle_tradingview_webhook(raw, ip, secret, connect=db(tmp_path), now=WED)
    assert e.value.status == status


def test_ip_allowlist_can_be_turned_off(tmp_path):
    raw = {"chains": {"base": {"chain_id": 8453, "tokens": {
        "USDC": {"address": "0x" + "1" * 40, "decimals": 6}, "EURC": {"address": "0x" + "2" * 40, "decimals": 6}}}},
        "pairs": [{"base": "EURC", "quote": "USDC", "reference": "EUR/USD", "chains": ["base"]}],
        "market_mid": {"enforce_ip_allowlist": False}}
    reg = registry.parse(raw)
    out = service.handle_tradingview_webhook(body(), "1.2.3.4", SECRET, connect=db(tmp_path), now=WED, reg=reg)
    assert out["ok"]


# --- status ---------------------------------------------------------------------


def row(mid, at):
    return {"mid": mid, "received_at": at, "source": "tradingview"}


def test_status_available_when_open_and_fresh():
    st = service.market_mid_status("EUR/USD", WED, lambda pair: row(1.1231, WED - timedelta(seconds=40)))
    assert st["available"] and st["mid"] == 1.1231 and st["age_seconds"] == 40 and st["reason"] is None
    assert st["market_open"] is True and st["next_change"].startswith("2026-10-09T21:00")


def test_status_closed_ignores_mid():
    called = []
    st = service.market_mid_status("EUR/USD", SAT, lambda pair: called.append(pair))
    assert not st["available"] and st["reason"] == "FX market closed" and st["mid"] is None
    assert called == []  # no database read while closed


def test_status_stale_missing_and_broken():
    stale = service.market_mid_status("EUR/USD", WED, lambda p: row(1.12, WED - timedelta(minutes=10)))
    assert not stale["available"] and stale["reason"] == "last TradingView price is 10 min old" and stale["mid"] == 1.12
    missing = service.market_mid_status("EUR/USD", WED, lambda p: None)
    assert not missing["available"] and "no market mid" in missing["reason"]

    def boom(pair):
        raise RuntimeError("db down")

    broken = service.market_mid_status("EUR/USD", WED, boom)
    assert not broken["available"] and "RuntimeError" in broken["reason"]


# --- quotes ----------------------------------------------------------------------


class Fake(Venue):
    def __init__(self, name, bid, ask):
        self.name, self.bid, self.ask = name, bid, ask

    def simulate(self, pair, token_in, token_out, amount_in):
        amount = token_in.from_raw(amount_in)
        out = amount * self.bid if token_in is pair.base else amount / self.ask
        return SwapResult(token_out.to_raw(out))


def run_quote(now, lookup):
    return service.quote(
        service.QuoteParams(notional=10_000, venues=["zerox", "aerodrome"]),
        reference=StaticReference({"EUR/USD": 1.1225}),
        venue_factory=lambda names: [Fake("zerox", 1.1228, 1.1234), Fake("aerodrome", 1.1226, 1.1236)],
        mid_lookup=lookup,
        now=now,
    )


def test_quotes_measured_against_market_mid_when_open():
    out = run_quote(WED, lambda p: row(1.1231, WED - timedelta(seconds=30)))
    json.dumps(out)
    assert out["market_mid"]["available"] and out["market_mid"]["mid"] == 1.1231
    by = {(q["venue"], q["side"]): q for q in out["quotes"]}
    # sell at 1.1228 vs mid 1.1231: 3 pips below mid = cost of ~2.67 bps
    assert by[("zerox", "sell")]["vs_market_mid_bps"] == pytest.approx((1.1231 - 1.1228) / 1.1231 * 1e4, rel=1e-3)
    assert by[("zerox", "buy")]["vs_market_mid_bps"] == pytest.approx((1.1234 - 1.1231) / 1.1231 * 1e4, rel=1e-3)
    assert out["best"]["sell"]["venue"] == "zerox" and out["best"]["sell"]["vs_market_mid_bps"] > 0
    assert out["best"]["sell"]["side"] == "sell"


def test_quotes_show_na_when_closed_or_stale():
    closed = run_quote(SAT, lambda p: row(1.1231, SAT))
    assert all(q["vs_market_mid_bps"] is None for q in closed["quotes"])
    assert all(b["vs_market_mid_bps"] is None for b in closed["best"].values())
    assert closed["market_mid"]["reason"] == "FX market closed"
    stale = run_quote(WED, lambda p: row(1.1231, WED - timedelta(hours=1)))
    assert all(q["vs_market_mid_bps"] is None for q in stale["quotes"])
    # the stored reference price still drives the 2% band and the stored deviation
    assert all(q["deviation_bps"] is not None for q in stale["quotes"])


def test_config_exposes_market_hours():
    cfg = service.config_view()
    assert set(cfg["market_hours"]) == {"open", "next_change", "hours"}
    assert cfg["market_mid"]["max_age_seconds"] > 0
    assert cfg["market_hours"]["open"] == market_hours.is_open(datetime.now(timezone.utc))


# --- bid/ask ---------------------------------------------------------------------


def test_bid_ask_stored_and_exposed(tmp_path):
    connect = db(tmp_path)
    raw = json.dumps({"secret": SECRET, "ticker": "EURUSD", "bid": 1.12300, "ask": 1.12320, "time": "1791381600000"})
    out = service.handle_tradingview_webhook(raw, TV_IP, SECRET, connect=connect, now=WED)
    assert out["mid"] == pytest.approx(1.1231) and out["bid"] == 1.123 and out["ask"] == 1.1232
    conn = connect()
    row = storage.latest_market_mid(conn, "EUR/USD")
    conn.close()
    assert (row["bid"], row["ask"]) == (1.123, 1.1232) and row["mid"] == pytest.approx(1.1231)
    st = service.market_mid_status("EUR/USD", WED, lambda p: row)
    assert st["available"] and (st["bid"], st["ask"]) == (1.123, 1.1232)


def test_price_only_leaves_bid_ask_empty(tmp_path):
    connect = db(tmp_path)
    out = service.handle_tradingview_webhook(body(), TV_IP, SECRET, connect=connect, now=WED)
    assert "bid" not in out
    conn = connect()
    row = storage.latest_market_mid(conn, "EUR/USD")
    conn.close()
    assert row["bid"] is None and row["ask"] is None


def test_unfilled_placeholders_get_a_clear_error(tmp_path):
    # What TradingView actually sends for {{bid}}/{{ask}}: it leaves them as-is.
    raw = '{"secret": "%s", "ticker": "EURUSD", "bid": {{bid}}, "ask":{{ask}}, "time": "2026-10-04T22:00:00Z"}' % SECRET
    with pytest.raises(service.WebhookError, match=r"\{\{ask\}\}, \{\{bid\}\}.*Pine script") as e:
        service.handle_tradingview_webhook(raw, TV_IP, SECRET, connect=db(tmp_path), now=WED)
    assert e.value.status == 400
    # bytes bodies (what the API passes) are handled the same way
    with pytest.raises(service.WebhookError, match="bid"):
        service.handle_tradingview_webhook(raw.encode(), TV_IP, SECRET, connect=db(tmp_path), now=WED)


def test_old_market_mids_table_is_migrated(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE market_mids (received_at TEXT NOT NULL, pair TEXT NOT NULL, mid REAL NOT NULL,"
                " source TEXT NOT NULL, ticker TEXT, bar_time TEXT)")
    old.execute("INSERT INTO market_mids VALUES (?, 'EUR/USD', 1.12, 'tradingview', 'EURUSD', NULL)",
                ((WED - timedelta(minutes=1)).isoformat(),))
    old.commit()
    old.close()

    conn = storage.connect(path)  # adds the missing columns
    assert storage.latest_market_mid(conn, "EUR/USD")["bid"] is None
    storage.save_market_mid(conn, "EUR/USD", 1.1231, "tradingview", WED, bid=1.123, ask=1.1232)
    row = storage.latest_market_mid(conn, "EUR/USD")
    assert (row["mid"], row["bid"], row["ask"]) == (1.1231, 1.123, 1.1232)
    conn.close()
    storage.connect(path).close()  # running the migration again is harmless


def test_quote_uses_tradingview_price_as_reference_with_one_read():
    calls = []

    def lookup(pair):
        calls.append(pair)
        return row(1.1231, SAT - timedelta(hours=40))  # Friday's last price, read on Saturday

    out = service.quote(
        service.QuoteParams(notional=10_000, venues=["zerox"]),
        venue_factory=lambda names: [Fake("zerox", 1.1228, 1.1234)],
        mid_lookup=lookup,
        now=SAT,
    )
    ref = out["references"]["EURC/USDC"]
    assert ref["mid"] == 1.1231 and ref["source"] == "tradingview"
    assert out["market_mid"]["reason"] == "FX market closed"  # column is N/A at the weekend...
    assert all(q["vs_market_mid_bps"] is None for q in out["quotes"])
    assert all(q["deviation_bps"] is not None for q in out["quotes"])  # ...but quotes still measured vs Friday's price
    assert calls == ["EUR/USD"]
