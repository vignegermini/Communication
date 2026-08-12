"""Triangular arbitrage enumeration and round-trip math on a single venue.

A triangle is a 3-leg cycle START -> X -> Y -> START where every leg is a real
market. Each leg is either:
    BUY  the leg's base using the asset we hold  (fill against the ASK), or
    SELL the asset we hold for the leg's quote   (fill against the BID).

Round-trip return is just the product of the three legs' (1 - fee) * price
factors, minus 1 — see fees.buy_base / fees.sell_base. All of it is pure and
covered by tests/test_triangles.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations, permutations

from .fees import buy_base, sell_base


@dataclass(frozen=True, slots=True)
class Market:
    """One tradable market on a venue."""
    symbol: str      # venue-native, e.g. "ETHBTC"
    base: str        # e.g. "ETH"
    quote: str       # e.g. "BTC"


@dataclass(frozen=True, slots=True)
class Leg:
    """One conversion in a triangle: turn `from_asset` into `to_asset`."""
    action: str          # "buy" (use ask) or "sell" (use bid)
    symbol: str
    from_asset: str
    to_asset: str


@dataclass(frozen=True, slots=True)
class Triangle:
    start: str
    legs: tuple[Leg, Leg, Leg]

    @property
    def symbols(self) -> tuple[str, str, str]:
        return tuple(leg.symbol for leg in self.legs)  # type: ignore[return-value]

    def describe(self) -> str:
        path = self.start
        for leg in self.legs:
            path += f" -{leg.action}:{leg.symbol}-> {leg.to_asset}"
        return path


def parse_symbol(symbol: str, assets: set[str]) -> Market | None:
    """Split a concatenated venue symbol (e.g. 'ETHBTC') into base/quote using
    the known asset set. Returns None if it isn't a market among `assets`.
    """
    for base in assets:
        if symbol.startswith(base):
            quote = symbol[len(base):]
            if quote in assets and quote != base:
                return Market(symbol=symbol, base=base, quote=quote)
    return None


def build_market_map(markets: list[Market]) -> dict[frozenset[str], Market]:
    """Index markets by the unordered asset pair they connect."""
    out: dict[frozenset[str], Market] = {}
    for m in markets:
        out[frozenset((m.base, m.quote))] = m
    return out


def _leg_for(from_asset: str, to_asset: str, m: Market) -> Leg:
    """Determine whether converting from->to is a buy or a sell on market `m`."""
    if m.base == to_asset and m.quote == from_asset:
        # We hold the quote and want the base -> BUY base (fill the ask).
        return Leg("buy", m.symbol, from_asset, to_asset)
    if m.base == from_asset and m.quote == to_asset:
        # We hold the base and want the quote -> SELL base (fill the bid).
        return Leg("sell", m.symbol, from_asset, to_asset)
    raise ValueError(f"market {m.symbol} does not connect {from_asset}->{to_asset}")


def enumerate_triangles(
    assets: list[str], markets: list[Market]
) -> list[Triangle]:
    """All 3-asset cycles whose every leg is a real market.

    For each unordered triple of assets with all three connecting markets
    present, both traversal directions are emitted (they use different
    bid/ask sides and generally have different returns). Rotations of the same
    direction are collapsed by always starting at the sorted-first asset, so
    each (triple, direction) appears exactly once.
    """
    mmap = build_market_map(markets)
    triangles: list[Triangle] = []
    for triple in combinations(sorted(set(assets)), 3):
        # both directions, anchored so start = triple[0] (avoids 3x rotations)
        for ordering in permutations(triple):
            if ordering[0] != triple[0]:
                continue
            a, b, c = ordering
            pairs = [frozenset((a, b)), frozenset((b, c)), frozenset((c, a))]
            if not all(p in mmap for p in pairs):
                continue
            try:
                legs = (
                    _leg_for(a, b, mmap[pairs[0]]),
                    _leg_for(b, c, mmap[pairs[1]]),
                    _leg_for(c, a, mmap[pairs[2]]),
                )
            except ValueError:
                continue
            triangles.append(Triangle(start=a, legs=legs))
    return triangles


def triangle_return(
    legs: tuple[Leg, Leg, Leg],
    quotes: dict[str, tuple[float, float]],
    fee: float,
) -> float:
    """Fractional round-trip return starting from 1 unit of the start asset.

    `quotes` maps each leg symbol to (bid, ask). Buys fill the ask, sells fill
    the bid. Return is (final_amount - 1). Raises KeyError if a quote is missing.
    """
    amount = 1.0
    for leg in legs:
        bid, ask = quotes[leg.symbol]
        if leg.action == "buy":
            amount = buy_base(amount, ask, fee)
        elif leg.action == "sell":
            amount = sell_base(amount, bid, fee)
        else:  # pragma: no cover - guarded by construction
            raise ValueError(f"bad leg action {leg.action!r}")
    return amount - 1.0
