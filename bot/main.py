# --- 1. Configuration du chemin pour les imports locaux ---
import os
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))  # ✅ Résout l'erreur d'import

# --- 2. Imports standard ---
import time
import threading
import requests
import ctypes
import pandas as pd
from dotenv import load_dotenv
from datetime import datetime
from binance.client import Client
import platform
import atexit

# --- 3. Imports locaux (toutes les fonctions utilisées) ---
from indicators import (
    calculate_rsi, calculate_macd, calculate_atr,  # <-- calculate_atr était manquant !
    calculate_bollinger_bands, calculate_vwap, calculate_obv,
    calculate_volume_ratio, calculate_ichimoku
)
from strategies import calculate_score, get_data, should_trade
from telegram_bot import (
    notify_trade,
    update_balance,
    add_trade,
    set_bot_running,
    start_telegram_bot
)

# --- Configuration Trading ---
MIN_BUY_SCORE = int(os.getenv("MIN_BUY_SCORE", 70))
MAX_SELL_SCORE = int(os.getenv("MAX_SELL_SCORE", 30))
TRADE_SIZE_PCT = float(os.getenv("TRADE_SIZE_PCT", 0.05))
TP_MARGIN_PCT = float(os.getenv("TP_MARGIN_PCT", 0.03))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", -0.015))
TRAILING_STOP_PCT = float(os.getenv("TRAILING_STOP_PCT", 0.02))
RISK_REWARD_RATIO = float(os.getenv("RISK_REWARD_RATIO", 2.0))
ATR_MULTIPLIER = float(os.getenv("ATR_MULTIPLIER", 1.5))
MIN_HOLD_TIME_SECONDS = int(os.getenv("MIN_HOLD_TIME_SECONDS", 300))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", 120))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS_PER_SYMBOL", 2))
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", 30))
PRIMARY_TIMEFRAME = os.getenv("PRIMARY_TIMEFRAME", "15m")

# --- Variables globales ---
current_balance = float(os.getenv("INITIAL_BALANCE", 1000.0))
current_positions = []  # {"symbol": str, "price": float, "quantity": float, "time": datetime, "trailing_stop": float, "atr": float}
trade_history = []
last_trade_time = None
bot_running = True
DAILY_TRADE_COUNT = 0
client = None  # sera initialisé plus tard

# Initialiser le client Binance (PRODUCTION)
client = Client(
    os.getenv("BINANCE_API_KEY"),
    os.getenv("BINANCE_API_SECRET"),
    testnet=False
)

# Vérification du mode PROD
if client.testnet:
    raise ValueError("❌ ERREUR CRITIQUE : Le client Binance est en mode TESTNET !")
else:
    print("[INFO] ✅ Mode PRODUCTION activé. ⚠️ ATTENTION : les trades sont réels !")

# --- Blocage de la mise en veille (Windows uniquement) ---
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

def prevent_sleep():
    if platform.system() == "Windows":
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        )

def allow_sleep():
    if platform.system() == "Windows":
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)

if platform.system() == "Windows":
    prevent_sleep()
    atexit.register(allow_sleep)
else:
    print("[INFO] Mode Linux/serveur : pas de blocage de veille.")

# --- Fonctions auxiliaires ---
def get_current_price(symbol="SOLUSDT", max_retries=3):
    """Récupère le prix actuel sur Binance (avec retries)."""
    for attempt in range(max_retries):
        try:
            ticker = client.get_symbol_ticker(symbol=symbol)
            return float(ticker["price"])
        except Exception as e:
            print(f"[ERROR] Erreur récupération prix {symbol} (essai {attempt + 1}/{max_retries}): {e}")
            time.sleep(1)
    log_action(f"[ERROR] Impossible de récupérer le prix de {symbol} après {max_retries} tentatives")
    return None

def calculate_position_size(balance, price):
    """Calcule la taille de la position (5% du solde par défaut)."""
    usdt_amount = balance * TRADE_SIZE_PCT
    quantity = usdt_amount / price
    return quantity

