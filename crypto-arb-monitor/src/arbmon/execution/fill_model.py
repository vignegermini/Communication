"""Pure route-walking against top-of-book, with L1 depth capping.

A 'route' is an ordered list of legs; each leg converts the amount it receives
into the next asset. Because every conversion (buy_base / sell_base) is LINEAR
in its input at fixed prices, scaling the starting amount scales every
intermediate amount by the same factor. That makes depth capping exact: find
the tightest per-leg fill ratio and scale the whole route by it.

Leg dict shape:
    {"action": "buy"|"sell", "symbol": <str>, "fee": <float>, ...}
`tops`  maps symbol -> (bid, ask).
`sizes` maps symbol -> (bid_size, ask_size) in base units.

LIMITATION (documented honestly): we only model L1 (best bid/ask and the size
resting there). We do NOT walk deeper levels. So a fill is capped at the top
size; we never simulate sweeping multiple price levels. Results are therefore
optimistic for orders larger than L1 depth — which is exactly why fills are
capped at L1 and the report shows fill sizes.
"""
from __future__ import annotations

from ..strategy.fees import buy_base, sell_base


def route_final(legs: list[dict], start_amount: float,
                tops: dict[str, tuple[float, float]]) -> float:
    """Amount of the terminal asset after walking `legs` from `start_amount`."""
    amount = start_amount
    for leg in legs:
        bid, ask = tops[leg["symbol"]]
        if leg["action"] == "buy":
            amount = buy_base(amount, ask, leg["fee"])
        elif leg["action"] == "sell":
            amount = sell_base(amount, bid, leg["fee"])
        else:
            raise ValueError(f"bad leg action: {leg['action']!r}")
    return amount


def route_edge(legs: list[dict],
               tops: dict[str, tuple[float, float]]) -> float:
    """Fractional round-trip edge for a unit start (final/1 - 1)."""
    return route_final(legs, 1.0, tops) - 1.0


def depth_limited_input(
    legs: list[dict],
    desired_input: float,
    tops: dict[str, tuple[float, float]],
    sizes: dict[str, tuple[float, float]],
) -> tuple[float, str | None]:
    """Largest input <= `desired_input` fillable within each leg's L1 depth.

    Returns (filled_input, binding_symbol). binding_symbol is the leg that
    limited the size, or None if the full desired input fits.
    """
    if desired_input <= 0:
        return 0.0, None
    amount = desired_input
    min_frac = 1.0
    binding: str | None = None
    for leg in legs:
        sym = leg["symbol"]
        bid, ask = tops[sym]
        bid_size, ask_size = sizes.get(sym, (0.0, 0.0))
        entering = amount  # input units for this leg
        if leg["action"] == "buy":
            cap = ask_size * ask          # max quote absorbable at the ask
            amount = buy_base(amount, ask, leg["fee"])
        else:
            cap = bid_size                # max base absorbable at the bid
            amount = sell_base(amount, bid, leg["fee"])
        frac = 1.0 if entering <= 0 else min(1.0, cap / entering)
        if frac < min_frac:
            min_frac = frac
            binding = sym
    return desired_input * min_frac, binding
