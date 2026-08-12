"""Hard risk limits and the kill switch.

Three independent brakes, any of which halts strategy execution:
  1. Kill-switch file: if it exists on disk, everything stops immediately.
  2. Max simulated daily loss: cumulative simulated PnL below -limit halts.
  3. Max position per pair: a route cannot exceed its per-pair exposure cap.

'Halt' means: stop opening new simulated positions and stop scheduling new
fills. Data capture continues so you still have the record.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .paper_portfolio import PaperPortfolio

log = logging.getLogger(__name__)


class RiskManager:
    def __init__(
        self,
        portfolio: PaperPortfolio,
        *,
        kill_switch_file: str,
        max_position_per_pair_quote: float,
        max_daily_sim_loss_quote: float,
        halt_on_breach: bool = True,
    ) -> None:
        self.portfolio = portfolio
        self.kill_switch_file = Path(kill_switch_file)
        self.max_position_per_pair_quote = float(max_position_per_pair_quote)
        self.max_daily_sim_loss_quote = float(max_daily_sim_loss_quote)
        self.halt_on_breach = halt_on_breach
        self._halted = False
        self._halt_reason = ""
        self._day_start_pnl = 0.0  # realized sim pnl at start of the UTC day

    # -- kill switch ----------------------------------------------------------
    def kill_switch_present(self) -> bool:
        return self.kill_switch_file.exists()

    # -- daily loss -----------------------------------------------------------
    def daily_sim_pnl(self) -> float:
        return self.portfolio.realized_sim_pnl_quote - self._day_start_pnl

    def roll_day(self) -> None:
        """Reset the daily-loss baseline (call at UTC midnight)."""
        self._day_start_pnl = self.portfolio.realized_sim_pnl_quote
        if self._halted and self._halt_reason.startswith("daily_loss"):
            self._halted = False
            self._halt_reason = ""
            log.warning("daily loss halt cleared at day roll")

    # -- gate -----------------------------------------------------------------
    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    def _halt(self, reason: str) -> None:
        if not self._halted:
            self._halted = True
            self._halt_reason = reason
            log.error("STRATEGY HALTED: %s", reason)

    def can_open(self, route_key: str, intended_quote: float) -> tuple[bool, str]:
        """May we deploy `intended_quote` into one opportunity on `route_key`?

        An arbitrage round trip is flat once complete, so the per-pair limit
        bounds the notional of a SINGLE opportunity rather than a growing
        inventory. The kill switch and daily-loss brakes are hard halts.
        """
        if self.kill_switch_present():
            self._halt("kill_switch_file_present")
            return False, "kill_switch"
        if self.halt_on_breach and self.daily_sim_pnl() <= -self.max_daily_sim_loss_quote:
            self._halt(f"daily_loss_limit ({self.daily_sim_pnl():.2f} <= "
                       f"-{self.max_daily_sim_loss_quote:.2f})")
            return False, "daily_loss_limit"
        if self._halted:
            return False, self._halt_reason
        if intended_quote > self.max_position_per_pair_quote:
            return False, "max_position_per_pair"
        return True, ""
