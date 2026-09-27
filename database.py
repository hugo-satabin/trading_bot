"""
database.py — Persistance SQLite : trades, état de session.
Survie aux redémarrages. Thread-safe via timeout et contextmanager.
"""
import sqlite3, json, os
from contextlib import contextmanager

_script_dir = os.path.dirname(os.path.abspath(__file__))
_configured_db_path = os.getenv("TRADING_DB_PATH", "trading_bot.db")
DB_PATH = (
    _configured_db_path
    if os.path.isabs(_configured_db_path)
    else os.path.join(_script_dir, _configured_db_path)
)


@contextmanager
def _conn():
    c = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            side     TEXT,
            symbol   TEXT,
            price    REAL,
            quantity REAL,
            pnl      REAL,
            score    REAL,
            reason   TEXT,
            time     TEXT
        );
        CREATE TABLE IF NOT EXISTS kv (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """)


def save_trade(trade: dict):
    with _conn() as c:
        c.execute(
            "INSERT INTO trades(side,symbol,price,quantity,pnl,score,reason,time) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                trade.get("side"),     trade.get("symbol"),
                trade.get("price"),    trade.get("quantity"),
                trade.get("pnl", 0.0), trade.get("score", 0),
                trade.get("reason", ""), trade.get("time"),
            ),
        )


def settle_order(order_id: str, values: dict, trade: dict | None = None) -> bool:
    """Atomically persist a filled exchange order and its resulting bot state."""
    with _conn() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS settled_orders (order_id TEXT PRIMARY KEY)"
        )
        result = c.execute(
            "INSERT OR IGNORE INTO settled_orders(order_id) VALUES(?)", (order_id,)
        )
        if result.rowcount == 0:
            return False
        c.executemany(
            "INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)",
            [(key, json.dumps(value)) for key, value in values.items()],
        )
        if trade is not None:
            c.execute(
                "INSERT INTO trades(side,symbol,price,quantity,pnl,score,reason,time) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    trade.get("side"), trade.get("symbol"),
                    trade.get("price"), trade.get("quantity"),
                    trade.get("pnl", 0.0), trade.get("score", 0),
                    trade.get("reason", ""), trade.get("time"),
                ),
            )
    return True


def kv_set(key: str, value):
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)",
            (key, json.dumps(value)),
        )


def kv_get(key: str, default=None):
    with _conn() as c:
        row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default


def get_recent_sells(n: int = 20) -> list:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM trades WHERE side='SELL' ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_trades(n: int = 20) -> list:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


def get_all_trades() -> list:
    with _conn() as c:
        rows = c.execute("SELECT * FROM trades ORDER BY id").fetchall()
        return [dict(r) for r in rows]
