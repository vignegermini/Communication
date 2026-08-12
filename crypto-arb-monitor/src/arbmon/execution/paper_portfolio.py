"""Paper portfolio accounting. Tracks simulated PnL only — there is no real
balance and no order endpoint anywhere near this class.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PaperPortfolio:
    starting_notional_quote: float
    realized_sim_pnl_quote: float = 0.0
    theo_pnl_quote: float = 0.0
    n_fills: int = 0
    n_survived: int = 0
    # simulated exposure currently attributed per pair/route (quote units)
    exposure: dict[str, float] = field(default_factory=dict)

    @property
    def equity_quote(self) -> float:
        return self.starting_notional_quote + self.realized_sim_pnl_quote

    def apply_fill(self, route_key: str, sim_pnl_quote: float,
                   theo_pnl_quote: float, filled_quote: float,
                   survived: bool) -> None:
        self.realized_sim_pnl_quote += sim_pnl_quote
        self.theo_pnl_quote += theo_pnl_quote
        self.n_fills += 1
        if survived:
            self.n_survived += 1
        self.exposure[route_key] = self.exposure.get(route_key, 0.0) + filled_quote

    def snapshot(self) -> dict:
        return {
            "starting_notional_quote": self.starting_notional_quote,
            "equity_quote": self.equity_quote,
            "realized_sim_pnl_quote": self.realized_sim_pnl_quote,
            "theo_pnl_quote": self.theo_pnl_quote,
            "n_fills": self.n_fills,
            "n_survived": self.n_survived,
        }
