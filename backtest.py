"""
backtest.py — Moteur de backtesting historique.

Usage :
  python backtest.py --symbol SOLUSDT --interval 15m --days 30
  python backtest.py --symbol ETHUSDT --days 60 --balance 2000
"""
import argparse, os, sys
from datetime import datetime, timedelta
import pandas as pd

script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, script_dir)

from dotenv import load_dotenv
load_dotenv(os.path.join(script_dir, "config.env"))

from binance.client import Client
from strategies import calculate_score, compute_levels


def _fetch(client, symbol: str, interval: str, days: int) -> pd.DataFrame:
    start = (datetime.utcnow() - timedelta(days=days)).strftime("%d %b %Y %H:%M:%S")
    raw = client.get_historical_klines(symbol, interval, start)
    df = pd.DataFrame(raw, columns=[
        "timestamp", "open", "high", "low", "close", "volume",
        "close_time", "qv", "trades", "tb", "tq", "ign",
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col])
    return df


def run_backtest(
    symbol: str = "SOLUSDT",
    interval: str = "15m",
    days: int = 30,
    initial_balance: float = 1000.0,
    fees_pct: float = 0.00075,
    trade_size_pct: float = 0.08,
    trailing_dist: float = 0.012,
    max_positions: int = 4,
    params: dict = None,
    verbose: bool = True,
) -> dict:
    """
    Simule la stratégie sur données historiques Binance.
    Retourne un dict avec les métriques clés.
    params : dict optionnel de paramètres (rsi_period, atr_period, etc.)
             transmis directement à calculate_score / compute_levels.
    """
    client = Client(
        os.getenv("BINANCE_API_KEY"),
        os.getenv("BINANCE_API_SECRET"),
        testnet=True,
    )

    if verbose:
        print(f"[BACKTEST] Téléchargement {symbol} {interval} {days}j...")

    df_full   = _fetch(client, symbol, interval, days)
    df_1h_full = _fetch(client, symbol, "1h", days)

    if verbose:
        print(f"[BACKTEST] {len(df_full)} bougies 15m — simulation en cours...")

    balance   = initial_balance
    positions = []
    trades    = []
    warmup    = 150

    for i in range(warmup, len(df_full)):
        df_slice  = df_full.iloc[: i + 1].copy()
        ts_now    = df_full.iloc[i]["timestamp"]
        df_1h_slice = df_1h_full[df_1h_full["timestamp"] <= ts_now].tail(60)
        price     = float(df_full.iloc[i]["close"])

        # ── Gestion des positions ouvertes ────────────────────────────────
        closed_ids = []
        for j, pos in enumerate(positions):
            if pos["qty_remaining"] <= 0:
                closed_ids.append(j)
                continue

            if price > pos["highest"]:
                pos["highest"] = price

            if pos["trailing_on"]:
                new_trail = pos["highest"] * (1 - trailing_dist)
                pos["sl"] = max(new_trail, pos["sl"])

            if pos["tp1_done"] and not pos["trailing_on"]:
                pos["trailing_on"] = True

            # SL
            if price <= pos["sl"]:
                qty = pos["qty_remaining"]
                revenue = qty * price
                fee = revenue * fees_pct
                pnl = (revenue - fee) - qty * pos["entry"] \
                      - pos["fee_paid"] * (qty / pos["qty_total"])
                balance += revenue - fee
                trades.append({"pnl": pnl, "reason": "sl",
                                "entry": pos["entry"], "exit": price})
                pos["qty_remaining"] = 0
                closed_ids.append(j)
                continue

            # TP1
            if not pos["tp1_done"] and price >= pos["tp1"]:
                qty = pos["qty_remaining"] * 0.50
                revenue = qty * price
                fee = revenue * fees_pct
                pnl = (revenue - fee) - qty * pos["entry"] \
                      - pos["fee_paid"] * (qty / pos["qty_total"])
                balance += revenue - fee
                pos["qty_remaining"] -= qty
                pos["tp1_done"] = True
                pos["sl"] = pos["entry"] * (1 + fees_pct * 2)
                trades.append({"pnl": pnl, "reason": "tp1",
                                "entry": pos["entry"], "exit": price})

            # TP2
            elif pos["tp1_done"] and price >= pos["tp2"]:
                qty = pos["qty_remaining"]
                revenue = qty * price
                fee = revenue * fees_pct
                pnl = (revenue - fee) - qty * pos["entry"] \
                      - pos["fee_paid"] * (qty / pos["qty_total"])
                balance += revenue - fee
                pos["qty_remaining"] = 0
                trades.append({"pnl": pnl, "reason": "tp2",
                                "entry": pos["entry"], "exit": price})
                closed_ids.append(j)

        positions = [p for k, p in enumerate(positions)
                     if k not in closed_ids and p["qty_remaining"] > 0]

        # ── Signal d'achat ────────────────────────────────────────────────
        if len(positions) >= max_positions:
            continue

        result = calculate_score(
            df_slice,
            df_1h_slice if not df_1h_slice.empty else None,
            symbol=symbol,
            params=params,
            quiet=True,
        )
        if result["action"] != "BUY" or result["levels"] is None:
            continue

        levels = result["levels"]
        qty = (balance * trade_size_pct) / price
        cost = qty * price
        fee  = cost * fees_pct

        if cost + fee > balance or cost < 10:
            continue

        balance -= cost + fee
        positions.append({
            "entry":         price,
            "qty_total":     qty,
            "qty_remaining": qty,
            "sl":            levels["sl"],
            "tp1":           levels["tp1"],
            "tp2":           levels["tp2"],
            "highest":       price,
            "tp1_done":      False,
            "trailing_on":   False,
            "fee_paid":      fee,
        })

    # ── Fermer les positions restantes au dernier prix ─────────────────────
    last_price = float(df_full.iloc[-1]["close"])
    for pos in positions:
        qty = pos["qty_remaining"]
        if qty <= 0:
            continue
        revenue = qty * last_price
        fee = revenue * fees_pct
        pnl = (revenue - fee) - qty * pos["entry"] \
              - pos["fee_paid"] * (qty / pos["qty_total"])
        balance += revenue - fee
        trades.append({"pnl": pnl, "reason": "close_eod",
                        "entry": pos["entry"], "exit": last_price})

    # ── Métriques ────────────────────────────────────────────────────────
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in trades)
    win_rate  = len(wins) / len(trades) * 100 if trades else 0.0

    # Max drawdown
    equity = initial_balance
    peak   = equity
    max_dd = 0.0
    for t in trades:
        equity += t["pnl"]
        peak = max(peak, equity)
        dd = (peak - equity) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    # Profit factor
    gross_win  = sum(t["pnl"] for t in wins)  if wins   else 0
    gross_loss = abs(sum(t["pnl"] for t in losses)) if losses else 1e-10
    profit_factor = gross_win / gross_loss

    if verbose:
        sep = "=" * 52
        print(f"\n{sep}")
        print(f"  BACKTEST — {symbol} {interval} {days}j")
        print(sep)
        print(f"  Capital initial  : {initial_balance:.2f} USDT")
        print(f"  Capital final    : {balance:.2f} USDT")
        pnl_sign = "+" if total_pnl >= 0 else ""
        print(f"  PnL total        : {pnl_sign}{total_pnl:.2f} USDT "
              f"({pnl_sign}{total_pnl/initial_balance*100:.1f}%)")
        print(f"  Trades           : {len(trades)} "
              f"({len(wins)}W / {len(losses)}L)")
        print(f"  Win rate         : {win_rate:.1f}%")
        print(f"  Profit factor    : {profit_factor:.2f}")
        if wins:
            print(f"  Gain moyen       : +{gross_win/len(wins):.2f} USDT")
        if losses:
            print(f"  Perte moyenne    : -{gross_loss/len(losses):.2f} USDT")
        print(f"  Max drawdown     : {max_dd*100:.1f}%")
        print(sep + "\n")

    return {
        "final_balance": balance,
        "total_pnl":     total_pnl,
        "pnl_pct":       total_pnl / initial_balance * 100,
        "win_rate":      win_rate,
        "trades":        len(trades),
        "max_drawdown":  max_dd,
        "profit_factor": profit_factor,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest du bot de trading")
    parser.add_argument("--symbol",   default="SOLUSDT")
    parser.add_argument("--interval", default="15m")
    parser.add_argument("--days",     type=int,   default=30)
    parser.add_argument("--balance",  type=float, default=1000.0)
    parser.add_argument("--size",     type=float, default=0.08,
                        help="Fraction du capital par trade (défaut 8%%)")
    args = parser.parse_args()

    run_backtest(
        symbol=args.symbol,
        interval=args.interval,
        days=args.days,
        initial_balance=args.balance,
        trade_size_pct=args.size,
    )
