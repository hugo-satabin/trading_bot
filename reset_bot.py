"""
reset_bot.py — Remet la base de données et tous les compteurs à zéro.
Lancer AVANT de relancer main.py.
"""
import sqlite3, json, os
from dotenv import load_dotenv

script_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(script_dir, "config.env"))
configured_db = os.getenv("TRADING_DB_PATH", "trading_bot.db")
DB_PATH = configured_db if os.path.isabs(configured_db) else os.path.join(script_dir, configured_db)
INITIAL_BALANCE = 1000.0

if not os.path.exists(DB_PATH):
    print("[INFO] Aucune base de données trouvée, rien à effacer.")
else:
    with sqlite3.connect(DB_PATH) as c:
        c.execute("DELETE FROM trades")
        c.execute("DELETE FROM kv")
        c.execute("CREATE TABLE IF NOT EXISTS settled_orders (order_id TEXT PRIMARY KEY)")
        c.execute("DELETE FROM settled_orders")
        c.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", ("balance",      json.dumps(INITIAL_BALANCE)))
        c.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", ("high_watermark", json.dumps(INITIAL_BALANCE)))
        c.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", ("initial_balance", json.dumps(INITIAL_BALANCE)))
        c.commit()
    print(f"[OK] Base effacee. Balance et high_watermark remis a {INITIAL_BALANCE} EUR.")
    print("[WARNING] Cela ne vend ni ne modifie les actifs détenus sur Kraken.")

print("[OK] Reset termine. Tu peux relancer main.py.")
