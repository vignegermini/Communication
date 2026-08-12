"""Fee-aware trade math. This module is the source of truth for every profit
calculation in the system and is covered exhaustively by tests/test_fees.py.

CONVENTION (used everywhere in arbmon, chosen for realism and consistency):
    A taker trade's OUTPUT asset is reduced by the fee. If you convert an input
    amount into an output amount at some price, you receive
        output * (1 - fee)
    where `fee` is a fraction (0.001 == 0.10%). This matches how Binance and
    Coinbase actually deduct taker fees (from the asset you receive), and it
    composes cleanly across multiple legs: each leg simply carries its own
    (1 - fee) haircut, so an N-leg route multiplies N such factors.

Two primitives cover every trade:
    buy_base  — spend QUOTE, receive BASE, filling against the ASK.
    sell_base — spend BASE, receive QUOTE, filling against the BID.

`bps` helpers express edges in basis points (1 bps = 0.01% = 1e-4).
"""
from __future__ import annotations


def _check_fee(fee: float) -> None:
    if not (0.0 <= fee < 1.0):
        raise ValueError(f"fee must be in [0, 1), got {fee!r}")


def _check_price(price: float) -> None:
    if price <= 0.0:
        raise ValueError(f"price must be > 0, got {price!r}")


def buy_base(quote_in: float, ask_price: float, fee: float) -> float:
    """Spend `quote_in` of quote currency at `ask_price`; return BASE received,
    net of taker `fee`.

        base_received = (quote_in / ask_price) * (1 - fee)
    """
    _check_price(ask_price)
    _check_fee(fee)
    if quote_in < 0.0:
        raise ValueError("quote_in must be >= 0")
    return (quote_in / ask_price) * (1.0 - fee)


def sell_base(base_in: float, bid_price: float, fee: float) -> float:
    """Sell `base_in` of base currency at `bid_price`; return QUOTE received,
    net of taker `fee`.

        quote_received = (base_in * bid_price) * (1 - fee)
    """
    _check_price(bid_price)
    _check_fee(fee)
    if base_in < 0.0:
        raise ValueError("base_in must be >= 0")
    return (base_in * bid_price) * (1.0 - fee)


def cross_exchange_return(
    bid_a: float, ask_b: float, fee_a: float, fee_b: float
) -> float:
    """Fractional return of the round trip: BUY on venue B at `ask_b`, then
    SELL on venue A at `bid_a`. Two trades → two fee haircuts.

    Starting with 1 unit of quote:
        base  = buy_base(1, ask_b, fee_b) = (1 / ask_b) * (1 - fee_b)
        quote = sell_base(base, bid_a, fee_a) = base * bid_a * (1 - fee_a)
              = (bid_a / ask_b) * (1 - fee_a) * (1 - fee_b)

    Return = final_quote - 1. Positive means the bid on A exceeds the ask on B
    by more than both venues' taker fees combined — a real, net-of-fees edge.
    """
    _check_price(bid_a)
    _check_price(ask_b)
    _check_fee(fee_a)
    _check_fee(fee_b)
    final_quote = (bid_a / ask_b) * (1.0 - fee_a) * (1.0 - fee_b)
    return final_quote - 1.0


def to_bps(fraction: float) -> float:
    """Convert a fractional return (0.001) to basis points (10.0)."""
    return fraction * 10_000.0


def from_bps(bps: float) -> float:
    """Convert basis points (10.0) to a fraction (0.001)."""
    return bps / 10_000.0
