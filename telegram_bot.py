import os
import threading
import requests
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, CallbackContext
from dotenv import load_dotenv

# Chargement de config.env
script_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(script_dir, 'config.env')
load_dotenv(config_path)

# Variables globales
current_balance = float(os.getenv("INITIAL_BALANCE", 1000.0))
trade_history = []
bot_running = True
DAILY_TRADE_COUNT = 0
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", 30))

# Fonctions pour interagir avec main.py
def update_balance(new_balance):
    global current_balance
    current_balance = new_balance

def add_trade(trade):
    global trade_history
    trade_history.append(trade)

def set_bot_running(status):
    global bot_running
    bot_running = status

# Commandes Telegram
async def start(update: Update, context: CallbackContext):
    keyboard = [
        [InlineKeyboardButton("💰 Solde", callback_data='balance'),
         InlineKeyboardButton("📊 Historique", callback_data='history')],
        [InlineKeyboardButton("📈 Derniers Trades", callback_data='last_trades'),
         InlineKeyboardButton("📉 Stats", callback_data='stats')],
        [InlineKeyboardButton("🎯 Positions Ouvertes", callback_data='positions'),
         InlineKeyboardButton("📌 Paramètres", callback_data='settings')],
        [InlineKeyboardButton("🛑 Arrêter", callback_data='stop'),
         InlineKeyboardButton("🔄 Redémarrer", callback_data='restart')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🚀 Bot Trading SOLUSDT (Stratégie PRO) 🚀\n\n"
        "10 indicateurs techniques | Multi-Timeframe | Risk Management\n"
        "MODE PRODUCTION : Les trades sont réels !\n\n"
        "Utilise les boutons :",
        reply_markup=reply_markup
    )

async def button_click(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    global current_balance, trade_history

    if query.data == "balance":
        await query.edit_message_text(
            f"💰 Solde Actuel : {current_balance:.2f} USDT\n"
            f"📊 Trades aujourd'hui : {DAILY_TRADE_COUNT}/{MAX_TRADES_PER_DAY}"
        )
    elif query.data == "history":
        if not trade_history:
            await query.edit_message_text("📊 Aucun trade.")
        else:
            message = "📊 Historique des Trades (10 derniers)\n\n"
            for trade in trade_history[-10:]:
                emoji = "🟢" if trade["side"] == "BUY" else "🔴"
                pnl_color = "🟢" if trade["pnl"] > 0 else "🔴"
                message += (
                    f"{emoji} {trade['side']} {trade['symbol']} | "
                    f"Prix: {trade['price']:.4f} | "
                    f"Qte: {trade['quantity']:.6f} | "
                    f"PnL: {pnl_color} {trade['pnl']:.2f} USDT | "
                    f"Score: {trade['score']} | "
                    f"{trade['time']}\n"
                )
            await query.edit_message_text(message)
    elif query.data == "last_trades":
        if not trade_history:
            await query.edit_message_text("📈 Aucun trade récent.")
        else:
            message = "📈 5 Derniers Trades\n\n"
            for trade in trade_history[-5:]:
                emoji = "🟢" if trade["side"] == "BUY" else "🔴"
                pnl_color = "🟢" if trade.get("pnl", 0) > 0 else "🔴"
                reason = trade.get("reason", "N/A")
                message += (
                    f"{emoji} {trade['side']} a {trade['price']:.4f} | "
                    f"PnL: {pnl_color} {trade.get('pnl', 0):.2f} USDT | "
                    f"Score: {trade['score']} | "
                    f"Raison: {reason} | "
                    f"{trade['time']}\n"
                )
            await query.edit_message_text(message)
    elif query.data == "stats":
        if not trade_history:
            await query.edit_message_text("📉 Aucune stat.")
        else:
            wins = sum(1 for t in trade_history if t["pnl"] > 0)
            losses = len(trade_history) - wins
            total_pnl = sum(t["pnl"] for t in trade_history)
            win_rate = (wins / len(trade_history)) * 100 if trade_history else 0
            avg_pnl = total_pnl / len(trade_history) if trade_history else 0
            avg_win = sum(t["pnl"] for t in trade_history if t["pnl"] > 0) / wins if wins > 0 else 0
            avg_loss = sum(t["pnl"] for t in trade_history if t["pnl"] < 0) / losses if losses > 0 else 0
            rr = abs(avg_win / avg_loss) if avg_loss != 0 else 0
            message = (
                f"📉 Statistiques (Stratégie PRO)\n\n"
                f"🎯 Gagnants: {wins} ({win_rate:.1f}%)\n"
                f"❌ Perdants: {losses}\n"
                f"💰 PnL Total: {total_pnl:.2f} USDT\n"
                f"📊 PnL Moyen: {avg_pnl:.2f} USDT\n"
                f"📈 Gain Moyen: {avg_win:.2f} USDT\n"
                f"📉 Perte Moyenne: {avg_loss:.2f} USDT\n"
                f"📊 Trades totaux: {len(trade_history)}\n"
                f"🔄 Trades aujourd'hui: {DAILY_TRADE_COUNT}/{MAX_TRADES_PER_DAY}\n"
                f"🎯 Ratio Gain/Perte: {rr:.2f} (ideal > 1.5)"
            )
            await query.edit_message_text(message)
    elif query.data == "positions":
        from main import current_positions
        open_pos = [p for p in current_positions if p.get("qty_remaining", 0) > 0]
        if not open_pos:
            await query.edit_message_text("🎯 Aucune position ouverte.")
        else:
            message = "🎯 Positions Ouvertes\n\n"
            for pos in open_pos:
                elapsed = int((datetime.now() - pos["time"]).total_seconds() // 60)
                tp1_tag = "✅" if pos.get("tp1_done") else "⏳"
                message += (
                    f"📌 {pos['symbol']} | "
                    f"Entree: {pos['entry']:.4f} | "
                    f"Qte: {pos['qty_remaining']:.4f} | "
                    f"SL: {pos['sl']:.4f} | "
                    f"TP1: {tp1_tag} {pos['tp1']:.4f} | "
                    f"TP2: {pos['tp2']:.4f} | "
                    f"Score: {pos['score']:.0f} | "
                    f"{elapsed} min\n"
                )
            await query.edit_message_text(message)
    elif query.data == "settings":
        await query.edit_message_text(
            f"📌 Parametres Actuels\n\n"
            f"🎯 Score Achat: >= {os.getenv('MIN_BUY_SCORE', 70)}\n"
            f"🎯 Score Vente: <= {os.getenv('MAX_SELL_SCORE', 30)}\n"
            f"💰 Taille Trade: {os.getenv('TRADE_SIZE_PCT', 0.05)}% du solde\n"
            f"📈 Take-Profit: +{os.getenv('TP_MARGIN_PCT', 0.03)}%\n"
            f"🛑 Stop-Loss: {os.getenv('STOP_LOSS_PCT', -0.015)}%\n"
            f"🔄 Trailing Stop: {os.getenv('TRAILING_STOP_PCT', 0.02)}%\n"
            f"⚖️ Risk-Reward: >= {os.getenv('RISK_REWARD_RATIO', 2.0)}:1\n"
            f"📊 Max Positions: {os.getenv('MAX_POSITIONS_PER_SYMBOL', 2)}\n"
            f"🔄 Max Trades/Jour: {os.getenv('MAX_TRADES_PER_DAY', 30)}"
        )
    elif query.data == "stop":
        set_bot_running(False)
        await query.edit_message_text("❌ Bot ARRETE !\n⚠️ Plus aucun trade ne sera execute.")
    elif query.data == "restart":
        set_bot_running(True)
        await query.edit_message_text("🔄 Bot REDEMARRE !\n✅ Les trades peuvent reprendre.")

async def solde(update: Update, context: CallbackContext):
    await update.message.reply_text(
        f"💰 Solde Actuel : {current_balance:.2f} USDT\n"
        f"📊 Trades aujourd'hui : {DAILY_TRADE_COUNT}/{MAX_TRADES_PER_DAY}"
    )

def notify_trade(side, symbol, price, quantity, pnl, balance_before, balance_after, stop_loss=None, take_profit=None, rr_ratio=None, reason=None):
    if not os.getenv("TELEGRAM_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID"):
        return
    emoji = "🟢" if side == "BUY" else "🔴"
    pnl_sign = "+" if pnl >= 0 else ""
    message = (
        f"{emoji} {side} {symbol}\n"
        f"Prix: {price:.4f} USDT\n"
        f"Quantite: {quantity:.6f}\n"
        f"Solde: {balance_before:.2f} -> {balance_after:.2f} USDT\n"
        f"PnL: {pnl_sign}{pnl:.2f} USDT\n"
    )
    if stop_loss and take_profit:
        message += f"SL: {stop_loss:.4f} | TP: {take_profit:.4f}\n"
    if rr_ratio:
        message += f"Risk-Reward: {rr_ratio:.2f}:1\n"
    if reason:
        message += f"Raison: {reason}\n"
    if DAILY_TRADE_COUNT and MAX_TRADES_PER_DAY:
        message += f"Trades aujourd'hui: {DAILY_TRADE_COUNT}/{MAX_TRADES_PER_DAY}"

    url = f"https://api.telegram.org/bot{os.getenv('TELEGRAM_TOKEN')}/sendMessage"
    payload = {"chat_id": os.getenv("TELEGRAM_CHAT_ID"), "text": message}
    try:
        r = requests.post(url, json=payload, timeout=5)
        if not r.ok:
            print(f"[ERROR] notify_trade HTTP {r.status_code}: {r.text[:120]}")
    except Exception as e:
        print(f"[ERROR] Notification trade échouée: {e}")

def start_telegram_bot():
    if not os.getenv("TELEGRAM_TOKEN"):
        print("[WARNING] TELEGRAM_TOKEN manquant. Bot Telegram non démarré.")
        return
    app = Application.builder().token(os.getenv("TELEGRAM_TOKEN")).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("solde", solde))
    app.add_handler(CallbackQueryHandler(button_click))
    print("[INFO] Bot Telegram démarré...")
    app.run_polling()
