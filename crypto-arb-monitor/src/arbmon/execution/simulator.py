"""Latency-adjusted execution simulator.

For every detected opportunity we assume our order arrives `latency_ms` after
detection and fills against the book AS IT EXISTS THEN. In live capture that is
implemented faithfully: we sleep `latency_ms`, then read the current top of book
(which is, by definition, the book ~latency_ms after detection) and fill against
it. Stored snapshots let the same computation be reproduced offline.

The simulator reports THEORETICAL PnL (at detection prices) and SIMULATED PnL
(at fill-time prices, after fees, capped to L1 depth) as separate numbers, so
the cost of latency is always visible and never hidden inside one blended figure.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from ..data.book_state import BookStore
from ..models import Opportunity, SimFill, now_ms
from ..storage.db import Storage
from .fill_model import depth_limited_input, route_edge, route_final
from .paper_portfolio import PaperPortfolio
from .risk import RiskManager

log = logging.getLogger(__name__)


class LatencySimulator:
    def __init__(
        self,
        book: BookStore,
        storage: Storage,
        portfolio: PaperPortfolio,
        risk: RiskManager,
        *,
        latency_ms: int,
        order_notional_quote: float,
        max_position_per_pair_quote: float,
        to_usdt: Callable[[str, float], float | None],
        on_fill: Callable[[SimFill], None] | None = None,
    ) -> None:
        self.book = book
        self.storage = storage
        self.portfolio = portfolio
        self.risk = risk
        self.latency_ms = latency_ms
        self.order_notional_quote = order_notional_quote
        self.max_position_per_pair_quote = max_position_per_pair_quote
        self.to_usdt = to_usdt
        self.on_fill = on_fill
        self._tasks: set[asyncio.Task] = set()

    def schedule(self, opp: Opportunity) -> None:
        """Schedule the latency-delayed fill for a freshly detected opportunity."""
        route_key = str(opp.detail.get("route_key", opp.opp_type))
        # Per-opportunity notional, bounded by the per-pair risk limit.
        intended_usdt = min(self.order_notional_quote,
                            self.max_position_per_pair_quote)
        ok, reason = self.risk.can_open(route_key, intended_usdt)
        if not ok:
            log.info("sim skipped (%s) for %s", reason, route_key,
                     extra={"extra_fields": {"route": route_key, "reason": reason}})
            return
        task = asyncio.create_task(self._run(opp, intended_usdt, route_key))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, opp: Opportunity, intended_usdt: float,
                   route_key: str) -> None:
        await asyncio.sleep(self.latency_ms / 1000.0)
        try:
            fill = self._simulate(opp, intended_usdt, route_key)
        except Exception:  # noqa: BLE001
            log.exception("simulation failed for opp %s", opp.id)
            return
        self.storage.insert_sim_fill(fill)
        self.portfolio.apply_fill(
            route_key, fill.sim_pnl_quote, fill.theo_pnl_quote,
            fill.filled_quote, fill.survived,
        )
        if self.on_fill:
            self.on_fill(fill)

    def _simulate(self, opp: Opportunity, intended_usdt: float,
                  route_key: str) -> SimFill:
        legs = opp.legs
        start_asset = str(opp.detail.get("start_asset", "USDT"))
        ts_fill = now_ms()

        # Current (post-latency) tops and sizes for every leg symbol.
        tops: dict[str, tuple[float, float]] = {}
        sizes: dict[str, tuple[float, float]] = {}
        for leg in legs:
            venue, sym = leg["venue"], leg["symbol"]
            top = self.book.get(venue, sym)
            if top is None:
                return SimFill(
                    opp_id=opp.id, ts_detect=opp.ts_detect, ts_fill=ts_fill,
                    latency_ms=self.latency_ms, survived=False, filled_quote=0.0,
                    theo_pnl_quote=opp.theo_pnl_quote, sim_pnl_quote=0.0,
                    slippage_bps=0.0, reason=f"no_book_at_fill:{venue}:{sym}",
                )
            tops[sym] = (top.bid, top.ask)
            sizes[sym] = (top.bid_size, top.ask_size)

        # Start amount expressed in the route's start asset.
        start_usdt_px = self.to_usdt(start_asset, 1.0) or 0.0
        if start_usdt_px <= 0:
            return SimFill(
                opp_id=opp.id, ts_detect=opp.ts_detect, ts_fill=ts_fill,
                latency_ms=self.latency_ms, survived=False, filled_quote=0.0,
                theo_pnl_quote=opp.theo_pnl_quote, sim_pnl_quote=0.0,
                slippage_bps=0.0, reason=f"no_usdt_price:{start_asset}",
            )
        desired_start = intended_usdt / start_usdt_px

        edge_fill = route_edge(legs, tops)
        filled_input, binding = depth_limited_input(
            legs, desired_start, tops, sizes)
        final = route_final(legs, filled_input, tops)
        sim_pnl_start = final - filled_input
        sim_pnl_usdt = sim_pnl_start * start_usdt_px
        filled_usdt = filled_input * start_usdt_px

        survived = edge_fill > 0.0
        # Slippage = how much the net edge decayed over the latency window.
        slippage_bps = opp.edge_bps - edge_fill * 10_000.0
        reason = "filled" if survived else "edge_gone_at_fill"
        if binding:
            reason += f";depth_capped:{binding}"

        return SimFill(
            opp_id=opp.id, ts_detect=opp.ts_detect, ts_fill=ts_fill,
            latency_ms=self.latency_ms, survived=survived,
            filled_quote=filled_usdt, theo_pnl_quote=opp.theo_pnl_quote,
            sim_pnl_quote=sim_pnl_usdt, slippage_bps=slippage_bps, reason=reason,
        )

    async def drain(self) -> None:
        """Await all in-flight simulations (used on shutdown)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
