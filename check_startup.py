"""Vérifie la configuration et les données publiques Kraken avant lancement."""
import os
from dotenv import load_dotenv
import ccxt

script_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(script_dir, "config.env"))

symbols = [s.strip() for s in os.getenv("SYMBOLS", "SOL/EUR").split(",")]
btc_symbol = os.getenv("BTC_SYMBOL", "BTC/EUR")
checks = {
    "TELEGRAM_TOKEN": os.getenv("TELEGRAM_TOKEN"),
    "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID"),
    "SYMBOLS": ",".join(symbols),
    "INITIAL_BALANCE": os.getenv("INITIAL_BALANCE"),
}
all_ok = True
print("=== Vérification config ===")
for name, value in checks.items():
    status = "[OK]" if value else "[MANQUANT]"
    if not value:
        all_ok = False
    print(f"  {status} {name}")

print("\n=== Test des marchés publics Kraken ===")
try:
    exchange = ccxt.kraken({"enableRateLimit": True})
    markets = exchange.load_markets()
    for symbol in list(dict.fromkeys(symbols + [btc_symbol])):
        if symbol not in markets:
            raise ValueError(f"Paire indisponible: {symbol}")
        ticker = exchange.fetch_ticker(symbol)
        print(f"  [OK] {symbol}: {ticker.get('last') or ticker.get('close')}")
except Exception as exc:
    print(f"  [ERREUR] Données Kraken: {exc}")
    all_ok = False

print()
print("=== Test modules ===")
try:
    import database as db
    db.init_db()
    print("  [OK] database.py + SQLite")
except Exception as e:
    print(f"  [ERREUR] database : {e}")
    all_ok = False

try:
    from strategies import calculate_score
    print("  [OK] strategies.py")
except Exception as e:
    print(f"  [ERREUR] strategies : {e}")
    all_ok = False

try:
    from dashboard import start_dashboard
    print("  [OK] dashboard.py")
except Exception as e:
    print(f"  [ERREUR] dashboard : {e}")
    all_ok = False

try:
    from telegram_bot import start_telegram_bot
    print("  [OK] telegram_bot.py")
except Exception as e:
    print(f"  [ERREUR] telegram_bot : {e}")
    all_ok = False

print()
if all_ok:
    print("=== Vérifications réussies — simulation papier : python main.py ===")
else:
    print("=== PROBLÈMES DÉTECTÉS — corriger avant de lancer ===")