def log_action(message):
    """Log une action (et envoie à Telegram si important)."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_message = f"{timestamp}: {message}"

    keywords = ["[BUY]", "[SELL]", "[ERROR]", "[WARNING]", "[INFO]", "[TP]", "[SL]", "[TRAILING]"]
    if any(keyword in message for keyword in keywords):
        if os.getenv("TELEGRAM_TOKEN"):
            url = f"https://api.telegram.org/bot{os.getenv('TELEGRAM_TOKEN')}/sendMessage"
            payload = {
                "chat_id": os.getenv("TELEGRAM_CHAT_ID"),
                "text": f"🚨 *{log_message}*",
                "parse_mode": "MarkdownV2"
            }
            try:
                requests.post(url, json=payload, timeout=5)
            except Exception as e:
                print(f"[WARNING] Erreur envoi log Telegram: {e}")

    print(log_message)

# --- Gestion des positions ---
def add_position(symbol, price, quantity, atr=None):
    """Ajoute une position avec trailing stop basé sur ATR."""
    trailing_stop = price * (1 - TRAILING_STOP_PCT) if atr is None else price - (atr * ATR_MULTIPLIER)
    current_positions.append({
        "symbol": symbol,
        "price": price,
        "quantity": quantity,
        "time": datetime.now(),
        "trailing_stop": trailing_stop,
        "atr": atr,
        "highest_price": price  # Pour le trailing stop dynamique
    })
    return len(current_positions)

def update_trailing_stop(symbol, current_price):
    """Met à jour le trailing stop si le prix monte."""
    for pos in current_positions:
        if pos["symbol"] == symbol:
            if current_price > pos["highest_price"]:
                pos["highest_price"] = current_price
                # Met à jour le trailing stop (basé sur ATR ou %)
                if pos["atr"]:
                    pos["trailing_stop"] = pos["highest_price"] - (pos["atr"] * ATR_MULTIPLIER)
                else:
                    pos["trailing_stop"] = pos["highest_price"] * (1 - TRAILING_STOP_PCT)
                log_action(f"[TRAILING] Trailing stop mis à jour pour {symbol} : {pos['trailing_stop']:.4f}")
            return

def remove_position(symbol, price, quantity):
    """Retire une position de la liste."""
    for i, pos in enumerate(current_positions):
        if pos["symbol"] == symbol:
            current_positions.pop(i)
            return True
    return False

def get_position_count(symbol="SOLUSDT"):
    """Retourne le nombre de positions ouvertes pour un symbole."""
    return sum(1 for pos in current_positions if pos["symbol"] == symbol)

def get_position(symbol="SOLUSDT"):
    """Retourne la première position pour un symbole."""
    for pos in current_positions:
        if pos["symbol"] == symbol:
            return pos
    return None

# --- Calcul du Risk-Reward Ratio ---
def calculate_risk_reward(buy_price, stop_loss_price, take_profit_price):
    """Calcule le ratio risque/récompense."""
    risk = buy_price - stop_loss_price
    reward = take_profit_price - buy_price
    if risk <= 0:
        return 0
    return reward / risk

# --- Exécution des trades ---
def execute_buy(symbol="SOLUSDT", score=50):
    """Exécute un achat avec vérification du Risk-Reward Ratio."""
    global current_balance, last_trade_time, DAILY_TRADE_COUNT

    if DAILY_TRADE_COUNT >= MAX_TRADES_PER_DAY:
        log_action(f"[WARNING] Limite de {MAX_TRADES_PER_DAY} trades/jour atteinte !")
        return False

    if get_position_count(symbol) >= MAX_POSITIONS:
        log_action(f"[WARNING] Nombre max de positions ({MAX_POSITIONS}) atteint pour {symbol}")
        return False

    price = get_current_price(symbol)
    if price is None:
        log_action(f"[ERROR] Impossible de récupérer le prix de {symbol}")
        return False

    # Récupère l'ATR pour le trailing stop
    data = get_data(client, symbol, PRIMARY_TIMEFRAME, limit=50)
    atr = calculate_atr(data).iloc[-1] if len(data) >= 14 else None

    quantity = calculate_position_size(current_balance, price)
    cost = quantity * price
    fee = cost * 0.001  # Frais à 0.1%
    total_cost = cost + fee

    if total_cost > current_balance:
        log_action(f"[ERROR] Solde insuffisant pour acheter {symbol} (besoin: {total_cost:.2f} USDT)")
        return False

    # Calcul du stop-loss et take-profit
    stop_loss_price = price * (1 + STOP_LOSS_PCT)
    take_profit_price = price * (1 + TP_MARGIN_PCT)

    # Vérification du Risk-Reward Ratio
    rr_ratio = calculate_risk_reward(price, stop_loss_price, take_profit_price)
    if rr_ratio < RISK_REWARD_RATIO:
        log_action(f"[WARNING] Risk-Reward Ratio trop faible ({rr_ratio:.2f} < {RISK_REWARD_RATIO}) pour {symbol}")
        return False

    current_balance -= total_cost
    last_trade_time = datetime.now()
    DAILY_TRADE_COUNT += 1
    update_balance(current_balance)

    add_position(symbol, price, quantity, atr)

    trade = {
        "side": "BUY",
        "symbol": symbol,
        "price": price,
        "quantity": quantity,
        "pnl": 0.0,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "score": score,
        "stop_loss": stop_loss_price,
        "take_profit": take_profit_price,
        "rr_ratio": rr_ratio
    }
    trade_history.append(trade)
    add_trade(trade)

    notify_trade(
        side="BUY",
        symbol=symbol,
        price=price,
        quantity=quantity,
        pnl=0.0,
        balance_before=current_balance + total_cost,
        balance_after=current_balance,
        stop_loss=stop_loss_price,
        take_profit=take_profit_price,
        rr_ratio=rr_ratio
    )

    log_action(f"[BUY] Achat de {quantity:.6f} {symbol} à {price:.4f} USDT | Score: {score} | RR: {rr_ratio:.2f} | SL: {stop_loss_price:.4f} | TP: {take_profit_price:.4f}")
    return True

def execute_sell(symbol="SOLUSDT", score=50, reason="Score faible"):
    """Exécute une vente (ferme une position)."""
    global current_balance, last_trade_time, DAILY_TRADE_COUNT

    if DAILY_TRADE_COUNT >= MAX_TRADES_PER_DAY:
        log_action(f"[WARNING] Limite de {MAX_TRADES_PER_DAY} trades/jour atteinte !")
        return False

    position = get_position(symbol)
    if not position:
        log_action(f"[WARNING] Aucune position à vendre pour {symbol}")
        return False

    price = get_current_price(symbol)
    if price is None:
        log_action(f"[ERROR] Impossible de récupérer le prix de {symbol}")
        return False

    quantity = position["quantity"]
    buy_price = position["price"]
    revenue = quantity * price
    fee = revenue * 0.001  # Frais à 0.1%
    total_revenue = revenue - fee
    pnl = total_revenue - (quantity * buy_price)

    current_balance += total_revenue
    last_trade_time = datetime.now()
    DAILY_TRADE_COUNT += 1
    update_balance(current_balance)

    remove_position(symbol, price, quantity)

    trade = {
        "side": "SELL",
        "symbol": symbol,
        "price": price,
        "quantity": quantity,
        "pnl": pnl,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "score": score,
        "reason": reason
    }
    trade_history.append(trade)
    add_trade(trade)

    notify_trade(
        side="SELL",
        symbol=symbol,
        price=price,
        quantity=quantity,
        pnl=pnl,
        balance_before=current_balance - total_revenue,
        balance_after=current_balance,
        reason=reason
    )

    log_action(f"[SELL] Vente de {quantity:.6f} {symbol} à {price:.4f} USDT | PnL: {pnl:.2f} USDT | Score: {score} | Raison: {reason}")
    return True

# --- Vérifie et ferme les positions si TP/SL/Trailing Stop atteint ---
def check_positions_for_exit():
    """Vérifie si une position atteint TP, SL ou Trailing Stop."""
    global current_positions

    for pos in current_positions[:]:
        symbol = pos["symbol"]
        buy_price = pos["price"]
        current_price = get_current_price(symbol)
        if current_price is None:
            continue

        # Take-Profit
        take_profit_price = buy_price * (1 + TP_MARGIN_PCT)
        if current_price >= take_profit_price:
            log_action(f"[TP] Take-Profit atteint pour {symbol} (achat: {buy_price:.4f}, actuel: {current_price:.4f})")
            execute_sell(symbol, score=20, reason="Take-Profit atteint")

        # Stop-Loss
        stop_loss_price = buy_price * (1 + STOP_LOSS_PCT)
        if current_price <= stop_loss_price:
            log_action(f"[SL] Stop-Loss atteint pour {symbol} (achat: {buy_price:.4f}, actuel: {current_price:.4f})")
            execute_sell(symbol, score=20, reason="Stop-Loss atteint")

        # Trailing Stop (basé sur ATR ou %)
        if pos["atr"]:
            trailing_stop_price = pos["highest_price"] - (pos["atr"] * ATR_MULTIPLIER)
        else:
            trailing_stop_price = pos["highest_price"] * (1 - TRAILING_STOP_PCT)

        if current_price <= trailing_stop_price:
            log_action(f"[TRAILING] Trailing Stop atteint pour {symbol} (achat: {buy_price:.4f}, actuel: {current_price:.4f})")
            execute_sell(symbol, score=20, reason="Trailing Stop atteint")

        # Met à jour le trailing stop si le prix monte
        if current_price > pos["highest_price"]:
            update_trailing_stop(symbol, current_price)

        # Vérifie le temps de détention minimum
        hold_time = (datetime.now() - pos["time"]).total_seconds()
        if hold_time < MIN_HOLD_TIME_SECONDS:
            continue  # Ne vend pas avant le temps minimum

# --- Boucle principale ---
def check_trade_opportunities():
    """Vérifie les opportunités de trade en continu."""
    global last_trade_time, bot_running, DAILY_TRADE_COUNT, client

    # Réinitialise le compteur de trades à minuit
    now = datetime.now()
    if now.hour == 0 and now.minute == 0:
        DAILY_TRADE_COUNT = 0
        log_action("[INFO] Réinitialisation du compteur de trades (nouveau jour).")

    while bot_running:
        try:
            # Récupère les données
            data = get_data(client, "SOLUSDT", PRIMARY_TIMEFRAME, limit=200)
            if len(data) < 50:
                log_action("[WARNING] Pas assez de données pour calculer les indicateurs.")
                time.sleep(30)
                continue

            # Calcule le score (avec client pour le multi-timeframe)
            score = calculate_score(data, "SOLUSDT", client=client)

            # Vérifie le cooldown
            if last_trade_time and (datetime.now() - last_trade_time).total_seconds() < COOLDOWN_SECONDS:
                time.sleep(5)
                continue

            # Vérifie et ferme les positions si TP/SL/Trailing Stop atteint
            check_positions_for_exit()

            # Logique d'achat (si pas de position ouverte et score élevé)
            if get_position_count("SOLUSDT") < MAX_POSITIONS:
                do_trade, reason = should_trade(score)
                if do_trade:
                    execute_buy("SOLUSDT", score=score)

            time.sleep(5)

        except KeyboardInterrupt:
            log_action("[INFO] Arrêt manuel du bot.")
            bot_running = False
            break
        except Exception as e:
            log_action(f"[ERROR] Erreur dans check_trade_opportunities: {e}")
            time.sleep(30)

# --- Lance le bot Telegram dans un thread ---
def start_telegram_thread():
    threading.Thread(target=start_telegram_bot, daemon=True).start()

if __name__ == "__main__":
    print("[INFO] ✅ Démarrage du bot de trading PRO (Stratégie Multi-Indicateurs)...")
    log_action("[INFO] Bot démarré en MODE PRODUCTION. ⚠️ Les trades sont réels !")

    # Lance le bot Telegram
    start_telegram_thread()

    # Lance la boucle de trading
    check_trade_opportunities()