# arbmon — crypto market monitor & paper-trading (detection only)

A read-only research tool that answers **one** question with real data before
any capital is at risk:

> *Do exploitable pricing inefficiencies exist at retail speed — after fees,
> after latency, after slippage?*

It watches live order-book tops and trades from **Binance** and a second venue
(**Coinbase**), detects two classes of opportunity (cross-exchange spreads and
Binance triangular arbitrage), and then **simulates** what would actually have
happened if your order arrived 250 ms after you spotted the edge. Theoretical
PnL and latency-adjusted PnL are always reported as **separate numbers**, so the
cost of being a retail participant is never hidden.

There are **no order endpoints anywhere in this codebase.** It cannot place,
cancel, or sign a trade. See [Safety](#safety).

---

## Why the honest framing matters (read this first)

The gap between "an arbitrage existed" and "I could have captured it" is where
almost all retail crypto-arb strategies die. This tool is built to *measure that
gap*, not to sell you on a strategy. Concretely:

- A **theoretical** opportunity is a top-of-book price relationship that is
  profitable net of fees at the instant it appears.
- A **surviving** opportunity is one that is *still* profitable 250 ms later,
  when your order would realistically hit the book.
- The headline you should care about is the **survival rate** and the
  **simulated PnL after latency and slippage** — not the theoretical count.

In the offline demo shipped here, injected cross-exchange edges show theoretical
PnL that turns **negative** once the 250 ms latency is applied, because the edge
reverts before the order lands. That is the normal, expected result for
retail-speed cross-exchange arb between high-fee venues. If your real 24 h
capture shows the same thing, the system is telling you the truth: **don't trade
it.**

---

## Architecture

Python 3.11+, asyncio, single process, clean layer separation:

```
src/arbmon/
  models.py            # dataclasses shared across layers (ms timestamps)
  config.py            # config load/validate + API-key WITHDRAWAL SAFETY GATE
  logging_conf.py      # structured (JSON) logging
  data/                # DATA LAYER
    binance_ws.py      #   Binance public bookTicker + trade streams
    coinbase_ws.py     #   Coinbase public ticker stream (2nd venue)
    book_state.py      #   in-memory current top-of-book
    reconnect.py       #   exponential-backoff reconnection
  strategy/            # STRATEGY LAYER (detection only)
    fees.py            #   fee math  (provably correct — tested)
    triangles.py       #   triangle enumeration + math (provably correct — tested)
    cross_exchange.py  #   cross-exchange spread monitor
    triangular.py      #   Binance triangular scanner
  execution/           # EXECUTION LAYER (paper only)
    fill_model.py      #   route walk + L1 depth capping (tested)
    simulator.py       #   250 ms latency replay + slippage
    paper_portfolio.py #   simulated PnL accounting
    risk.py            #   kill switch + daily-loss halt + per-pair cap
  storage/db.py        # STORAGE  (SQLite: ticks, opportunities, sim fills)
  reporting/
    daily_report.py    #   daily summary report
    dashboard.py       #   single-page auto-refresh web dashboard
  app.py               # orchestrator
scripts/demo.py        # offline end-to-end demo (no network)
tests/                 # fee math, triangle math, fill model, risk, config, e2e
```

Data flow: `ws client → BookStore + SQLite → strategy monitors → Opportunity →
LatencySimulator (sleep 250 ms, refill against current book) → SimFill → SQLite
→ report / dashboard`.

---

## Install

```bash
cd crypto-arb-monitor
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # or: pip install -e ".[dev]"
```

## Configure

```bash
cp config.example.yaml config.yaml
# edit fees, symbols, latency, risk limits, dashboard port…
```

Everything is parameterized: taker fees per venue (default Binance 0.10 %,
Coinbase 0.60 %), the 20 Binance symbols, latency (default 250 ms), per-pair and
daily-loss risk limits, and the kill-switch filename.

## Run (live capture)

```bash
python -m arbmon.app --config config.yaml
```

Open the dashboard at `http://127.0.0.1:8787`. Stop any time with `Ctrl-C`, or
instantly halt **everything** by creating the kill-switch file:

```bash
touch KILL          # matches run.kill_switch_file in config
```

### The 24-hour capture

Set `run.duration_seconds: 86400` in `config.yaml` and run it for a full day
(ideally under a process supervisor / `tmux` / `systemd`). The websocket layer
reconnects automatically across the inevitable disconnects (Binance force-closes
every 24 h). Then produce the summary:

```bash
python -m arbmon.reporting.daily_report data/arbmon.sqlite
```

## Try it now without network — offline demo

Exchanges may be unreachable in some environments. This runs the **exact same**
monitors, simulator, storage and report against a synthetic feed:

```bash
python scripts/demo.py --seconds 20            # prints a populated report
python scripts/demo.py --seconds 20 --dashboard  # + live dashboard, stays up
```

---

## Reading the daily report honestly

```
  THEORETICAL OPPORTUNITIES (top-of-book, net of fees)
    cross_exchange         14 detected          0 survived 250ms
    triangular             90 detected         43 survived 250ms
  SURVIVAL RATE (still profitable after latency): 41.35%
  SIMULATED PnL (USDT)
    theoretical (at detection prices) :          43.13
    simulated   (after 250ms + slip)  :         -89.27   <-- the number that matters
    latency cost                      :         132.39
```

- **detected** — a net-of-fees edge existed at top of book. Cheap to find; means
  little on its own.
- **survived 250 ms** — the edge was *still* net-positive when your order would
  arrive. This is the real hit rate.
- **theoretical vs simulated PnL** — if simulated is far below theoretical (or
  negative), latency and slippage are eating the edge. That is the finding.
- **latency cost** — theoretical minus simulated, in USDT. The tax you pay for
  not being co-located.
- **lifetime distribution (ms)** — how long opportunities live. A p50 of ~10 ms
  means you had no realistic chance; only opportunities living well beyond your
  latency are actionable.

**A profitable simulated line is necessary but not sufficient.** It still
assumes you fill at the L1 price up to L1 size, ignores queue position, ignores
the market-impact of your own order, and ignores withdrawal/transfer time
between venues (which for real cross-exchange arb is the actual killer). Treat a
positive result as "worth investigating with more instrumentation," never as
"switch on capital."

---

## What is modeled — and what is deliberately not (limitations)

Being explicit here is the point of the tool.

| Area | What we do | Honest limitation |
|---|---|---|
| Book depth | Capture **L1** (best bid/ask + size). Fills capped at L1 size. | No deeper levels; large orders that would walk the book are *not* modeled — results are optimistic beyond L1 depth. |
| Slippage | Adverse price movement over the 250 ms latency window; edge decay reported in bps. | Does not model your own market impact or multi-level sweep. |
| Latency | Fixed 250 ms detection→arrival (configurable). Fill uses the book as it exists then. | Real latency is variable and venue-dependent; no network jitter model. |
| Cross-exchange | Both venues quoted in **USDT** (BTC-USDT / ETH-USDT on Coinbase) so the spread is clean. | Ignores inter-venue transfer time & withdrawal limits — the dominant real-world constraint. Does **not** include maker rebates. |
| Fees | Parameterized taker fees, applied to each leg's output. | Assumes taker on every leg; no BNB-discount tiering unless you set the fee. |
| Lifetime | Measured at book-update granularity (sub-ms for cross-exchange, per-tick for triangular). | An opportunity shorter than the gap between updates can be under-measured. |
| Clock | Single local `time.time()` in ms. | Not exchange-clock-synced; latency figures are relative, not absolute. |

The fee convention (documented in `strategy/fees.py`): **every taker trade's
output asset is reduced by `(1 - fee)`**, matching how Binance/Coinbase deduct
fees from the received asset. This one rule composes across cross-exchange (2
legs) and triangular (3 legs) routes.

---

## Safety

- **No order endpoints.** Nothing in this repo places, cancels, or signs an
  order. Search it: there is no `POST /order`, no signed trade path.
- **Keys are optional and never used to trade.** If you ever add API keys to
  `config.yaml`, startup performs a **read-only** check of the key's permissions
  (`GET /sapi/v1/account/apiRestrictions`) and **refuses to run if withdrawals
  are enabled** — failing closed if it cannot verify. Use trading-and-reading
  keys only, or better, no keys at all (market data needs none).
- **Kill switch.** The moment the configured file (default `KILL`) exists,
  everything stops.
- **Hard risk limits.** Max notional per pair per opportunity, and a cumulative
  daily simulated-loss limit that halts all strategies (data capture continues
  so you keep the record). Both are enforced in `execution/risk.py`.

---

## Tests

The fee and triangle calculations are the load-bearing math, so they are pinned
down exhaustively:

```bash
python -m pytest -q
# tests/test_fees.py        fee primitives, cross-exchange break-even
# tests/test_triangles.py   enumeration, rotation-invariance, real-edge detection
# tests/test_fill_model.py  route walk + L1 depth capping (linearity, binding leg)
# tests/test_risk.py        kill switch, daily-loss halt, per-pair cap, day roll
# tests/test_config_safety.py  withdrawal-permission gate (mocked, no network)
# tests/test_integration.py    detect → persist → simulate → report end-to-end
```

---

## Roadmap (not yet built — say the word)

- L2 depth streams for true book-walking slippage.
- A third venue (Kraken) for triangulated cross-exchange.
- Maker/passive fill modeling with queue-position assumptions.
- Inter-venue transfer-time modeling for cross-exchange realism.
