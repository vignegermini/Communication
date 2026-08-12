"""Core data structures passed between the data, strategy, and execution layers.

Everything is a plain dataclass so it is trivially serialisable and testable.
Timestamps are epoch milliseconds (int) throughout — one clock, no timezone
ambiguity, and directly comparable for latency math.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


def now_ms() -> int:
    """Wall-clock epoch milliseconds. The single time source for the system."""
    return int(time.time() * 1000)


class Venue(str, Enum):
    BINANCE = "binance"
    COINBASE = "coinbase"


@dataclass(slots=True, frozen=True)
class BookTop:
    """Top of book (L1) for one symbol on one venue at one instant.

    `bid`/`ask` are prices; `bid_size`/`ask_size` are the quantities resting at
    those prices (in base units). This is exactly what the venue `bookTicker` /
    `ticker` streams give us, and it is the unit of replay for the latency sim.
    """
    venue: str
    symbol: str          # venue-native symbol, e.g. "BTCUSDT" or "BTC-USDT"
    bid: float
    bid_size: float
    ask: float
    ask_size: float
    ts_event: int        # venue event time (ms) if provided, else recv time
    ts_recv: int         # local receive time (ms)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(slots=True, frozen=True)
class Trade:
    venue: str
    symbol: str
    price: float
    qty: float
    is_buyer_maker: bool
    ts_event: int
    ts_recv: int


class OppType(str, Enum):
    CROSS_EXCHANGE = "cross_exchange"
    TRIANGULAR = "triangular"


@dataclass(slots=True)
class Opportunity:
    """A theoretical, top-of-book profit opportunity at the moment of detection.

    `edge_bps` is net of all relevant taker fees. `legs` describes the route in
    a venue-agnostic way so the simulator can replay it. `notional_quote` is the
    size we *intend* to trade; `depth_quote` is how much the book could actually
    absorb at the detected prices (min across legs). Theoretical PnL assumes the
    detected prices; the simulator computes the latency-adjusted PnL separately.
    """
    opp_type: str
    ts_detect: int
    edge_bps: float                 # net edge in basis points (after fees)
    notional_quote: float           # intended trade size, quote units
    depth_quote: float              # size actually available at detected prices
    theo_pnl_quote: float           # theoretical PnL at detected top-of-book
    legs: list[dict] = field(default_factory=list)
    detail: dict = field(default_factory=dict)  # human-readable context
    id: int | None = None           # assigned by storage on insert


@dataclass(slots=True)
class SimFill:
    """Result of replaying an opportunity `latency_ms` after detection."""
    opp_id: int | None
    ts_detect: int
    ts_fill: int                    # ts_detect + latency_ms (the replay instant)
    latency_ms: int
    survived: bool                  # was the edge still positive at fill time?
    filled_quote: float             # size actually fillable at fill time
    theo_pnl_quote: float           # carried from the opportunity
    sim_pnl_quote: float            # realised PnL after slippage + fees
    slippage_bps: float             # adverse move vs. detected prices
    reason: str = ""                # why it did/didn't survive
