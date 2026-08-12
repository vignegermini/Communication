"""Application orchestrator: wires the data, strategy, execution, storage, and
dashboard layers into one asyncio process.

Run:  python -m arbmon.app --config config.yaml
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from .config import Config, load_config
from .data.binance_ws import BinanceWS
from .data.book_state import BookStore
from .data.coinbase_ws import CoinbaseWS
from .data.reconnect import run_with_reconnect
from .execution.paper_portfolio import PaperPortfolio
from .execution.risk import RiskManager
from .execution.simulator import LatencySimulator
from .logging_conf import setup_logging
from .models import BookTop, Opportunity, Trade, now_ms
from .reporting.dashboard import DashboardServer
from .storage.db import Storage
from .strategy.cross_exchange import CrossExchangeMonitor
from .strategy.triangular import TriangularScanner

log = logging.getLogger("arbmon")

# Binance's real symbol for an unordered asset pair. Quote is the
# higher-priority asset (USDT is always quote; BTC quotes ETH/BNB; ETH quotes BNB).
_QUOTE_PRIORITY = {"USDT": 4, "FDUSD": 4, "BTC": 3, "ETH": 2, "BNB": 1}


def _binance_symbol_for(a: str, b: str) -> str:
    if _QUOTE_PRIORITY.get(a, 0) >= _QUOTE_PRIORITY.get(b, 0):
        base, quote = b, a
    else:
        base, quote = a, b
    return f"{base}{quote}"


def _required_triangle_symbols(assets: list[str]) -> list[str]:
    from itertools import combinations
    return [_binance_symbol_for(a, b) for a, b in combinations(assets, 2)]


class _Sink:
    """Bridges strategy detections to storage + the latency simulator."""

    def __init__(self, storage: Storage, simulator: LatencySimulator) -> None:
        self.storage = storage
        self.simulator = simulator

    def opened(self, opp: Opportunity) -> int:
        opp_id = self.storage.insert_opportunity(opp)
        self.simulator.schedule(opp)
        return opp_id

    def closed(self, opp_id: int, lifetime_ms: int, peak_edge_bps: float) -> None:
        self.storage.update_opportunity_close(opp_id, lifetime_ms, peak_edge_bps)


class App:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.stop = asyncio.Event()

        self.storage = Storage(
            cfg.get("storage.sqlite_path", "data/arbmon.sqlite"),
            persist_snapshots=bool(cfg.get("storage.persist_book_snapshots", True)),
        )
        self.book = BookStore()
        self.portfolio = PaperPortfolio(
            starting_notional_quote=float(cfg.get("paper.starting_notional_quote", 100000.0))
        )
        self.risk = RiskManager(
            self.portfolio,
            kill_switch_file=cfg.get("run.kill_switch_file", "KILL"),
            max_position_per_pair_quote=float(cfg.get("risk.max_position_per_pair_quote", 5000.0)),
            max_daily_sim_loss_quote=float(cfg.get("risk.max_daily_sim_loss_quote", 1000.0)),
            halt_on_breach=bool(cfg.get("risk.halt_on_breach", True)),
        )

        self.binance_fee = float(cfg.get("venues.binance.taker_fee", 0.001))
        self.coinbase_fee = float(cfg.get("venues.coinbase.taker_fee", 0.006))
        order_notional = float(cfg.get("execution.order_notional_quote", 1000.0))
        max_pos = float(cfg.get("risk.max_position_per_pair_quote", 5000.0))

        self.simulator = LatencySimulator(
            self.book, self.storage, self.portfolio, self.risk,
            latency_ms=int(cfg.get("execution.latency_ms", 250)),
            order_notional_quote=order_notional,
            max_position_per_pair_quote=max_pos,
            to_usdt=self._to_usdt,
        )
        self.sink = _Sink(self.storage, self.simulator)

        # -- symbols: base list plus any cross-quote pairs the triangles need --
        symbols = [s.upper() for s in cfg.get("venues.binance.symbols", [])]
        tri_assets = list(cfg.get("strategy.triangular.assets", ["BTC", "ETH", "USDT", "BNB"]))
        if cfg.get("strategy.triangular.enabled", True):
            for sym in _required_triangle_symbols(tri_assets):
                if sym not in symbols:
                    symbols.append(sym)
                    log.info("auto-added triangle symbol %s", sym)
        self.symbols = symbols

        # -- strategies --
        self.monitors: list = []
        if cfg.get("strategy.cross_exchange.enabled", True):
            self.cross = CrossExchangeMonitor(
                self.book, self.sink,
                pairs=cfg.get("strategy.cross_exchange.pairs", []),
                binance_fee=self.binance_fee, coinbase_fee=self.coinbase_fee,
                order_notional_quote=order_notional,
                min_edge_bps=float(cfg.get("strategy.cross_exchange.min_edge_bps", 0.0)),
            )
            self.monitors.append(self.cross)
        else:
            self.cross = None
        if cfg.get("strategy.triangular.enabled", True):
            self.tri = TriangularScanner(
                self.book, self.sink, assets=tri_assets, symbols=self.symbols,
                binance_fee=self.binance_fee, order_notional_quote=order_notional,
                to_usdt=self._to_usdt,
                min_edge_bps=float(cfg.get("strategy.triangular.min_edge_bps", 0.0)),
            )
            self.monitors.append(self.tri)
        else:
            self.tri = None

        # -- data clients --
        self.binance = BinanceWS(
            cfg.get("venues.binance.ws_base", "wss://stream.binance.com:9443"),
            self.symbols, self._on_booktop, self._on_trade)
        self.coinbase = CoinbaseWS(
            cfg.get("venues.coinbase.ws_url", "wss://ws-feed.exchange.coinbase.com"),
            list(cfg.get("venues.coinbase.products", [])),
            self._on_booktop, self._on_trade)

        # -- dashboard --
        self.dashboard: DashboardServer | None = None
        if cfg.get("dashboard.enabled", True):
            self.dashboard = DashboardServer(
                self.book, self.storage, self.portfolio, self.risk,
                pairs=cfg.get("strategy.cross_exchange.pairs", []),
                binance_fee=self.binance_fee, coinbase_fee=self.coinbase_fee,
                host=cfg.get("dashboard.host", "127.0.0.1"),
                port=int(cfg.get("dashboard.port", 8787)),
                refresh_seconds=int(cfg.get("dashboard.refresh_seconds", 3)),
            )

    # -- pricing helper -------------------------------------------------------
    def _to_usdt(self, asset: str, amount: float) -> float | None:
        if asset == "USDT":
            return amount
        top = self.book.get("binance", f"{asset}USDT")
        if top is None:
            return None
        return amount * top.mid

    # -- data callbacks -------------------------------------------------------
    def _on_booktop(self, top: BookTop) -> None:
        self.book.update(top)
        self.storage.record_book(top)
        for m in self.monitors:
            try:
                m.on_book_update(top)
            except Exception:  # noqa: BLE001
                log.exception("monitor error on %s", top.symbol)

    def _on_trade(self, trade: Trade) -> None:
        self.storage.record_trade(trade)

    # -- background loops -----------------------------------------------------
    async def _flush_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            n = self.storage.flush()
            if n:
                log.debug("flushed %d rows", n)

    async def _kill_switch_loop(self) -> None:
        while not self.stop.is_set():
            if self.risk.kill_switch_present():
                log.error("KILL SWITCH present (%s) — stopping everything",
                          self.risk.kill_switch_file)
                self.stop.set()
                return
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    async def _day_roll_loop(self) -> None:
        # Reset the daily-loss baseline every 24h from start.
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=86400.0)
                return
            except asyncio.TimeoutError:
                self.risk.roll_day()
                log.info("daily risk baseline rolled")

    async def _duration_loop(self, seconds: float | None) -> None:
        if not seconds:
            return
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            log.info("configured duration reached (%.0fs) — stopping", seconds)
            self.stop.set()

    # -- lifecycle ------------------------------------------------------------
    async def run(self) -> None:
        if self.risk.kill_switch_present():
            log.error("kill switch %s exists at startup — remove it to run",
                      self.risk.kill_switch_file)
            return
        if self.dashboard:
            await self.dashboard.start()

        duration = self.cfg.get("run.duration_seconds")
        tasks = [
            asyncio.create_task(run_with_reconnect(
                "binance", self.binance.connect_and_stream, self.stop)),
            asyncio.create_task(run_with_reconnect(
                "coinbase", self.coinbase.connect_and_stream, self.stop)),
            asyncio.create_task(self._flush_loop()),
            asyncio.create_task(self._kill_switch_loop()),
            asyncio.create_task(self._day_roll_loop()),
            asyncio.create_task(self._duration_loop(
                float(duration) if duration else None)),
        ]
        log.info("arbmon running: %d binance symbols, %d cross pairs, %d triangles",
                 len(self.symbols),
                 len(self.cfg.get("strategy.cross_exchange.pairs", [])),
                 self.tri.n_routes if self.tri else 0)
        await self.stop.wait()
        log.info("shutting down…")
        for m in self.monitors:
            m.close_all()
        await self.simulator.drain()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.dashboard:
            await self.dashboard.stop()
        self.storage.close()
        log.info("stopped. portfolio: %s", self.portfolio.snapshot())


def _install_signal_handlers(app: App) -> None:
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, app.stop.set)
        except (NotImplementedError, RuntimeError):
            pass  # e.g. Windows / non-main thread


async def _amain(config_path: str) -> None:
    cfg = load_config(config_path)
    setup_logging(
        level=cfg.get("logging.level", "INFO"),
        as_json=bool(cfg.get("logging.json", True)),
        file=cfg.get("logging.file"),
    )
    app = App(cfg)
    _install_signal_handlers(app)
    await app.run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="arbmon — crypto arb monitor (paper only)")
    parser.add_argument("--config", default="config.yaml", help="path to config YAML")
    args = parser.parse_args(argv)
    try:
        asyncio.run(_amain(args.config))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
