"""Tests for the route walk and L1 depth capping used by the simulator."""
import pytest

from arbmon.execution.fill_model import (
    depth_limited_input,
    route_edge,
    route_final,
)


def _xexch_legs(fee_buy=0.001, fee_sell=0.006):
    # Buy BTCUSDT on binance (ask), sell BTC-USDT on coinbase (bid).
    return [
        {"action": "buy", "venue": "binance", "symbol": "BTCUSDT", "fee": fee_buy},
        {"action": "sell", "venue": "coinbase", "symbol": "BTC-USDT", "fee": fee_sell},
    ]


def test_route_final_round_trip_no_edge_loses_fees():
    legs = _xexch_legs(0.001, 0.001)
    tops = {"BTCUSDT": (100.0, 100.0), "BTC-USDT": (100.0, 100.0)}
    final = route_final(legs, 1000.0, tops)
    assert final == pytest.approx(1000.0 * (1 - 0.001) ** 2)


def test_route_edge_matches_cross_exchange_formula():
    legs = _xexch_legs(0.001, 0.006)
    # buy ask=100 on binance, sell bid=101 on coinbase
    tops = {"BTCUSDT": (99.9, 100.0), "BTC-USDT": (101.0, 101.1)}
    edge = route_edge(legs, tops)
    expected = (101.0 / 100.0) * (1 - 0.006) * (1 - 0.001) - 1.0
    assert edge == pytest.approx(expected)


def test_depth_cap_binds_on_smaller_side():
    legs = _xexch_legs(0.0, 0.0)
    tops = {"BTCUSDT": (100.0, 100.0), "BTC-USDT": (100.0, 100.0)}
    # Buy leg: ask_size 2 base -> 200 quote absorbable.
    # Sell leg: bid_size 0.5 base -> only 0.5 base can be sold.
    sizes = {"BTCUSDT": (0.0, 2.0), "BTC-USDT": (0.5, 0.0)}
    # Desired 1000 quote -> buys 10 base, but only 0.5 base sellable.
    # Binding is the sell leg at 0.5 base = 50 quote of input.
    filled, binding = depth_limited_input(legs, 1000.0, tops, sizes)
    assert binding == "BTC-USDT"
    assert filled == pytest.approx(50.0)


def test_depth_cap_no_binding_when_book_is_deep():
    legs = _xexch_legs(0.0, 0.0)
    tops = {"BTCUSDT": (100.0, 100.0), "BTC-USDT": (100.0, 100.0)}
    sizes = {"BTCUSDT": (0.0, 1000.0), "BTC-USDT": (1000.0, 0.0)}
    filled, binding = depth_limited_input(legs, 100.0, tops, sizes)
    assert binding is None
    assert filled == pytest.approx(100.0)


def test_depth_scaling_is_linear():
    legs = _xexch_legs(0.002, 0.002)
    tops = {"BTCUSDT": (99.0, 100.0), "BTC-USDT": (101.0, 102.0)}
    a = route_final(legs, 100.0, tops)
    b = route_final(legs, 200.0, tops)
    assert b == pytest.approx(2.0 * a)


def test_zero_desired_input():
    legs = _xexch_legs()
    filled, binding = depth_limited_input(legs, 0.0, {}, {})
    assert filled == 0.0 and binding is None
