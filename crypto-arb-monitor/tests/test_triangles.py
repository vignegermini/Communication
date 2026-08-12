"""Tests for triangle enumeration and round-trip math."""
import pytest

from arbmon.strategy.triangles import (
    Leg,
    Market,
    build_market_map,
    enumerate_triangles,
    parse_symbol,
    triangle_return,
)

ASSETS = ["BTC", "ETH", "USDT", "BNB"]
MARKETS = [
    Market("BTCUSDT", "BTC", "USDT"),
    Market("ETHUSDT", "ETH", "USDT"),
    Market("BNBUSDT", "BNB", "USDT"),
    Market("ETHBTC", "ETH", "BTC"),
    Market("BNBBTC", "BNB", "BTC"),
    Market("BNBETH", "BNB", "ETH"),
]


def test_parse_symbol_splits_known_assets():
    m = parse_symbol("ETHBTC", set(ASSETS))
    assert m == Market("ETHBTC", "ETH", "BTC")


def test_parse_symbol_rejects_unknown_quote():
    assert parse_symbol("ETHFOO", set(ASSETS)) is None


def test_parse_symbol_rejects_non_market():
    assert parse_symbol("DOGEUSDT", set(ASSETS)) is None


def test_market_map_indexes_by_pair():
    mmap = build_market_map(MARKETS)
    assert mmap[frozenset(("ETH", "BTC"))].symbol == "ETHBTC"
    assert mmap[frozenset(("BTC", "USDT"))].symbol == "BTCUSDT"


def test_enumerate_counts_directions():
    # 4 assets choose 3 = 4 triples. Each of our triples has all 3 markets, and
    # each yields 2 directions -> 8 triangles.
    tris = enumerate_triangles(ASSETS, MARKETS)
    assert len(tris) == 8


def test_enumerate_skips_incomplete_triples():
    # Drop BNBETH -> the {ETH,BNB,*} triangles needing it lose a leg.
    partial = [m for m in MARKETS if m.symbol != "BNBETH"]
    tris = enumerate_triangles(ASSETS, partial)
    # Triple {BTC,ETH,BNB} needs ETHBTC, BNBBTC, BNBETH; BNBETH gone -> 0 for it.
    # Triple {ETH,USDT,BNB} needs ETHUSDT, BNBUSDT, BNBETH; gone -> 0 for it.
    # Remaining fully-connected triples: {BTC,ETH,USDT}, {BTC,USDT,BNB} -> 2 each.
    assert len(tris) == 4


def test_triangle_return_is_zero_at_consistent_prices_no_fee():
    # Construct perfectly consistent prices so a no-fee round trip nets ~0.
    # Let 1 BTC = 20000 USDT, 1 ETH = 1000 USDT, so ETH/BTC = 0.05.
    quotes = {
        "BTCUSDT": (20000.0, 20000.0),
        "ETHUSDT": (1000.0, 1000.0),
        "ETHBTC": (0.05, 0.05),
        "BNBUSDT": (300.0, 300.0),
        "BNBBTC": (0.015, 0.015),
        "BNBETH": (0.3, 0.3),
    }
    for tri in enumerate_triangles(ASSETS, MARKETS):
        r = triangle_return(tri.legs, quotes, fee=0.0)
        assert r == pytest.approx(0.0, abs=1e-12)


def test_triangle_return_negative_once_fees_applied_on_consistent_prices():
    quotes = {
        "BTCUSDT": (20000.0, 20000.0),
        "ETHUSDT": (1000.0, 1000.0),
        "ETHBTC": (0.05, 0.05),
        "BNBUSDT": (300.0, 300.0),
        "BNBBTC": (0.015, 0.015),
        "BNBETH": (0.3, 0.3),
    }
    for tri in enumerate_triangles(ASSETS, MARKETS):
        r = triangle_return(tri.legs, quotes, fee=0.001)
        # Three legs of 0.1% -> about -0.3% on consistent prices.
        assert r < 0.0
        assert r == pytest.approx((1 - 0.001) ** 3 - 1.0, rel=1e-6)


def test_triangle_return_detects_real_edge():
    # Dislocate ETHBTC so ETH is cheap in BTC terms -> a genuine cycle profit.
    # Consistent would be 0.05; make the ask 0.0490 (ETH cheap when buying with BTC).
    quotes = {
        "BTCUSDT": (20000.0, 20000.0),
        "ETHUSDT": (1000.0, 1000.0),
        "ETHBTC": (0.0490, 0.0490),
        "BNBUSDT": (300.0, 300.0),
        "BNBBTC": (0.015, 0.015),
        "BNBETH": (0.3, 0.3),
    }
    # Route USDT->BTC->ETH->USDT should now be profitable even with fees:
    # buy BTC with USDT, buy ETH with BTC (cheap), sell ETH for USDT.
    best = max(
        triangle_return(t.legs, quotes, fee=0.001)
        for t in enumerate_triangles(ASSETS, MARKETS)
    )
    assert best > 0.0


def test_leg_direction_buy_vs_sell():
    mmap = build_market_map(MARKETS)
    tris = enumerate_triangles(ASSETS, MARKETS)
    # Find the triangle starting at BTC over {BTC,ETH,USDT}.
    tri = next(
        t for t in tris
        if t.start == "BTC" and set(l.symbol for l in t.legs) == {"BTCUSDT", "ETHUSDT", "ETHBTC"}
    )
    # First leg BTC->? : depends on ordering, just assert each leg is coherent.
    for leg in tri.legs:
        m = mmap[frozenset((leg.from_asset, leg.to_asset))]
        if leg.action == "buy":
            assert m.base == leg.to_asset and m.quote == leg.from_asset
        else:
            assert m.base == leg.from_asset and m.quote == leg.to_asset


def test_triangle_return_missing_quote_raises():
    with pytest.raises(KeyError):
        triangle_return(
            (Leg("buy", "XXXYYY", "YYY", "XXX"),),  # type: ignore[arg-type]
            {},
            fee=0.0,
        )
