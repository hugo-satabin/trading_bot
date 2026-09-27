"""
reset_bot.py — Remet la base de données et tous les compteurs à zéro.
Lancer AVANT de relancer main.py.
"""
import sqlite3, json, os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trading_bot.db")
INITIAL_BALANCE = 1000.0

if not os.path.exists(DB_PATH):
    print("[INFO] Aucune base de données trouvée, rien à effacer.")
else:
    with sqlite3.connect(DB_PATH) as c:
        c.execute("DELETE FROM trades")
        c.execute("DELETE FROM kv")
        c.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", ("balance",      json.dumps(INITIAL_BALANCE)))
        c.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", ("high_watermark", json.dumps(INITIAL_BALANCE)))
        c.commit()
    print(f"[OK] Base effacee. Balance et high_watermark remis a {INITIAL_BALANCE} EUR.")

print("[OK] Reset termine. Tu peux relancer main.py.")
