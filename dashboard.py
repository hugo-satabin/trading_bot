"""
dashboard.py — Dashboard web Flask.
Accès : http://localhost:5000

Affiche en temps réel (refresh JS 10s) :
  • Solde + PnL session + Win Rate
  • Positions ouvertes
  • Historique des 30 derniers trades
"""
from flask import Flask, jsonify, render_template_string
import database as db

app = Flask(__name__)

_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <title>Trading Bot</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:monospace;background:#0d0d0d;color:#d0d0d0;padding:24px}
    h1{color:#00e5ff;margin-bottom:20px;font-size:1.4rem}
    h2{color:#aaa;font-size:.95rem;margin:18px 0 8px}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:20px}
    .card{background:#161616;border:1px solid #2a2a2a;border-radius:8px;padding:14px}
    .card .label{font-size:.75rem;color:#666;margin-bottom:4px}
    .card .value{font-size:1.3rem;font-weight:bold}
    .green{color:#00e676} .red{color:#ff5252} .neutral{color:#aaa}
    table{width:100%;border-collapse:collapse;font-size:.82rem}
    th{background:#1a1a1a;padding:7px 10px;text-align:left;color:#888;border-bottom:1px solid #2a2a2a}
    td{padding:6px 10px;border-bottom:1px solid #1a1a1a}
    tr:hover td{background:#1a1a1a}
    .badge{display:inline-block;padding:2px 7px;border-radius:4px;font-size:.78rem}
    .badge-buy{background:#004d00;color:#00e676}
    .badge-sell{background:#4d0000;color:#ff5252}
  </style>
</head>
<body>
  <h1>Trading Bot — Dashboard</h1>
  <div id="summary" class="grid"></div>
  <h2>Positions ouvertes</h2>
  <div id="positions"></div>
  <h2>Historique des trades</h2>
  <div id="trades"></div>

  <script>
    function fmt(n,d=4){return Number(n).toFixed(d)}
    function pnlClass(v){return v>0?'green':v<0?'red':'neutral'}
    function pnlSign(v){return v>=0?'+':''}

    function render(data){
      // Summary cards
      const pb = data.balance>=data.initial?'green':'red';
      document.getElementById('summary').innerHTML = `
        <div class="card"><div class="label">Solde</div>
          <div class="value ${pb}">${fmt(data.balance,2)} USDT</div></div>
        <div class="card"><div class="label">PnL réalisé (session)</div>
          <div class="value ${pnlClass(data.session_pnl)}">${pnlSign(data.session_pnl)}${fmt(data.session_pnl,2)} USDT</div></div>
        <div class="card"><div class="label">PnL portefeuille</div>
          <div class="value ${pnlClass(data.balance_pnl)}">${pnlSign(data.balance_pnl)}${fmt(data.balance_pnl,2)} USDT</div></div>
        <div class="card"><div class="label">Win Rate</div>
          <div class="value">${fmt(data.win_rate,1)}%</div></div>
        <div class="card"><div class="label">Trades (W/L)</div>
          <div class="value">${data.total_trades} <span class="green">${data.wins}W</span> / <span class="red">${data.losses}L</span></div></div>
        <div class="card"><div class="label">Positions ouvertes</div>
          <div class="value">${data.open_positions}</div></div>
        <div class="card"><div class="label">Profit Factor</div>
          <div class="value ${data.profit_factor>=1?'green':'red'}">${fmt(data.profit_factor,2)}</div></div>
      `;

      // Open positions
      if(!data.positions||data.positions.length===0){
        document.getElementById('positions').innerHTML='<p style="color:#555;font-size:.85rem">Aucune position ouverte.</p>';
      } else {
        let html='<table><tr><th>Symbole</th><th>Entrée</th><th>SL</th><th>TP1</th><th>TP2</th><th>Score</th><th>Régime</th><th>TP1 fait?</th></tr>';
        data.positions.forEach(p=>{
          html+=`<tr>
            <td>${p.symbol}</td>
            <td>${fmt(p.entry)}</td>
            <td class="red">${fmt(p.sl)}</td>
            <td class="green">${fmt(p.tp1)}</td>
            <td class="green">${fmt(p.tp2)}</td>
            <td>${fmt(p.score,0)}</td>
            <td>${p.regime}</td>
            <td>${p.tp1_done?'<span class="green">✓</span>':'<span class="neutral">—</span>'}</td>
          </tr>`;
        });
        html+='</table>';
        document.getElementById('positions').innerHTML=html;
      }

      // Trade history
      if(!data.recent_trades||data.recent_trades.length===0){
        document.getElementById('trades').innerHTML='<p style="color:#555;font-size:.85rem">Aucun trade.</p>';
      } else {
        let html='<table><tr><th>Heure</th><th>Side</th><th>Symbole</th><th>Prix</th><th>Qté</th><th>PnL</th><th>Score</th><th>Raison</th></tr>';
        data.recent_trades.forEach(t=>{
          const badge=t.side==='BUY'?'badge-buy':'badge-sell';
          const pc=pnlClass(t.pnl);
          html+=`<tr>
            <td>${t.time}</td>
            <td><span class="badge ${badge}">${t.side}</span></td>
            <td>${t.symbol||'—'}</td>
            <td>${fmt(t.price)}</td>
            <td>${fmt(t.quantity,6)}</td>
            <td class="${pc}">${pnlSign(t.pnl)}${fmt(t.pnl,4)}</td>
            <td>${t.score||0}</td>
            <td>${t.reason||'—'}</td>
          </tr>`;
        });
        html+='</table>';
        document.getElementById('trades').innerHTML=html;
      }
    }

    function refresh(){
      fetch('/api/stats')
        .then(r=>r.json())
        .then(render)
        .catch(e=>console.warn('Dashboard refresh error:',e));
    }

    refresh();
    setInterval(refresh, 10000);
  </script>
</body>
</html>"""

# Ces références sont injectées depuis main.py après import
_positions_ref   = []
_initial_balance = 1000.0
_session_ref     = {"pnl": 0.0, "wins": 0, "losses": 0}


def set_positions_ref(positions: list, initial_balance: float):
    global _positions_ref, _initial_balance
    _positions_ref   = positions
    _initial_balance = initial_balance


def set_session_ref(d: dict):
    """Reçoit le dict en mémoire de main.py — mise à jour instantanée sans copie."""
    global _session_ref
    _session_ref = d


@app.route("/")
def index():
    return render_template_string(_HTML)


@app.route("/api/stats")
def stats():
    s_wins   = _session_ref["wins"]
    s_losses = _session_ref["losses"]
    s_total  = s_wins + s_losses
    win_rate = s_wins / s_total * 100 if s_total > 0 else 0.0

    gross_win     = _session_ref.get("gross_win",  0.0)
    gross_loss    = _session_ref.get("gross_loss", 0.0)
    profit_factor = gross_win / gross_loss if gross_loss > 1e-10 else 0.0

    balance = db.kv_get("balance", _initial_balance)
    recent  = db.get_recent_trades(30)

    open_pos = [
        {
            "symbol":   p.get("symbol", "?"),
            "entry":    p.get("entry",  0),
            "sl":       p.get("sl",     0),
            "tp1":      p.get("tp1",    0),
            "tp2":      p.get("tp2",    0),
            "score":    p.get("score",  0),
            "regime":   p.get("regime", "?"),
            "tp1_done": p.get("tp1_done", False),
        }
        for p in _positions_ref
        if p.get("qty_remaining", 0) > 0
    ]

    start_bal    = _session_ref.get("start_balance", _initial_balance)
    # Inclure le coût des positions ouvertes pour ne pas afficher l'achat comme une perte
    open_cost    = sum(
        p.get("entry", 0) * p.get("qty_remaining", 0)
        for p in _positions_ref if p.get("qty_remaining", 0) > 0
    )
    balance_pnl  = (balance + open_cost) - start_bal

    return jsonify({
        "balance":       balance,
        "initial":       _initial_balance,
        "start_balance": start_bal,
        "session_pnl":   _session_ref["pnl"],
        "balance_pnl":   balance_pnl,
        "win_rate":      win_rate,
        "total_trades":  s_total,
        "wins":          s_wins,
        "losses":        s_losses,
        "profit_factor": profit_factor,
        "open_positions": len(open_pos),
        "positions":     open_pos,
        "recent_trades": recent,
    })


def start_dashboard(port: int = 5000):
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    start_dashboard()
