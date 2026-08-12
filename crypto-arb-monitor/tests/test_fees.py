"""Exhaustive tests for the fee math. These are the calculations that decide
whether an 'opportunity' is real, so they are pinned down precisely.
"""
import math

import pytest

from arbmon.strategy.fees import (
    buy_base,
    cross_exchange_return,
    from_bps,
    sell_base,
    to_bps,
)


def test_buy_base_zero_fee_is_exact_division():
    # Spend 100 quote at ask 50 with no fee -> exactly 2 base.
    assert buy_base(100.0, 50.0, 0.0) == pytest.approx(2.0)


def test_sell_base_zero_fee_is_exact_multiplication():
    # Sell 2 base at bid 50 with no fee -> exactly 100 quote.
    assert sell_base(2.0, 50.0, 0.0) == pytest.approx(100.0)


def test_buy_base_applies_fee_to_output():
    # 0.1% fee: receive 2 * 0.999 base.
    assert buy_base(100.0, 50.0, 0.001) == pytest.approx(2.0 * 0.999)


def test_sell_base_applies_fee_to_output():
    assert sell_base(2.0, 50.0, 0.001) == pytest.approx(100.0 * 0.999)


def test_buy_then_sell_same_price_loses_only_fees():
    # Round trip at an unchanged price should lose exactly the two fee haircuts.
    fee = 0.001
    base = buy_base(1000.0, 25.0, fee)
    quote_back = sell_base(base, 25.0, fee)
    # (1 - fee)^2 of the original notional.
    assert quote_back == pytest.approx(1000.0 * (1 - fee) ** 2)
    assert quote_back < 1000.0


def test_cross_exchange_return_matches_closed_form():
    bid_a, ask_b, fee_a, fee_b = 101.0, 100.0, 0.001, 0.006
    expected = (bid_a / ask_b) * (1 - fee_a) * (1 - fee_b) - 1.0
    assert cross_exchange_return(bid_a, ask_b, fee_a, fee_b) == pytest.approx(expected)


def test_cross_exchange_no_edge_when_prices_equal():
    # Equal bid/ask with positive fees can never be profitable.
    assert cross_exchange_return(100.0, 100.0, 0.001, 0.006) < 0.0


def test_cross_exchange_break_even_threshold():
    # With default fees (0.1% + 0.6% = 0.7%), the raw bid/ask ratio must exceed
    # 1/((1-0.001)(1-0.006)) for a positive net return.
    fee_a, fee_b = 0.001, 0.006
    break_even_ratio = 1.0 / ((1 - fee_a) * (1 - fee_b))
    ask_b = 100.0
    # Just below break-even -> negative.
    bid_just_under = ask_b * break_even_ratio * 0.9999
    assert cross_exchange_return(bid_just_under, ask_b, fee_a, fee_b) < 0.0
    # Just above break-even -> positive.
    bid_just_over = ask_b * break_even_ratio * 1.0001
    assert cross_exchange_return(bid_just_over, ask_b, fee_a, fee_b) > 0.0


def test_cross_exchange_is_gross_minus_fees_direction():
    # A 2% gross gap net of 0.7% fees is still comfortably positive.
    r = cross_exchange_return(102.0, 100.0, 0.001, 0.006)
    assert r > 0.0
    # And smaller than the naive gross gap.
    assert r < 0.02


def test_bps_roundtrip():
    assert to_bps(0.001) == pytest.approx(10.0)
    assert from_bps(10.0) == pytest.approx(0.001)
    assert from_bps(to_bps(0.00037)) == pytest.approx(0.00037)


@pytest.mark.parametrize("bad_fee", [-0.1, 1.0, 1.5])
def test_fee_out_of_range_rejected(bad_fee):
    with pytest.raises(ValueError):
        buy_base(100.0, 50.0, bad_fee)


@pytest.mark.parametrize("bad_price", [0.0, -1.0])
def test_nonpositive_price_rejected(bad_price):
    with pytest.raises(ValueError):
        buy_base(100.0, bad_price, 0.001)
    with pytest.raises(ValueError):
        sell_base(1.0, bad_price, 0.001)


def test_fee_haircut_is_multiplicative_across_symmetry():
    # buy_base and sell_base are inverses up to the squared fee factor.
    fee = 0.0025
    q0 = 500.0
    price = 12.34
    base = buy_base(q0, price, fee)
    q1 = sell_base(base, price, fee)
    assert q1 / q0 == pytest.approx((1 - fee) ** 2)
