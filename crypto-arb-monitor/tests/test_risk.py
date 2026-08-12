"""Tests for the risk brakes: kill switch, daily loss halt, per-pair cap."""
from arbmon.execution.paper_portfolio import PaperPortfolio
from arbmon.execution.risk import RiskManager


def _mgr(tmp_path, **kw):
    pf = PaperPortfolio(starting_notional_quote=100_000.0)
    defaults = dict(
        kill_switch_file=str(tmp_path / "KILL"),
        max_position_per_pair_quote=5_000.0,
        max_daily_sim_loss_quote=1_000.0,
    )
    defaults.update(kw)
    return pf, RiskManager(pf, **defaults)


def test_allows_normal_trade(tmp_path):
    _, r = _mgr(tmp_path)
    ok, reason = r.can_open("BTC", 1_000.0)
    assert ok and reason == ""


def test_kill_switch_blocks_and_halts(tmp_path):
    _, r = _mgr(tmp_path)
    (tmp_path / "KILL").write_text("stop")
    ok, reason = r.can_open("BTC", 100.0)
    assert not ok and reason == "kill_switch"
    assert r.halted


def test_per_pair_cap_enforced(tmp_path):
    _, r = _mgr(tmp_path, max_position_per_pair_quote=1_000.0)
    ok, reason = r.can_open("BTC", 1_500.0)
    assert not ok and reason == "max_position_per_pair"
    ok, _ = r.can_open("BTC", 1_000.0)
    assert ok


def test_daily_loss_halts(tmp_path):
    pf, r = _mgr(tmp_path, max_daily_sim_loss_quote=500.0)
    # Simulate cumulative loss beyond the limit.
    pf.realized_sim_pnl_quote = -600.0
    ok, reason = r.can_open("BTC", 100.0)
    assert not ok and reason == "daily_loss_limit"
    assert r.halted


def test_day_roll_clears_daily_halt(tmp_path):
    pf, r = _mgr(tmp_path, max_daily_sim_loss_quote=500.0)
    pf.realized_sim_pnl_quote = -600.0
    r.can_open("BTC", 100.0)  # trips halt
    assert r.halted
    r.roll_day()             # new day, baseline resets
    assert not r.halted
    ok, _ = r.can_open("BTC", 100.0)
    assert ok


def test_daily_pnl_measured_from_baseline(tmp_path):
    pf, r = _mgr(tmp_path, max_daily_sim_loss_quote=500.0)
    pf.realized_sim_pnl_quote = -400.0
    r.roll_day()  # baseline now -400; today's pnl = 0
    assert r.daily_sim_pnl() == 0.0
    pf.realized_sim_pnl_quote = -800.0  # lost 400 today, under 500 limit
    ok, _ = r.can_open("BTC", 100.0)
    assert ok
    pf.realized_sim_pnl_quote = -950.0  # lost 550 today, over limit
    ok, reason = r.can_open("BTC", 100.0)
    assert not ok and reason == "daily_loss_limit"
