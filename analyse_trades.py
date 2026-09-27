"""Analyse des trades enregistrés en base SQLite."""
import database as db

db.init_db()
trades = db.get_all_trades()

if not trades:
    print("Aucun trade en base de données.")
else:
    sells  = [t for t in trades if t["side"] == "SELL"]
    buys   = [t for t in trades if t["side"] == "BUY"]
    wins   = [t for t in sells if t["pnl"] > 0]
    losses = [t for t in sells if t["pnl"] <= 0]

    total_pnl  = sum(t["pnl"] for t in sells)
    gross_win  = sum(t["pnl"] for t in wins)   if wins   else 0
    gross_loss = sum(t["pnl"] for t in losses) if losses else 0
    avg_win    = gross_win  / len(wins)   if wins   else 0
    avg_loss   = gross_loss / len(losses) if losses else 0
    win_rate   = len(wins) / len(sells) * 100 if sells else 0
    pf         = gross_win / abs(gross_loss) if gross_loss else float("inf")

    initial = db.kv_get("balance", 1000.0)

    print("=" * 60)
    print("  ANALYSE DES PERFORMANCES")
    print("=" * 60)
    print(f"  Achats       : {len(buys)}")
    print(f"  Ventes       : {len(sells)}")
    print(f"  Wins / Losses: {len(wins)} W / {len(losses)} L")
    print(f"  Win rate     : {win_rate:.1f}%")
    print(f"  PnL total    : {total_pnl:+.4f} EUR")
    print(f"  Profit factor: {pf:.2f}")
    print(f"  Gain moyen   : {avg_win:+.4f} EUR")
    print(f"  Perte moyenne: {avg_loss:+.4f} EUR")
    print(f"  Ratio G/P    : {abs(avg_win/avg_loss):.2f}" if avg_loss else "  Ratio G/P    : inf")

    print()
    print("--- Répartition des raisons de sortie ---")
    reasons = {}
    for t in sells:
        r = t.get("reason") or "?"
        reasons[r] = reasons.get(r, {"count": 0, "pnl": 0.0})
        reasons[r]["count"] += 1
        reasons[r]["pnl"]   += t["pnl"]
    for r, v in sorted(reasons.items(), key=lambda x: x[1]["pnl"]):
        print(f"  {r:15s} : {v['count']:3d} fois | PnL total {v['pnl']:+.4f}")

    print()
    print("--- 20 derniers trades ---")
    print(f"  {'Heure':19} {'Side':4} {'Symbole':8} {'Prix':>10} {'Qté':>8} {'PnL':>10} Raison")
    for t in trades[-20:]:
        pnl_str = f"{t['pnl']:+.4f}" if t["pnl"] != 0 else "   ---  "
        print(f"  {t['time']:19} {t['side']:4} {(t['symbol'] or '?'):8} "
              f"{t['price']:>10.4f} {t['quantity']:>8.4f} {pnl_str:>10} {t.get('reason','')}")
