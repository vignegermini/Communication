"""Daily summary report.

Answers the only questions that matter for 'do exploitable inefficiencies exist
at retail speed?':
  - How many THEORETICAL opportunities appeared?
  - How many SURVIVED the latency simulation (still profitable 250ms later)?
  - What is the net SIMULATED PnL after fees and slippage vs. the theoretical?
  - What is the distribution of opportunity lifetimes in milliseconds?

Reads the SQLite database directly (read-only), so it can run while a capture
is still going. `python -m arbmon.reporting.daily_report data/arbmon.sqlite`.
"""
from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass, field

from ..models import now_ms


def _pct(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = q * (len(sorted_vals) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


@dataclass
class Report:
    since_ms: int
    until_ms: int
    by_type: dict = field(default_factory=dict)
    total_theoretical: int = 0
    total_survived: int = 0
    survival_rate: float = 0.0
    theo_pnl_quote: float = 0.0
    sim_pnl_quote: float = 0.0
    avg_slippage_bps: float = 0.0
    lifetime_ms: dict = field(default_factory=dict)
    data_first_ts: int = 0
    data_last_ts: int = 0

    def to_text(self) -> str:
        L = self.lifetime_ms
        if self.data_first_ts and self.data_last_ts:
            span_h = (self.data_last_ts - self.data_first_ts) / 3.6e6
            window = (f"data spans {span_h:.2f} h "
                      f"({self.data_first_ts} .. {self.data_last_ts})")
        else:
            window = "no opportunities in window"
        lines = [
            "=" * 68,
            "  arbmon daily summary",
            f"  {window}",
            "=" * 68,
            "",
            "  THEORETICAL OPPORTUNITIES (top-of-book, net of fees)",
        ]
        for t, c in sorted(self.by_type.items()):
            lines.append(f"    {t:<16} {c['theoretical']:>8} detected   "
                         f"{c['survived']:>8} survived 250ms")
        lines += [
            f"    {'TOTAL':<16} {self.total_theoretical:>8} detected   "
            f"{self.total_survived:>8} survived",
            "",
            f"  SURVIVAL RATE (still profitable after latency): "
            f"{self.survival_rate * 100:.2f}%",
            "",
            "  SIMULATED PnL (USDT)",
            f"    theoretical (at detection prices) : {self.theo_pnl_quote:>14.2f}",
            f"    simulated   (after 250ms + slip)  : {self.sim_pnl_quote:>14.2f}",
            f"    latency cost                      : "
            f"{self.theo_pnl_quote - self.sim_pnl_quote:>14.2f}",
            f"    avg slippage (edge decay)         : "
            f"{self.avg_slippage_bps:>10.2f} bps",
            "",
            "  OPPORTUNITY LIFETIME DISTRIBUTION (ms)",
            f"    count={L.get('count', 0)}  min={L.get('min', 0):.0f}  "
            f"p50={L.get('p50', 0):.0f}  p90={L.get('p90', 0):.0f}  "
            f"p99={L.get('p99', 0):.0f}  max={L.get('max', 0):.0f}  "
            f"mean={L.get('mean', 0):.0f}",
            "=" * 68,
        ]
        return "\n".join(lines)


def generate_report(db_path: str, since_ms: int = 0,
                    until_ms: int | None = None) -> Report:
    if until_ms is None:
        until_ms = now_ms()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rep = Report(since_ms=since_ms, until_ms=until_ms)

        rows = conn.execute(
            "SELECT opp_type, COUNT(*) n FROM opportunities "
            "WHERE ts_detect BETWEEN ? AND ? GROUP BY opp_type",
            (since_ms, until_ms),
        ).fetchall()
        for r in rows:
            rep.by_type[r["opp_type"]] = {"theoretical": r["n"], "survived": 0}
            rep.total_theoretical += r["n"]

        surv = conn.execute(
            "SELECT o.opp_type ot, COUNT(*) n FROM sim_fills f "
            "JOIN opportunities o ON o.id = f.opp_id "
            "WHERE f.survived=1 AND f.ts_detect BETWEEN ? AND ? GROUP BY o.opp_type",
            (since_ms, until_ms),
        ).fetchall()
        for r in surv:
            rep.by_type.setdefault(r["ot"], {"theoretical": 0, "survived": 0})
            rep.by_type[r["ot"]]["survived"] = r["n"]
            rep.total_survived += r["n"]

        rep.survival_rate = (
            rep.total_survived / rep.total_theoretical
            if rep.total_theoretical else 0.0
        )

        agg = conn.execute(
            "SELECT COALESCE(SUM(theo_pnl_quote),0) theo, "
            "COALESCE(SUM(sim_pnl_quote),0) sim, "
            "COALESCE(AVG(slippage_bps),0) slip FROM sim_fills "
            "WHERE ts_detect BETWEEN ? AND ?",
            (since_ms, until_ms),
        ).fetchone()
        rep.theo_pnl_quote = agg["theo"]
        rep.sim_pnl_quote = agg["sim"]
        rep.avg_slippage_bps = agg["slip"]

        lifetimes = [
            row["lifetime_ms"] for row in conn.execute(
                "SELECT lifetime_ms FROM opportunities "
                "WHERE lifetime_ms IS NOT NULL AND ts_detect BETWEEN ? AND ? "
                "ORDER BY lifetime_ms", (since_ms, until_ms),
            ).fetchall()
        ]
        if lifetimes:
            rep.lifetime_ms = {
                "count": len(lifetimes),
                "min": float(lifetimes[0]),
                "max": float(lifetimes[-1]),
                "mean": sum(lifetimes) / len(lifetimes),
                "p50": _pct(lifetimes, 0.50),
                "p90": _pct(lifetimes, 0.90),
                "p99": _pct(lifetimes, 0.99),
            }
        else:
            rep.lifetime_ms = {"count": 0}

        span = conn.execute(
            "SELECT MIN(ts_detect) lo, MAX(ts_detect) hi FROM opportunities "
            "WHERE ts_detect BETWEEN ? AND ?", (since_ms, until_ms),
        ).fetchone()
        rep.data_first_ts = span["lo"] or 0
        rep.data_last_ts = span["hi"] or 0
        return rep
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: python -m arbmon.reporting.daily_report <db_path> "
              "[since_ms] [until_ms]", file=sys.stderr)
        return 2
    db = argv[0]
    since = int(argv[1]) if len(argv) > 1 else 0
    until = int(argv[2]) if len(argv) > 2 else None
    print(generate_report(db, since, until).to_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
