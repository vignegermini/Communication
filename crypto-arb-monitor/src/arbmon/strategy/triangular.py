"""Triangular arbitrage scanner on Binance.

Enumerates every 3-leg cycle over the configured assets (default BTC/ETH/USDT/
BNB), and on each relevant book update recomputes the cycles that use the
updated symbol. Every positive round-trip (net of three legs of taker fees) is
logged as an opportunity instance with timestamp, edge, lifetime, and L1 depth,
and fires a latency-adjusted simulation.

Cycles are re-anchored to start at USDT when USDT is one of the three assets, so
PnL is denominated in USDT directly; otherwise PnL is converted via the current
mid of the start asset.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from ..data.book_state import BookStore
from ..models import BookTop, Opportunity, now_ms
from .fees import to_bps
from .triangles import Leg, Market, Triangle, enumerate_triangles, parse_symbol

log = logging.getLogger(__name__)


@dataclass
class _Instance:
    opp_id: int
    ts_open: int
    peak_edge_bps: float


@dataclass
class _Route:
    key: str
    start_asset: str
    legs: list[dict]          # simulator/fill_model leg dicts (with venue+fee)
    symbols: tuple[str, ...]
    describe: str


def _rotate_to_start(tri: Triangle, start_asset: str) -> list[Leg]:
    """Reorder a cycle's legs so it begins at `start_asset` (rotation-invariant
    for the return fraction)."""
    legs = list(tri.legs)
    for i, leg in enumerate(legs):
        if leg.from_asset == start_asset:
            return legs[i:] + legs[:i]
    return legs  # start_asset not in cycle; leave as-is


class TriangularScanner:
    def __init__(
        self,
        book: BookStore,
        sink,
        *,
        assets: list[str],
        symbols: list[str],
        binance_fee: float,
        order_notional_quote: float,
        to_usdt: Callable[[str, float], float | None],
        min_edge_bps: float = 0.0,
    ) -> None:
        self.book = book
        self.sink = sink
        self.fee = binance_fee
        self.order_notional_quote = order_notional_quote
        self.to_usdt = to_usdt
        self.min_edge_bps = min_edge_bps
        self._active: dict[str, _Instance] = {}

        asset_set = set(assets)
        markets: list[Market] = []
        for sym in symbols:
            m = parse_symbol(sym.upper(), asset_set)
            if m is not None:
                markets.append(m)
        self.markets = markets
        triangles = enumerate_triangles(assets, markets)

        self._routes: list[_Route] = []
        self._symbol_index: dict[str, list[_Route]] = {}
        for tri in triangles:
            start = "USDT" if "USDT" in {tri.start, *(l.to_asset for l in tri.legs)} else tri.start
            legs = _rotate_to_start(tri, start)
            leg_dicts = [
                {"action": l.action, "venue": "binance", "symbol": l.symbol,
                 "fee": self.fee, "from": l.from_asset, "to": l.to_asset}
                for l in legs
            ]
            syms = tuple(l.symbol for l in legs)
            key = "TRI:" + "->".join([start] + [l.to_asset for l in legs])
            route = _Route(key, start, leg_dicts, syms, tri.describe())
            self._routes.append(route)
            for s in syms:
                self._symbol_index.setdefault(s, []).append(route)

        log.info("triangular scanner: %d markets, %d cycles",
                 len(markets), len(self._routes))

    @property
    def n_routes(self) -> int:
        return len(self._routes)

    def on_book_update(self, top: BookTop) -> None:
        if top.venue != "binance":
            return
        for route in self._symbol_index.get(top.symbol, ()):  # affected cycles
            self._evaluate(route)

    def _tops_and_sizes(self, route: _Route):
        tops: dict[str, tuple[float, float]] = {}
        sizes: dict[str, tuple[float, float]] = {}
        for sym in route.symbols:
            t = self.book.get("binance", sym)
            if t is None:
                return None, None
            tops[sym] = (t.bid, t.ask)
            sizes[sym] = (t.bid_size, t.ask_size)
        return tops, sizes

    def _evaluate(self, route: _Route) -> None:
        from ..execution.fill_model import depth_limited_input, route_edge

        tops, sizes = self._tops_and_sizes(route)
        if tops is None:
            return
        edge = route_edge(route.legs, tops)
        edge_bps = to_bps(edge)
        now = now_ms()
        active = self._active.get(route.key)

        if edge_bps > self.min_edge_bps:
            start_px = self.to_usdt(route.start_asset, 1.0) or 0.0
            # Max fillable start amount within L1 depth, expressed in USDT.
            filled_start, _ = depth_limited_input(route.legs, 1e18, tops, sizes)
            depth_quote = filled_start * start_px
            if active is None:
                self._open(route, edge, edge_bps, depth_quote, now)
            else:
                active.peak_edge_bps = max(active.peak_edge_bps, edge_bps)
        elif active is not None:
            self._close(route, active, now)

    def _open(self, route: _Route, edge: float, edge_bps: float,
              depth_quote: float, now: int) -> None:
        notional = min(self.order_notional_quote, depth_quote)
        opp = Opportunity(
            opp_type="triangular",
            ts_detect=now,
            edge_bps=edge_bps,
            notional_quote=self.order_notional_quote,
            depth_quote=depth_quote,
            theo_pnl_quote=edge * notional,
            legs=route.legs,
            detail={
                "route_key": route.key,
                "start_asset": route.start_asset,
                "path": route.describe,
            },
        )
        opp_id = self.sink.opened(opp)
        self._active[route.key] = _Instance(opp_id, now, edge_bps)
        log.info("triangular edge %s %.2f bps depth=%.0f USDT",
                 route.key, edge_bps, depth_quote,
                 extra={"extra_fields": {"route": route.key,
                                         "edge_bps": round(edge_bps, 3),
                                         "depth_quote": round(depth_quote, 2)}})

    def _close(self, route: _Route, inst: _Instance, now: int) -> None:
        lifetime = max(0, now - inst.ts_open)
        self.sink.closed(inst.opp_id, lifetime, inst.peak_edge_bps)
        del self._active[route.key]

    def close_all(self, now: int | None = None) -> None:
        now = now or now_ms()
        for key, inst in list(self._active.items()):
            self.sink.closed(inst.opp_id, max(0, now - inst.ts_open),
                             inst.peak_edge_bps)
            del self._active[key]
