"""Offline demo: run the FULL detection -> simulation -> storage -> report
pipeline against a synthetic order-book feed, with no network access.

This exercises the exact same monitors, latency simulator, risk manager, storage
and report code that the live app uses — only the data source is swapped for a
random-walk feed that occasionally injects a fleeting, realistic dislocation.
Use it to see the dashboard and daily report populate before pointing arbmon at
real venues.

    python scripts/demo.py --seconds 20 --db data/demo.sqlite [--dashboard]
"""
from __future__ import annotations

import argparse
import asyncio
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from arbmon.data.book_state import BookStore                     # noqa: E402
from arbmon.execution.paper_portfolio import PaperPortfolio       # noqa: E402
from arbmon.execution.risk import RiskManager                     # noqa: E402
from arbmon.execution.simulator import LatencySimulator           # noqa: E402
from arbmon.logging_conf import setup_logging                     # noqa: E402
from arbmon.models import BookTop, now_ms                         # noqa: E402
from arbmon.reporting.daily_report import generate_report         # noqa: E402
from arbmon.storage.db import Storage                             # noqa: E402
from arbmon.strategy.cross_exchange import CrossExchangeMonitor   # noqa: E402
from arbmon.strategy.triangular import TriangularScanner          # noqa: E402

BINANCE_FEE = 0.001
COINBASE_FEE = 0.006

# Base mid prices for a consistent, arbitrage-free starting point.
MIDS = {
    ("binance", "BTCUSDT"): 60000.0,
    ("binance", "ETHUSDT"): 3000.0,
    ("binance", "BNBUSDT"): 600.0,
    ("binance", "ETHBTC"): 3000.0 / 60000.0,
    ("binance", "BNBBTC"): 600.0 / 60000.0,
    ("binance", "BNBETH"): 600.0 / 3000.0,
    ("coinbase", "BTC-USDT"): 60000.0,
    ("coinbase", "ETH-USDT"): 3000.0,
}


def _emit(book, storage, monitors, venue, symbol, mid, rel_spread, size):
    half = mid * rel_spread / 2.0
    t = now_ms()
    top = BookTop(venue, symbol, bid=mid - half, bid_size=size,
                  ask=mid + half, ask_size=size, ts_event=t, ts_recv=t)
    book.update(top)
    storage.record_book(top)
    for m in monitors:
        m.on_book_update(top)


async def run(seconds: float, db_path: str, use_dashboard: bool) -> None:
    setup_logging(level="INFO", as_json=False, file=None)
    rng = random.Random(7)

    storage = Storage(db_path)
    book = BookStore()
    pf = PaperPortfolio(starting_notional_quote=100_000.0)
    risk = RiskManager(pf, kill_switch_file="KILL_DEMO",
                       max_position_per_pair_quote=5_000.0,
                       max_daily_sim_loss_quote=1_000.0)

    def to_usdt(asset, amount):
        if asset == "USDT":
            return amount
        top = book.get("binance", f"{asset}USDT")
        return None if top is None else amount * top.mid

    sim = LatencySimulator(book, storage, pf, risk, latency_ms=250,
                           order_notional_quote=1_000.0,
                           max_position_per_pair_quote=5_000.0, to_usdt=to_usdt)

    class Sink:
        def opened(self, opp):
            oid = storage.insert_opportunity(opp)
            sim.schedule(opp)
            return oid

        def closed(self, oid, lifetime_ms, peak):
            storage.update_opportunity_close(oid, lifetime_ms, peak)

    sink = Sink()
    pairs = [{"binance": "BTCUSDT", "coinbase": "BTC-USDT"},
             {"binance": "ETHUSDT", "coinbase": "ETH-USDT"}]
    cross = CrossExchangeMonitor(book, sink, pairs=pairs,
                                 binance_fee=BINANCE_FEE, coinbase_fee=COINBASE_FEE,
                                 order_notional_quote=1_000.0)
    tri = TriangularScanner(
        book, sink, assets=["BTC", "ETH", "USDT", "BNB"],
        symbols=[s for (v, s) in MIDS if v == "binance"],
        binance_fee=BINANCE_FEE, order_notional_quote=1_000.0, to_usdt=to_usdt)
    monitors = [cross, tri]

    dash = None
    if use_dashboard:
        from arbmon.reporting.dashboard import DashboardServer
        dash = DashboardServer(book, storage, pf, risk, pairs=pairs,
                               binance_fee=BINANCE_FEE, coinbase_fee=COINBASE_FEE)
        await dash.start()

    # Seed every book so the first evaluation has both sides.
    mids = dict(MIDS)
    for (venue, symbol), mid in mids.items():
        _emit(book, storage, monitors, venue, symbol, mid, 0.0002, 5.0)

    started = now_ms()
    tick = 0
    while (now_ms() - started) / 1000.0 < seconds:
        tick += 1
        # Random-walk each mid slightly (keeps prices realistic and moving).
        for key in list(mids):
            mids[key] *= (1.0 + rng.gauss(0, 0.00015))
        # Keep cross/triangle consistent most of the time.
        mids[("coinbase", "BTC-USDT")] = mids[("binance", "BTCUSDT")] * (1 + rng.gauss(0, 0.0002))
        mids[("coinbase", "ETH-USDT")] = mids[("binance", "ETHUSDT")] * (1 + rng.gauss(0, 0.0002))
        mids[("binance", "ETHBTC")] = (mids[("binance", "ETHUSDT")]
                                       / mids[("binance", "BTCUSDT")]) * (1 + rng.gauss(0, 0.0003))

        # ~2% of ticks: inject a brief, exploitable dislocation.
        inject = rng.random() < 0.02
        if inject:
            which = rng.choice(["cross_btc", "cross_eth", "tri_ethbtc"])
            # Cross-exchange must clear 0.1%+0.6% = 0.7% in taker fees before it
            # is a real edge, so an illustrative injection has to exceed that.
            if which == "cross_btc":
                mids[("coinbase", "BTC-USDT")] = mids[("binance", "BTCUSDT")] * 1.010
            elif which == "cross_eth":
                mids[("coinbase", "ETH-USDT")] = mids[("binance", "ETHUSDT")] * 1.009
            else:
                mids[("binance", "ETHBTC")] = (mids[("binance", "ETHUSDT")]
                                               / mids[("binance", "BTCUSDT")]) * 0.9975

        for (venue, symbol), mid in mids.items():
            spread = rng.uniform(0.0001, 0.0004)
            size = rng.uniform(2.0, 20.0)
            _emit(book, storage, monitors, venue, symbol, mid, spread, size)

        storage.flush()
        await asyncio.sleep(0.01)

    for m in monitors:
        m.close_all()
    await sim.drain()
    storage.flush()

    if dash:
        print("\nDashboard live at http://127.0.0.1:8787  (Ctrl-C to stop)")
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        await dash.stop()

    print()
    print(generate_report(db_path).to_text())
    storage.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="arbmon offline demo (no network)")
    ap.add_argument("--seconds", type=float, default=15.0)
    ap.add_argument("--db", default="data/demo.sqlite")
    ap.add_argument("--dashboard", action="store_true",
                    help="serve the live dashboard and keep running")
    args = ap.parse_args()
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    # Fresh DB each demo run.
    for suffix in ("", "-wal", "-shm"):
        p = Path(args.db + suffix)
        if p.exists():
            p.unlink()
    try:
        asyncio.run(run(args.seconds, args.db, args.dashboard))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
