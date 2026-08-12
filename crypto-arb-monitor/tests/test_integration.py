"""End-to-end smoke tests: feed a synthetic book, verify the full pipeline
(detect -> persist -> latency-simulate -> report) produces coherent records.
Uses a tiny latency so the async simulation completes within the test.
"""
import asyncio

import pytest

from arbmon.data.book_state import BookStore
from arbmon.execution.paper_portfolio import PaperPortfolio
from arbmon.execution.risk import RiskManager
from arbmon.execution.simulator import LatencySimulator
from arbmon.models import BookTop, now_ms
from arbmon.reporting.daily_report import generate_report
from arbmon.storage.db import Storage
from arbmon.strategy.cross_exchange import CrossExchangeMonitor
from arbmon.strategy.triangular import TriangularScanner


def _top(venue, symbol, bid, ask, size=1000.0):
    t = now_ms()
    return BookTop(venue, symbol, bid, size, ask, size, t, t)


def _wire(tmp_path, latency_ms=20):
    storage = Storage(str(tmp_path / "t.sqlite"))
    book = BookStore()
    pf = PaperPortfolio(starting_notional_quote=100_000.0)
    risk = RiskManager(
        pf, kill_switch_file=str(tmp_path / "KILL"),
        max_position_per_pair_quote=5_000.0, max_daily_sim_loss_quote=1_000.0)

    def to_usdt(asset, amount):
        if asset == "USDT":
            return amount
        top = book.get("binance", f"{asset}USDT")
        return None if top is None else amount * top.mid

    sim = LatencySimulator(
        book, storage, pf, risk, latency_ms=latency_ms,
        order_notional_quote=1_000.0, max_position_per_pair_quote=5_000.0,
        to_usdt=to_usdt)

    class Sink:
        def opened(self, opp):
            oid = storage.insert_opportunity(opp)
            sim.schedule(opp)
            return oid

        def closed(self, oid, lifetime_ms, peak):
            storage.update_opportunity_close(oid, lifetime_ms, peak)

    return storage, book, pf, risk, sim, Sink(), to_usdt


async def test_cross_exchange_detected_and_simulated(tmp_path):
    storage, book, pf, risk, sim, sink, _ = _wire(tmp_path)
    mon = CrossExchangeMonitor(
        book, sink,
        pairs=[{"binance": "BTCUSDT", "coinbase": "BTC-USDT"}],
        binance_fee=0.001, coinbase_fee=0.006, order_notional_quote=1_000.0)

    # Binance ask cheap, Coinbase bid rich -> a big net edge one direction.
    for top in (_top("binance", "BTCUSDT", 100.0, 100.1),
                _top("coinbase", "BTC-USDT", 102.0, 102.1)):
        book.update(top)
        mon.on_book_update(top)

    # Let the latency-delayed simulation fire while the book stays favourable.
    await asyncio.sleep(0.08)

    # Now the edge disappears -> instance closes with a measured lifetime.
    closing = _top("coinbase", "BTC-USDT", 100.0, 100.1)
    book.update(closing)
    mon.on_book_update(closing)
    await sim.drain()
    storage.flush()

    opp = storage.conn.execute("SELECT * FROM opportunities").fetchone()
    assert opp is not None
    assert opp["opp_type"] == "cross_exchange"
    assert opp["edge_bps"] > 0
    assert opp["lifetime_ms"] is not None and opp["lifetime_ms"] >= 0

    fill = storage.conn.execute("SELECT * FROM sim_fills").fetchone()
    assert fill is not None
    assert fill["survived"] == 1           # edge persisted through the 20ms
    assert fill["sim_pnl_quote"] > 0
    assert fill["filled_quote"] > 0

    rep = generate_report(str(tmp_path / "t.sqlite"))
    assert rep.total_theoretical == 1
    assert rep.total_survived == 1
    assert rep.sim_pnl_quote > 0
    assert rep.lifetime_ms["count"] == 1
    storage.close()


async def test_edge_decays_over_latency_is_not_survived(tmp_path):
    storage, book, pf, risk, sim, sink, _ = _wire(tmp_path, latency_ms=30)
    mon = CrossExchangeMonitor(
        book, sink,
        pairs=[{"binance": "BTCUSDT", "coinbase": "BTC-USDT"}],
        binance_fee=0.001, coinbase_fee=0.006, order_notional_quote=1_000.0)

    for top in (_top("binance", "BTCUSDT", 100.0, 100.1),
                _top("coinbase", "BTC-USDT", 102.0, 102.1)):
        book.update(top)
        mon.on_book_update(top)

    # Collapse the edge BEFORE the order arrives (well within 30ms latency).
    await asyncio.sleep(0.005)
    gone = _top("coinbase", "BTC-USDT", 100.0, 100.1)
    book.update(gone)
    # Don't route through the monitor (that would just close it); the point is
    # the book the SIM reads at +30ms no longer has the edge.
    await sim.drain()
    storage.flush()

    fill = storage.conn.execute("SELECT * FROM sim_fills").fetchone()
    assert fill is not None
    assert fill["survived"] == 0
    assert fill["theo_pnl_quote"] > 0     # it WAS profitable at detection
    assert "edge_gone_at_fill" in fill["reason"]
    storage.close()


async def test_triangular_detected(tmp_path):
    storage, book, pf, risk, sim, sink, to_usdt = _wire(tmp_path)
    symbols = ["BTCUSDT", "ETHUSDT", "ETHBTC", "BNBUSDT", "BNBBTC", "BNBETH"]
    scan = TriangularScanner(
        book, sink, assets=["BTC", "ETH", "USDT", "BNB"], symbols=symbols,
        binance_fee=0.0, order_notional_quote=1_000.0, to_usdt=to_usdt)

    # Consistent baseline: BTC=20000, ETH=1000 -> ETHBTC=0.05. Dislocate ETHBTC
    # cheap (0.048) so a USDT->BTC->ETH->USDT cycle is profitable at zero fee.
    feed = {
        "BTCUSDT": (20000.0, 20000.0),
        "ETHUSDT": (1000.0, 1000.0),
        "ETHBTC": (0.048, 0.048),
        "BNBUSDT": (300.0, 300.0),
        "BNBBTC": (0.015, 0.015),
        "BNBETH": (0.30, 0.30),
    }
    for sym, (bid, ask) in feed.items():
        top = _top("binance", sym, bid, ask)
        book.update(top)
    # Trigger evaluation via the dislocated leg.
    scan.on_book_update(_top("binance", "ETHBTC", 0.048, 0.048))
    await sim.drain()
    storage.flush()

    opp = storage.conn.execute(
        "SELECT * FROM opportunities WHERE opp_type='triangular' "
        "ORDER BY edge_bps DESC LIMIT 1").fetchone()
    assert opp is not None
    assert opp["edge_bps"] > 0
    assert opp["depth_quote"] > 0
    storage.close()
