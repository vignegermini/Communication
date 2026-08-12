"""SQLite persistence for ticks, opportunities, and simulated fills.

High-volume rows (book snapshots, trades) are buffered in memory and flushed in
batched transactions to keep the event loop responsive. Low-volume rows
(opportunities, sim fills) are written immediately because we need the row id
back straight away (an opportunity's id links to its simulated fill).

WAL mode lets the dashboard/report read while the capture keeps writing.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from ..models import BookTop, Opportunity, SimFill, Trade

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS book_snapshots (
    id        INTEGER PRIMARY KEY,
    venue     TEXT    NOT NULL,
    symbol    TEXT    NOT NULL,
    bid       REAL    NOT NULL,
    bid_size  REAL    NOT NULL,
    ask       REAL    NOT NULL,
    ask_size  REAL    NOT NULL,
    ts_event  INTEGER NOT NULL,
    ts_recv   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_book_lookup
    ON book_snapshots (venue, symbol, ts_recv);

CREATE TABLE IF NOT EXISTS trades (
    id        INTEGER PRIMARY KEY,
    venue     TEXT    NOT NULL,
    symbol    TEXT    NOT NULL,
    price     REAL    NOT NULL,
    qty       REAL    NOT NULL,
    is_buyer_maker INTEGER NOT NULL,
    ts_event  INTEGER NOT NULL,
    ts_recv   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_trade_lookup
    ON trades (venue, symbol, ts_recv);

CREATE TABLE IF NOT EXISTS opportunities (
    id             INTEGER PRIMARY KEY,
    opp_type       TEXT    NOT NULL,
    ts_detect      INTEGER NOT NULL,
    edge_bps       REAL    NOT NULL,
    notional_quote REAL    NOT NULL,
    depth_quote    REAL    NOT NULL,
    theo_pnl_quote REAL    NOT NULL,
    lifetime_ms    INTEGER,
    peak_edge_bps  REAL,
    legs_json      TEXT    NOT NULL,
    detail_json    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_opp_time ON opportunities (ts_detect);
CREATE INDEX IF NOT EXISTS ix_opp_type ON opportunities (opp_type);

CREATE TABLE IF NOT EXISTS sim_fills (
    id             INTEGER PRIMARY KEY,
    opp_id         INTEGER REFERENCES opportunities(id),
    ts_detect      INTEGER NOT NULL,
    ts_fill        INTEGER NOT NULL,
    latency_ms     INTEGER NOT NULL,
    survived       INTEGER NOT NULL,
    filled_quote   REAL    NOT NULL,
    theo_pnl_quote REAL    NOT NULL,
    sim_pnl_quote  REAL    NOT NULL,
    slippage_bps   REAL    NOT NULL,
    reason         TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fill_time ON sim_fills (ts_detect);
"""


class Storage:
    def __init__(self, db_path: str, persist_snapshots: bool = True) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.persist_snapshots = persist_snapshots
        self.conn = sqlite3.connect(db_path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.executescript(_SCHEMA)
        self._book_buf: list[tuple] = []
        self._trade_buf: list[tuple] = []

    # -- buffered, high volume ------------------------------------------------
    def record_book(self, t: BookTop) -> None:
        if not self.persist_snapshots:
            return
        self._book_buf.append(
            (t.venue, t.symbol, t.bid, t.bid_size, t.ask, t.ask_size,
             t.ts_event, t.ts_recv)
        )

    def record_trade(self, t: Trade) -> None:
        self._trade_buf.append(
            (t.venue, t.symbol, t.price, t.qty, int(t.is_buyer_maker),
             t.ts_event, t.ts_recv)
        )

    def flush(self) -> int:
        n = 0
        if self._book_buf:
            self.conn.executemany(
                "INSERT INTO book_snapshots "
                "(venue,symbol,bid,bid_size,ask,ask_size,ts_event,ts_recv) "
                "VALUES (?,?,?,?,?,?,?,?)",
                self._book_buf,
            )
            n += len(self._book_buf)
            self._book_buf.clear()
        if self._trade_buf:
            self.conn.executemany(
                "INSERT INTO trades "
                "(venue,symbol,price,qty,is_buyer_maker,ts_event,ts_recv) "
                "VALUES (?,?,?,?,?,?,?)",
                self._trade_buf,
            )
            n += len(self._trade_buf)
            self._trade_buf.clear()
        return n

    # -- immediate, low volume ------------------------------------------------
    def insert_opportunity(self, opp: Opportunity) -> int:
        cur = self.conn.execute(
            "INSERT INTO opportunities "
            "(opp_type,ts_detect,edge_bps,notional_quote,depth_quote,"
            " theo_pnl_quote,lifetime_ms,peak_edge_bps,legs_json,detail_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (opp.opp_type, opp.ts_detect, opp.edge_bps, opp.notional_quote,
             opp.depth_quote, opp.theo_pnl_quote, None, opp.edge_bps,
             json.dumps(opp.legs), json.dumps(opp.detail)),
        )
        opp.id = int(cur.lastrowid)
        return opp.id

    def update_opportunity_close(
        self, opp_id: int, lifetime_ms: int, peak_edge_bps: float
    ) -> None:
        self.conn.execute(
            "UPDATE opportunities SET lifetime_ms=?, peak_edge_bps=? WHERE id=?",
            (lifetime_ms, peak_edge_bps, opp_id),
        )

    def insert_sim_fill(self, f: SimFill) -> int:
        cur = self.conn.execute(
            "INSERT INTO sim_fills "
            "(opp_id,ts_detect,ts_fill,latency_ms,survived,filled_quote,"
            " theo_pnl_quote,sim_pnl_quote,slippage_bps,reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f.opp_id, f.ts_detect, f.ts_fill, f.latency_ms, int(f.survived),
             f.filled_quote, f.theo_pnl_quote, f.sim_pnl_quote, f.slippage_bps,
             f.reason),
        )
        return int(cur.lastrowid)

    # -- replay ---------------------------------------------------------------
    def book_at(self, venue: str, symbol: str, ts: int) -> sqlite3.Row | None:
        """Latest stored snapshot for (venue, symbol) at or before `ts` — the
        book 'as it existed then'. Used for offline re-simulation."""
        return self.conn.execute(
            "SELECT * FROM book_snapshots WHERE venue=? AND symbol=? "
            "AND ts_recv<=? ORDER BY ts_recv DESC LIMIT 1",
            (venue, symbol, ts),
        ).fetchone()

    def close(self) -> None:
        try:
            self.flush()
        finally:
            self.conn.close()
