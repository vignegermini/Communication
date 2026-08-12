"""Cross-exchange spread monitor.

Logs every instance where the bid on one venue exceeds the ask on the other by
more than both venues' taker fees. An 'instance' is a contiguous stretch of time
the net edge stays positive; we record its size, its lifetime in milliseconds,
and the L1 depth available at the detected prices. Detection fires a
latency-adjusted execution simulation from the moment the instance opens.

Both directions of each pair are tracked independently (Binance-bid vs
Coinbase-ask, and vice versa), since they use different sides of each book.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ..data.book_state import BookStore
from ..models import BookTop, Opportunity, now_ms
from .fees import cross_exchange_return, to_bps

log = logging.getLogger(__name__)


@dataclass
class _Instance:
    opp_id: int
    ts_open: int
    peak_edge_bps: float


@dataclass(frozen=True)
class _Direction:
    key: str
    buy_venue: str
    buy_symbol: str
    buy_fee: float
    sell_venue: str
    sell_symbol: str
    sell_fee: float
    pair_label: str


class CrossExchangeMonitor:
    def __init__(
        self,
        book: BookStore,
        sink,                       # object with opened(opp)->int and closed(...)
        *,
        pairs: list[dict],
        binance_fee: float,
        coinbase_fee: float,
        order_notional_quote: float,
        min_edge_bps: float = 0.0,
    ) -> None:
        self.book = book
        self.sink = sink
        self.order_notional_quote = order_notional_quote
        self.min_edge_bps = min_edge_bps
        self._dirs: list[_Direction] = []
        self._symbol_index: dict[tuple[str, str], list[_Direction]] = {}
        self._active: dict[str, _Instance] = {}

        for p in pairs:
            b, c = p["binance"], p["coinbase"]
            label = b
            d1 = _Direction(f"{label}:buyBIN_sellCB", "binance", b, binance_fee,
                            "coinbase", c, coinbase_fee, label)
            d2 = _Direction(f"{label}:buyCB_sellBIN", "coinbase", c, coinbase_fee,
                            "binance", b, binance_fee, label)
            for d in (d1, d2):
                self._dirs.append(d)
                self._symbol_index.setdefault((d.buy_venue, d.buy_symbol), []).append(d)
                self._symbol_index.setdefault((d.sell_venue, d.sell_symbol), []).append(d)

    def on_book_update(self, top: BookTop) -> None:
        for d in self._symbol_index.get((top.venue, top.symbol), ()):  # affected dirs
            self._evaluate(d)

    def _evaluate(self, d: _Direction) -> None:
        buy = self.book.get(d.buy_venue, d.buy_symbol)
        sell = self.book.get(d.sell_venue, d.sell_symbol)
        if buy is None or sell is None:
            return
        # Buy at buy.ask on buy_venue, sell at sell.bid on sell_venue.
        edge = cross_exchange_return(
            bid_a=sell.bid, ask_b=buy.ask, fee_a=d.sell_fee, fee_b=d.buy_fee)
        edge_bps = to_bps(edge)
        now = now_ms()

        active = self._active.get(d.key)
        if edge_bps > self.min_edge_bps:
            # Depth available (USDT) is the smaller of the two L1 sides.
            depth_quote = min(buy.ask_size * buy.ask, sell.bid_size * sell.bid)
            if active is None:
                self._open(d, edge, edge_bps, depth_quote, buy, sell, now)
            else:
                active.peak_edge_bps = max(active.peak_edge_bps, edge_bps)
        elif active is not None:
            self._close(d, active, now)

    def _open(self, d: _Direction, edge: float, edge_bps: float,
              depth_quote: float, buy: BookTop, sell: BookTop, now: int) -> None:
        notional = min(self.order_notional_quote, depth_quote)
        opp = Opportunity(
            opp_type="cross_exchange",
            ts_detect=now,
            edge_bps=edge_bps,
            notional_quote=self.order_notional_quote,
            depth_quote=depth_quote,
            theo_pnl_quote=edge * notional,
            legs=[
                {"action": "buy", "venue": d.buy_venue, "symbol": d.buy_symbol,
                 "fee": d.buy_fee},
                {"action": "sell", "venue": d.sell_venue, "symbol": d.sell_symbol,
                 "fee": d.sell_fee},
            ],
            detail={
                "route_key": d.key,
                "pair": d.pair_label,
                "start_asset": "USDT",
                "buy_venue": d.buy_venue, "buy_ask": buy.ask,
                "sell_venue": d.sell_venue, "sell_bid": sell.bid,
                "gross_bps": to_bps(sell.bid / buy.ask - 1.0),
            },
        )
        opp_id = self.sink.opened(opp)
        self._active[d.key] = _Instance(opp_id, now, edge_bps)
        log.info("cross-exchange edge %s %.2f bps depth=%.0f USDT",
                 d.key, edge_bps, depth_quote,
                 extra={"extra_fields": {"route": d.key, "edge_bps": round(edge_bps, 3),
                                         "depth_quote": round(depth_quote, 2)}})

    def _close(self, d: _Direction, inst: _Instance, now: int) -> None:
        lifetime = max(0, now - inst.ts_open)
        self.sink.closed(inst.opp_id, lifetime, inst.peak_edge_bps)
        del self._active[d.key]
        log.debug("cross-exchange closed %s lifetime=%dms peak=%.2fbps",
                  d.key, lifetime, inst.peak_edge_bps)

    def close_all(self, now: int | None = None) -> None:
        now = now or now_ms()
        for key, inst in list(self._active.items()):
            self.sink.closed(inst.opp_id, max(0, now - inst.ts_open),
                             inst.peak_edge_bps)
            del self._active[key]
