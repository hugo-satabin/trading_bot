"""Vérifie la configuration et les données publiques Kraken avant lancement."""
import os
from dotenv import load_dotenv
import ccxt

script_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(script_dir, "config.env"))

symbols = [s.strip() for s in os.getenv("SYMBOLS", "SOL/EUR").split(",")]
btc_symbol = os.getenv("BTC_SYMBOL", "BTC/EUR")
checks = {
    "TELEGRAM_TOKEN": os.getenv("TELEGRAM_TOKEN", "").strip(),
    "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID", "").strip(),
    "KRAKEN_API_KEY": os.getenv("KRAKEN_API_KEY", "").strip(),
    "KRAKEN_API_SECRET": os.getenv("KRAKEN_API_SECRET", "").strip(),
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
    print(f"  [ERREUR] Marchés publics Kraken: {exc}")
    all_ok = False

if checks["KRAKEN_API_KEY"] and checks["KRAKEN_API_SECRET"]:
    try:
        private_exchange = ccxt.kraken({
            "apiKey": checks["KRAKEN_API_KEY"],
            "secret": checks["KRAKEN_API_SECRET"],
            "enableRateLimit": True,
        })
        balance = private_exchange.fetch_balance()
        free_quote = float((balance.get("free") or {}).get(
            os.getenv("QUOTE_CURRENCY", "EUR"), 0.0
        ))
        private_exchange.fetch_open_orders()
        print(f"  [OK] Lecture du solde et des ordres ouverts: {free_quote:.2f} {os.getenv('QUOTE_CURRENCY', 'EUR')}")
    except Exception as exc:
        print(f"  [ERREUR] Authentification Kraken ou permission de lecture: {exc}")
        all_ok = False
else:
    print("  [À FAIRE] Renseigner les deux clés Kraken pour tester l'authentification")

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
    print("=== Vérifications réussies. main.py enverra des ordres Spot réels. ===")
else:
    print("=== PROBLÈMES DÉTECTÉS — corriger avant de lancer ===")
