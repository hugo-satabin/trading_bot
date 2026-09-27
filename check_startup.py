"""Script de vérification avant lancement du bot."""
from dotenv import load_dotenv
import os
load_dotenv("config.env")

print("=== Vérification config ===")
checks = {
    "BINANCE_API_KEY":    os.getenv("BINANCE_API_KEY"),
    "BINANCE_API_SECRET": os.getenv("BINANCE_API_SECRET"),
    "TELEGRAM_TOKEN":     os.getenv("TELEGRAM_TOKEN"),
    "TELEGRAM_CHAT_ID":   os.getenv("TELEGRAM_CHAT_ID"),
    "SYMBOLS":            os.getenv("SYMBOLS"),
    "INITIAL_BALANCE":    os.getenv("INITIAL_BALANCE"),
}
all_ok = True
for k, v in checks.items():
    status = "[OK]" if v else "[MANQUANT]"
    if not v:
        all_ok = False
    display = (v[:14] + "...") if v and len(v) > 14 else v
    print(f"  {status} {k} = {display}")

print()
print("=== Test connexion Binance testnet ===")
from binance.client import Client
try:
    c = Client(os.getenv("BINANCE_API_KEY"), os.getenv("BINANCE_API_SECRET"), testnet=True)
    info = c.get_account()
    balances = [b for b in info["balances"] if float(b["free"]) > 0]
    print("  [OK] Connexion testnet etablie")
    print("  Soldes disponibles :")
    for b in balances[:8]:
        print(f"    {b['asset']:6s} : {b['free']}")
except Exception as e:
    print(f"  [ERREUR] Connexion Binance : {e}")
    print("  --> Regenerer les cles sur https://testnet.binance.vision")
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
    print("=== TOUT EST OK — tu peux lancer : python main.py ===")
else:
    print("=== PROBLEMES DETECTES — corriger avant de lancer ===")
