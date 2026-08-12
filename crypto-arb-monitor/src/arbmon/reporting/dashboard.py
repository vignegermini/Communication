"""Local web dashboard: one self-contained HTML page that auto-refreshes.

Shows live cross-exchange spreads (computed from the current top of book, net of
fees), the most recent opportunity log, and cumulative simulated PnL. Binds to
localhost by default. Read-only: it never mutates state or places anything.
"""
from __future__ import annotations

import logging

from aiohttp import web

from ..data.book_state import BookStore
from ..execution.paper_portfolio import PaperPortfolio
from ..execution.risk import RiskManager
from ..strategy.fees import cross_exchange_return, to_bps

log = logging.getLogger(__name__)

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>arbmon dashboard</title>
<style>
 body{font-family:-apple-system,Segoe UI,Roboto,monospace;margin:0;background:#0c0f14;color:#d7dde5}
 header{padding:14px 20px;background:#11161f;border-bottom:1px solid #232a35;display:flex;gap:26px;flex-wrap:wrap;align-items:baseline}
 h1{font-size:16px;margin:0;color:#7cc7ff;font-weight:600}
 .kpi{font-size:13px}.kpi b{font-size:18px;display:block;color:#eaf0f6}
 .pos{color:#4ade80}.neg{color:#f87171}.warn{color:#fbbf24}
 main{padding:18px 20px;display:grid;gap:22px;grid-template-columns:1fr 1fr}
 section{background:#11161f;border:1px solid #232a35;border-radius:8px;padding:12px 14px}
 section.wide{grid-column:1/3}
 h2{font-size:13px;margin:0 0 10px;color:#9fb0c3;text-transform:uppercase;letter-spacing:.05em}
 table{width:100%;border-collapse:collapse;font-size:12px}
 th,td{text-align:right;padding:4px 8px;border-bottom:1px solid #1c232e}
 th:first-child,td:first-child{text-align:left}
 th{color:#6b7a8d;font-weight:500}
 .muted{color:#6b7a8d;font-size:11px}
 tr.live-edge{background:rgba(74,222,128,.08)}
</style></head><body>
<header>
 <h1>arbmon &mdash; paper / detection only</h1>
 <div class="kpi">equity (USDT)<b id="equity">&mdash;</b></div>
 <div class="kpi">sim PnL<b id="simpnl">&mdash;</b></div>
 <div class="kpi">theoretical PnL<b id="theopnl">&mdash;</b></div>
 <div class="kpi">fills / survived<b id="fills">&mdash;</b></div>
 <div class="kpi">status<b id="status">&mdash;</b></div>
 <div class="kpi muted">updated<b id="updated" class="muted">&mdash;</b></div>
</header>
<main>
 <section><h2>Live cross-exchange edge (net fees)</h2>
  <table><thead><tr><th>route</th><th>buy@ask</th><th>sell@bid</th>
  <th>gross bps</th><th>net bps</th></tr></thead><tbody id="spreads"></tbody></table>
  <div class="muted">Positive net bps = bid on sell-venue exceeds ask on buy-venue after both taker fees.</div>
 </section>
 <section><h2>Cumulative</h2>
  <table><tbody id="cum"></tbody></table>
 </section>
 <section class="wide"><h2>Recent opportunities</h2>
  <table><thead><tr><th>time</th><th>type</th><th>route</th><th>edge bps</th>
  <th>peak bps</th><th>depth USDT</th><th>lifetime ms</th></tr></thead>
  <tbody id="opps"></tbody></table>
 </section>
 <section class="wide"><h2>Recent simulated fills (after 250ms latency)</h2>
  <table><thead><tr><th>time</th><th>survived</th><th>filled USDT</th>
  <th>theo PnL</th><th>sim PnL</th><th>slippage bps</th><th>reason</th></tr></thead>
  <tbody id="fills-tbl"></tbody></table>
 </section>
</main>
<script>
const f2=(x)=>Number(x).toLocaleString(undefined,{maximumFractionDigits:2});
const cls=(x)=>x>0?'pos':(x<0?'neg':'');
async function tick(){
 let r; try{r=await (await fetch('/api/state')).json()}catch(e){return}
 const p=r.portfolio;
 equity.textContent=f2(p.equity_quote);
 simpnl.textContent=f2(p.realized_sim_pnl_quote); simpnl.className='kpi '+cls(p.realized_sim_pnl_quote);
 theopnl.textContent=f2(p.theo_pnl_quote);
 fills.textContent=p.n_fills+' / '+p.n_survived;
 status.textContent=r.status.text; status.className='kpi '+(r.status.ok?'pos':'neg');
 updated.textContent=new Date(r.now).toLocaleTimeString();
 spreads.innerHTML=r.spreads.map(s=>`<tr class="${s.net_bps>0?'live-edge':''}"><td>${s.route}</td>
  <td>${f2(s.buy_ask)}</td><td>${f2(s.sell_bid)}</td>
  <td class="${cls(s.gross_bps)}">${f2(s.gross_bps)}</td>
  <td class="${cls(s.net_bps)}">${f2(s.net_bps)}</td></tr>`).join('');
 cum.innerHTML=`<tr><td>opportunities logged</td><td>${r.counts.opportunities}</td></tr>
  <tr><td>survived latency</td><td>${r.counts.survived}</td></tr>
  <tr><td>book updates</td><td>${f2(r.counts.book_updates)}</td></tr>`;
 opps.innerHTML=r.opps.map(o=>`<tr><td>${new Date(o.ts_detect).toLocaleTimeString()}</td>
  <td>${o.opp_type}</td><td>${o.route}</td><td class="${cls(o.edge_bps)}">${f2(o.edge_bps)}</td>
  <td>${f2(o.peak_edge_bps)}</td><td>${f2(o.depth_quote)}</td>
  <td>${o.lifetime_ms==null?'&mdash;':o.lifetime_ms}</td></tr>`).join('');
 document.getElementById('fills-tbl').innerHTML=r.fills.map(x=>`<tr>
  <td>${new Date(x.ts_detect).toLocaleTimeString()}</td>
  <td class="${x.survived?'pos':'neg'}">${x.survived?'yes':'no'}</td>
  <td>${f2(x.filled_quote)}</td><td>${f2(x.theo_pnl_quote)}</td>
  <td class="${cls(x.sim_pnl_quote)}">${f2(x.sim_pnl_quote)}</td>
  <td>${f2(x.slippage_bps)}</td><td class="muted">${x.reason}</td></tr>`).join('');
}
tick(); setInterval(tick, REFRESH_MS);
</script></body></html>"""


class DashboardServer:
    def __init__(
        self,
        book: BookStore,
        storage,
        portfolio: PaperPortfolio,
        risk: RiskManager,
        *,
        pairs: list[dict],
        binance_fee: float,
        coinbase_fee: float,
        host: str = "127.0.0.1",
        port: int = 8787,
        refresh_seconds: int = 3,
    ) -> None:
        self.book = book
        self.storage = storage
        self.portfolio = portfolio
        self.risk = risk
        self.pairs = pairs
        self.binance_fee = binance_fee
        self.coinbase_fee = coinbase_fee
        self.host = host
        self.port = port
        self.refresh_seconds = refresh_seconds
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/api/state", self._state)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        log.info("dashboard on http://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    async def _index(self, _req: web.Request) -> web.Response:
        html = _PAGE.replace("REFRESH_MS", str(self.refresh_seconds * 1000))
        return web.Response(text=html, content_type="text/html")

    def _live_spreads(self) -> list[dict]:
        out: list[dict] = []
        for p in self.pairs:
            b = self.book.get("binance", p["binance"])
            c = self.book.get("coinbase", p["coinbase"])
            if b is None or c is None:
                continue
            # Direction 1: buy Binance ask, sell Coinbase bid.
            e1 = cross_exchange_return(c.bid, b.ask, self.coinbase_fee, self.binance_fee)
            out.append({"route": f"{p['binance']}:buyBIN_sellCB",
                        "buy_ask": b.ask, "sell_bid": c.bid,
                        "gross_bps": to_bps(c.bid / b.ask - 1.0),
                        "net_bps": to_bps(e1)})
            # Direction 2: buy Coinbase ask, sell Binance bid.
            e2 = cross_exchange_return(b.bid, c.ask, self.binance_fee, self.coinbase_fee)
            out.append({"route": f"{p['binance']}:buyCB_sellBIN",
                        "buy_ask": c.ask, "sell_bid": b.bid,
                        "gross_bps": to_bps(b.bid / c.ask - 1.0),
                        "net_bps": to_bps(e2)})
        return out

    async def _state(self, _req: web.Request) -> web.Response:
        import json as _json

        from ..models import now_ms
        conn = self.storage.conn
        opps = [dict(r) for r in conn.execute(
            "SELECT ts_detect,opp_type,edge_bps,peak_edge_bps,depth_quote,"
            "lifetime_ms,detail_json FROM opportunities "
            "ORDER BY id DESC LIMIT 30").fetchall()]
        for o in opps:
            try:
                o["route"] = _json.loads(o.pop("detail_json")).get("route_key", "")
            except Exception:  # noqa: BLE001
                o["route"] = ""
        fills = [dict(r) for r in conn.execute(
            "SELECT ts_detect,survived,filled_quote,theo_pnl_quote,sim_pnl_quote,"
            "slippage_bps,reason FROM sim_fills ORDER BY id DESC LIMIT 30").fetchall()]
        counts = {
            "opportunities": conn.execute(
                "SELECT COUNT(*) c FROM opportunities").fetchone()["c"],
            "survived": conn.execute(
                "SELECT COUNT(*) c FROM sim_fills WHERE survived=1").fetchone()["c"],
            "book_updates": self.book.update_count,
        }
        halted = self.risk.halted or self.risk.kill_switch_present()
        status = {
            "ok": not halted,
            "text": ("HALTED: " + self.risk.halt_reason) if halted else "running",
        }
        payload = {
            "now": now_ms(),
            "portfolio": self.portfolio.snapshot(),
            "spreads": self._live_spreads(),
            "opps": opps,
            "fills": fills,
            "counts": counts,
            "status": status,
        }
        return web.json_response(payload)
