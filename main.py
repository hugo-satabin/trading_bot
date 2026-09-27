"""
main.py — Bot de trading Kraken Spot en simulation locale. v3.0

Nouveautés v3 :
    • Kraken public API     : cours et chandeliers en EUR, polling
  • VWAP + OBV           : deux indicateurs supplémentaires dans le score
  • TP3 trending_up      : 25% du trade laissé courir jusqu'à 4×ATR
  • Sortie temporelle     : positions stagnantes (sans TP1) fermées après 4h
  • Circuit breaker       : 3 pertes consécutives → pause achats 1h
  • Sizing corrélé        : réduction automatique si actifs corrélés déjà ouverts
  • Réinvestissement      : compound tracking — solde × gain → taille montante
  • Tout v2 conservé      : multi-symboles, Kelly, ordres limites, Fear&Greed,
                            BTC filter, thread safety, retry, SQLite, dashboard
"""
import os, time, threading, requests, ctypes, atexit, math, queue
import logging
from logging.handlers import RotatingFileHandler
import pandas as pd
import ccxt
from dotenv import load_dotenv
from datetime import datetime, timedelta

script_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(script_dir, "config.env"))

for _var in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
    if not os.getenv(_var):
        raise ValueError(f"{_var} manquant dans config.env")

import database as db
from telegram_bot import (notify_trade, update_balance, add_trade,
                           set_bot_running, start_telegram_bot)
from strategies import calculate_score
from dashboard import start_dashboard, set_positions_ref, set_session_ref

# ─── CONFIG ──────────────────────────────────────────────────────────────────
FEES_PCT              = float(os.getenv("FEES_PCT",               0.00075))
TRADE_SIZE_MIN        = float(os.getenv("TRADE_SIZE_MIN",         0.05))
TRADE_SIZE_MAX        = float(os.getenv("TRADE_SIZE_MAX",         0.12))
MAX_POSITIONS         = int(os.getenv("MAX_POSITIONS",             6))
COOLDOWN_SECONDS      = int(os.getenv("COOLDOWN_SECONDS",         120))
TRAILING_DISTANCE     = float(os.getenv("TRAILING_DISTANCE",      0.012))
MAX_DRAWDOWN_PCT      = float(os.getenv("MAX_DRAWDOWN_PCT",       0.10))
LOOP_SLEEP            = int(os.getenv("LOOP_SLEEP",               15))
POSITIONS_SLEEP       = int(os.getenv("POSITIONS_SLEEP",          3))
LIMIT_OFFSET          = float(os.getenv("LIMIT_OFFSET",           0.001))
LIMIT_EXPIRE_S        = int(os.getenv("LIMIT_EXPIRE_S",           120))
STAGNATION_EXIT_H     = float(os.getenv("STAGNATION_EXIT_H",      4.0))
CIRCUIT_BREAKER_N     = int(os.getenv("CIRCUIT_BREAKER_N",        3))
CIRCUIT_BREAKER_H     = float(os.getenv("CIRCUIT_BREAKER_H",      1.0))
BTC_FILTER            = os.getenv("BTC_FILTER",         "true").lower() == "true"
FEAR_GREED_FILTER     = os.getenv("FEAR_GREED_FILTER",  "true").lower() == "true"
KELLY_CRITERION       = os.getenv("KELLY_CRITERION",    "true").lower() == "true"
DASHBOARD_PORT        = int(os.getenv("DASHBOARD_PORT", 5000))
BUY_THRESHOLD         = int(os.getenv("BUY_THRESHOLD",  65))

QUOTE_CURRENCY = os.getenv("QUOTE_CURRENCY", "EUR")
BTC_SYMBOL = os.getenv("BTC_SYMBOL", f"BTC/{QUOTE_CURRENCY}")
SYMBOLS = [s.strip() for s in os.getenv("SYMBOLS", f"SOL/{QUOTE_CURRENCY}").split(",")]

# ─── ÉTAT GLOBAL ─────────────────────────────────────────────────────────────
INITIAL_BALANCE   = float(os.getenv("INITIAL_BALANCE", 1000.0))

state_lock        = threading.Lock()

current_balance   = INITIAL_BALANCE
current_positions: list = []
pending_orders: dict    = {}
trade_history: list     = []
cooldowns: dict         = {}          # symbol -> datetime dernier achat
sl_cooldowns: dict      = {}          # symbol -> datetime dernier SL (cooldown étendu)
_last_symbol_log: dict  = {}          # symbol -> {"action", "regime", "ts"} — throttle verbosité
_session_ref: dict      = {"pnl": 0.0, "wins": 0, "losses": 0, "gross_win": 0.0, "gross_loss": 0.0, "start_balance": INITIAL_BALANCE}
bot_running       = True
session_pnl       = 0.0
session_wins      = 0
session_losses    = 0

# ── Réinvestissement composé ──────────────────────────────────────────────────
high_watermark    = INITIAL_BALANCE   # pic de solde jamais atteint (pour compound)
compound_factor   = 1.0               # current_balance / INITIAL_BALANCE

# ── Circuit breaker ───────────────────────────────────────────────────────────
consecutive_losses    = 0
circuit_breaker_until = None          # datetime | None

# ── Caches filtres globaux ────────────────────────────────────────────────────
_fg_cache  = {"value": 50,     "ts": 0.0}
_btc_cache = {"bearish": False, "ts": 0.0}          # cache 5 min (plus réactif)
_BTC_CACHE_TTL  = 300
_SL_COOLDOWN_S  = 900   # 15 min de pause après un stop-loss

exchange = ccxt.kraken({"enableRateLimit": True})

# ─── ANTI-VEILLE WINDOWS ─────────────────────────────────────────────────────
def _prevent_sleep():
    if os.name == "nt":
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000003)

def _allow_sleep():
    if os.name == "nt":
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)

_prevent_sleep()
atexit.register(_allow_sleep)

# ─── RETRY EXPONENTIEL ───────────────────────────────────────────────────────
def api_call(fn, *args, max_retries: int = 3, **kwargs):
    for attempt in range(max_retries):
        try:
            return fn(*args, **kwargs)
        except (ccxt.NetworkError, ccxt.ExchangeError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            if attempt == max_retries - 1:
                log(f"[ERROR] {fn.__name__} échoué ({max_retries} tentatives): {e}")
                return None
            delay = 2 ** attempt
            log(f"[WARNING] API erreur tentative {attempt+1}: {e} — retry {delay}s")
            time.sleep(delay)
    return None

# ─── LOGGING FICHIER + TELEGRAM ASYNC ───────────────────────────────────────

# Fichier : bot.log — rotation 5 Mo, 3 fichiers conservés
_file_logger = logging.getLogger("trading_bot")
_file_logger.setLevel(logging.DEBUG)
if not _file_logger.handlers:   # évite les handlers en double si le module est rechargé
    _fh = RotatingFileHandler(
        os.path.join(script_dir, "bot.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    _fh.setFormatter(logging.Formatter("%(asctime)s %(message)s",
                                       datefmt="%Y-%m-%d %H:%M:%S"))
    _file_logger.addHandler(_fh)
_file_logger.propagate = False   # empêche la remontée vers le logger root

# Queue Telegram — thread dédié pour ne jamais bloquer le trading
_tg_queue: queue.Queue = queue.Queue()

# Seuls les événements importants vont sur Telegram (pas chaque tick [INFO])
_TG_KEYWORDS = ["[BUY]", "[SELL]", "[SL]", "[TP", "[ERROR]", "[WARNING]", "[PERF]"]

def _telegram_worker():
    """Thread daemon : vide la queue Telegram sans bloquer le bot."""
    token   = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    while True:
        msg = _tg_queue.get()
        if not msg:
            break
        try:
            r = requests.post(
                url,
                json={"chat_id": chat_id, "text": msg},
                timeout=8,
            )
            if not r.ok:
                _file_logger.warning(f"Telegram HTTP {r.status_code}: {r.text[:120]}")
        except Exception as e:
            _file_logger.warning(f"Telegram erreur réseau: {e}")
        finally:
            _tg_queue.task_done()

threading.Thread(target=_telegram_worker, daemon=True, name="tg-worker").start()

def log(msg: str):
    """
    Log horodaté vers :
      • stdout (toujours)
      • bot.log avec rotation (toujours)
      • Telegram via queue async (seulement trades, erreurs, warnings, perf)
    """
    ts  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = f"{ts}: {msg}"
    print(out)
    _file_logger.info(msg)
    if any(k in msg for k in _TG_KEYWORDS):
        _tg_queue.put(out)

# ─── COURS PUBLICS KRAKEN ────────────────────────────────────────────────────
def start_price_websocket():
    log(f"[INFO] Cours Kraken interrogés toutes les {POSITIONS_SLEEP}s")

def get_live_price(symbol: str) -> float | None:
    return get_price(symbol)

# ─── KRAKEN REST ──────────────────────────────────────────────────────────────
_symbol_filters: dict = {}

def _load_symbol_filters(symbol: str):
    if symbol in _symbol_filters:
        return
    try:
        market = exchange.market(symbol)
    except (ccxt.BaseError, KeyError) as e:
        log(f"[ERROR] Marché Kraken indisponible pour {symbol}: {e}")
        return
    limits = market.get("limits") or {}
    min_notional = (limits.get("cost") or {}).get("min")
    _symbol_filters[symbol] = {
        "min_notional": float(min_notional or 5.0),
    }

def get_price(symbol: str) -> float | None:
    ticker = api_call(exchange.fetch_ticker, symbol)
    price = ticker.get("last") or ticker.get("close") if ticker else None
    return float(price) if price is not None else None

def get_klines(interval: str, limit: int, symbol: str) -> pd.DataFrame:
    raw = api_call(exchange.fetch_ohlcv, symbol, interval, None, limit)
    if not raw:
        return pd.DataFrame()
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col])
    return df

# ─── RÉINVESTISSEMENT COMPOSÉ ─────────────────────────────────────────────────
def save_positions():
    """Sérialise current_positions en DB pour survie aux redémarrages."""
    serializable = []
    for p in current_positions:
        p_copy = dict(p)
        if isinstance(p_copy.get("time"), datetime):
            p_copy["time"] = p_copy["time"].isoformat()
        serializable.append(p_copy)
    db.kv_set("open_positions", serializable)


def load_positions() -> list:
    """Restaure les positions depuis la DB au démarrage."""
    saved = db.kv_get("open_positions", [])
    for p in saved:
        if isinstance(p.get("time"), str):
            try:
                p["time"] = datetime.fromisoformat(p["time"])
            except Exception:
                p["time"] = datetime.now()
    return saved


def _update_compound():
    """
    Mise à jour du facteur de compound et alerte aux nouveaux sommets.
    Le réinvestissement est automatique : current_balance (avec gains) est
    utilisé pour tout calcul de taille → chaque gain augmente les positions
    futures proportionnellement.
    """
    global high_watermark, compound_factor
    compound_factor = current_balance / INITIAL_BALANCE

    if current_balance > high_watermark:
        high_watermark = current_balance
        growth_pct = (current_balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100
        log(f"[PERF] Nouveau sommet de solde ! "
            f"Capital={current_balance:.2f} {QUOTE_CURRENCY} "
            f"(+{growth_pct:.1f}% | ×{compound_factor:.2f} depuis le départ)")
        db.kv_set("high_watermark", high_watermark)

# ─── CIRCUIT BREAKER ─────────────────────────────────────────────────────────
_cb_last_log: float = 0.0

def is_circuit_breaker_active() -> bool:
    global _cb_last_log
    if circuit_breaker_until and datetime.now() < circuit_breaker_until:
        if time.time() - _cb_last_log > 300:
            remaining = int((circuit_breaker_until - datetime.now()).total_seconds() / 60)
            log(f"[INFO] Circuit breaker actif — reprend dans {remaining} min")
            _cb_last_log = time.time()
        return True
    return False

def _update_consecutive(pnl: float):
    """Appelé après chaque vente pour gérer le circuit breaker."""
    global consecutive_losses, circuit_breaker_until
    with state_lock:
        if pnl <= 0:
            consecutive_losses += 1
            if consecutive_losses >= CIRCUIT_BREAKER_N:
                circuit_breaker_until = datetime.now() + timedelta(hours=CIRCUIT_BREAKER_H)
                log(f"[WARNING] Circuit breaker déclenché — "
                    f"{consecutive_losses} pertes consécutives — "
                    f"achats suspendus {CIRCUIT_BREAKER_H:.0f}h")
                consecutive_losses = 0   # reset pour le prochain cycle
        else:
            consecutive_losses = 0
            circuit_breaker_until = None

# ─── FILTRES GLOBAUX ─────────────────────────────────────────────────────────
def get_fear_greed() -> int:
    if time.time() - _fg_cache["ts"] < 3600:
        return _fg_cache["value"]
    try:
        r   = requests.get("https://api.alternative.me/fng/", timeout=5)
        val = int(r.json()["data"][0]["value"])
        _fg_cache.update({"value": val, "ts": time.time()})
        log(f"[INFO] Fear & Greed index = {val}/100")
        return val
    except Exception as e:
        log(f"[WARNING] Fear & Greed indisponible: {e}")
        return 50

def is_btc_bearish() -> bool:
    if time.time() - _btc_cache["ts"] < _BTC_CACHE_TTL:
        return _btc_cache["bearish"]
    df = get_klines("1h", 30, BTC_SYMBOL)
    if df.empty or len(df) < 6:
        return False
    ema21   = df["close"].ewm(span=21, adjust=False).mean()
    bearish = bool(ema21.iloc[-1] < ema21.iloc[-5])
    _btc_cache.update({"bearish": bearish, "ts": time.time()})
    if bearish:
        log("[INFO] BTC EMA21 1h baissière — filtre macro actif")
    return bearish

# ─── KELLY CRITERION ─────────────────────────────────────────────────────────
def kelly_fraction() -> float:
    sells = db.get_recent_sells(20)
    if len(sells) < 5:
        return TRADE_SIZE_MIN
    wins   = [t for t in sells if t["pnl"] > 0]
    losses = [t for t in sells if t["pnl"] <= 0]
    if not wins or not losses:
        return TRADE_SIZE_MIN
    win_rate     = len(wins) / len(sells)
    avg_win_pct  = sum(t["pnl"] / max(t["quantity"] * t["price"], 1e-10)
                       for t in wins) / len(wins)
    avg_loss_pct = abs(sum(t["pnl"] / max(t["quantity"] * t["price"], 1e-10)
                           for t in losses) / len(losses))
    if avg_loss_pct < 1e-10:
        return TRADE_SIZE_MAX
    kelly = max(0.0, win_rate - (1 - win_rate) * (avg_win_pct / avg_loss_pct)) * 0.5
    return max(TRADE_SIZE_MIN, min(TRADE_SIZE_MAX, kelly))

# ─── SIZING CORRÉLÉ ──────────────────────────────────────────────────────────
def correlation_discount() -> float:
    """
    SOL, ETH et BNB sont corrélés à ~80% entre eux.
    Chaque position ouverte supplémentaire réduit la taille des nouvelles
    entrées pour éviter une sur-exposition déguisée.

    0 pos ouvertes → 100%
    1 pos ouverte  →  75%
    2+ pos ouvertes →  55%
    """
    n = sum(1 for p in current_positions if p.get("qty_remaining", 0) > 0)
    if n == 0:
        return 1.00
    if n == 1:
        return 0.75
    return 0.55

# ─── SIZING ──────────────────────────────────────────────────────────────────
def compute_qty(score: float, price: float, symbol: str,
                extra_mult: float = 1.0) -> float:
    """
    Taille de position = Kelly × corrélation × Fear&Greed × score.
    Utilise current_balance (gains inclus) → réinvestissement automatique.
    """
    base = current_balance   # ← inclut les gains → compound automatique

    if KELLY_CRITERION:
        frac = kelly_fraction()
        adj  = (score - 60) / 40 * 0.2 if score >= 60 else 0.0
        frac = max(TRADE_SIZE_MIN, min(TRADE_SIZE_MAX, frac + adj))
    else:
        frac = TRADE_SIZE_MIN + (TRADE_SIZE_MAX - TRADE_SIZE_MIN) \
               * max(0.0, (score - 60) / 40)

    corr_mult = correlation_discount()
    quote_amount = base * frac * corr_mult * extra_mult
    _load_symbol_filters(symbol)
    if symbol not in _symbol_filters:
        return 0.0
    try:
        return float(exchange.amount_to_precision(symbol, quote_amount / price))
    except ccxt.BaseError as e:
        log(f"[ERROR] Quantité invalide pour {symbol}: {e}")
        return 0.0

# ─── PROTECTION DRAWDOWN ─────────────────────────────────────────────────────
def is_max_drawdown_hit() -> bool:
    # Inclure la valeur des positions ouvertes pour ne pas compter les achats comme pertes
    open_cost = sum(
        p["entry"] * p.get("qty_remaining", 0)
        for p in current_positions if p.get("qty_remaining", 0) > 0
    )
    portfolio = current_balance + open_cost
    loss = (high_watermark - portfolio) / high_watermark
    if loss >= MAX_DRAWDOWN_PCT:
        log(f"[WARNING] Max drawdown {loss*100:.1f}% — achats suspendus")
        return True
    return False

def get_total_positions() -> int:
    return sum(1 for p in current_positions if p.get("qty_remaining", 0) > 0)

# ─── ORDRES LIMITES ───────────────────────────────────────────────────────────
def place_limit_buy(symbol: str, qty: float, limit_price: float,
                    score: float, levels: dict, regime: str) -> bool:
    global current_balance

    _load_symbol_filters(symbol)
    sf      = _symbol_filters[symbol]
    try:
        price_r = float(exchange.price_to_precision(symbol, limit_price))
        qty = float(exchange.amount_to_precision(symbol, qty))
    except ccxt.BaseError as e:
        log(f"[ERROR] Précision invalide pour {symbol}: {e}")
        return False
    cost    = qty * price_r
    fee     = cost * FEES_PCT
    total   = cost + fee

    if total > current_balance:
        log(f"[WARNING] Solde insuffisant pour {symbol} ({total:.2f} > {current_balance:.2f})")
        return False
    if cost < sf["min_notional"]:
        log(f"[WARNING] Notional {cost:.2f} < min {sf['min_notional']} ({symbol})")
        return False

    order_id = time.time_ns()
    with state_lock:
        current_balance -= total
        pending_orders[order_id] = {
            "symbol":    symbol, "qty": qty, "price": price_r,
            "cost": cost, "fee": fee, "total": total,
            "score": score, "levels": levels, "regime": regime,
            "placed_at": datetime.now(),
        }
    db.kv_set("balance", current_balance)
    update_balance(current_balance)

    log(f"[BUY] Ordre papier limite | {qty:.4f} {symbol} @ {price_r:.4f} "
        f"| Score={score:.0f} | SL={levels['sl']:.4f} "
        f"TP1={levels['tp1']:.4f} TP2={levels['tp2']:.4f}"
        + (f" TP3={levels['tp3']:.4f}" if levels.get("tp3") else ""))
    return True

def _create_position_from_fill(order_id: int, info: dict, fill_price: float):
    lv = info["levels"]

    # Recaler SL/TP sur le prix réel de fill.
    # Les niveaux ont été calculés depuis le dernier close 15m (base_entry).
    # Si le fill est plus haut, TP1 serait en-dessous de l'entry — vente immédiate à perte.
    base_entry = lv.get("entry", fill_price)
    delta = fill_price - base_entry
    adj_sl  = lv["sl"]  + delta
    adj_tp1 = lv["tp1"] + delta
    adj_tp2 = lv["tp2"] + delta
    adj_tp3 = (lv["tp3"] + delta) if lv.get("tp3") else None
    if delta != 0:
        log(f"[INFO] Niveaux recalés +{delta:+.4f} (fill={fill_price:.4f} vs close={base_entry:.4f})")

    pos = {
        "symbol":        info["symbol"],
        "entry":         fill_price,
        "qty_total":     info["qty"],
        "qty_remaining": info["qty"],
        "sl":            adj_sl,
        "tp1":           adj_tp1,
        "tp2":           adj_tp2,
        "tp3":           adj_tp3,
        "initial_sl":    adj_sl,
        "highest":       fill_price,
        "tp1_done":      False,
        "tp2_done":      False,
        "tp3_done":      False,
        "breakeven":     False,
        "trailing_on":   False,
        "fee_paid":      info["fee"],
        "score":         info["score"],
        "regime":        info["regime"],
        "time":          datetime.now(),      # pour la sortie temporelle
    }
    t = {
        "side": "BUY", "symbol": info["symbol"], "price": fill_price,
        "quantity": info["qty"], "pnl": 0.0, "score": info["score"],
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "reason": "limit_filled",
    }
    with state_lock:
        current_positions.append(pos)
        pending_orders.pop(order_id, None)
        trade_history.append(t)
    add_trade(t)
    db.save_trade(t)
    notify_trade("BUY", info["symbol"], fill_price, info["qty"], 0.0,
                 current_balance + info["total"], current_balance)
    save_positions()
    log(f"[BUY] Ordre rempli | {info['qty']:.4f} {info['symbol']} @ {fill_price:.4f} "
        f"| SL={adj_sl:.4f} TP1={adj_tp1:.4f} TP2={adj_tp2:.4f} "
        f"| Score={info['score']:.0f} | ×{compound_factor:.2f} compound")

def check_pending_orders():
    global current_balance
    with state_lock:
        ids = list(pending_orders.keys())

    for order_id in ids:
        with state_lock:
            if order_id not in pending_orders:
                continue
            info = dict(pending_orders[order_id])

        symbol  = info["symbol"]
        elapsed = (datetime.now() - info["placed_at"]).total_seconds()
        market_price = get_live_price(symbol)
        if market_price is not None and market_price <= info["price"]:
            _create_position_from_fill(order_id, info, info["price"])
        elif elapsed > LIMIT_EXPIRE_S:
            with state_lock:
                current_balance += info["total"]
                pending_orders.pop(order_id, None)
            db.kv_set("balance", current_balance)
            update_balance(current_balance)
            log(f"[INFO] Ordre papier {symbol} expiré — {info['total']:.2f} remboursé")

# ─── VENTE ───────────────────────────────────────────────────────────────────
def execute_sell(pos: dict, qty: float, reason: str, price: float) -> bool:
    """
    Vend qty unités au prix price (passé depuis manage_positions pour éviter
    le double appel API). Met à jour le circuit breaker et le compound.
    """
    global current_balance, session_pnl, session_wins, session_losses

    revenue      = qty * price
    fee          = revenue * FEES_PCT
    net_revenue  = revenue - fee
    buy_cost     = qty * pos["entry"]
    fee_buy_prop = pos["fee_paid"] * (qty / pos["qty_total"])
    pnl_net      = net_revenue - buy_cost - fee_buy_prop
    pnl_pct      = pnl_net / max(buy_cost, 1e-10) * 100

    with state_lock:
        current_balance      += net_revenue
        pos["qty_remaining"] -= qty
        session_pnl          += pnl_net
        if pnl_net > 0:
            session_wins += 1
        else:
            session_losses += 1
        _session_ref["pnl"]    = session_pnl
        _session_ref["wins"]   = session_wins
        _session_ref["losses"] = session_losses
        if pnl_net > 0:
            _session_ref["gross_win"]  = _session_ref.get("gross_win",  0.0) + pnl_net
        else:
            _session_ref["gross_loss"] = _session_ref.get("gross_loss", 0.0) + abs(pnl_net)

    db.kv_set("balance", current_balance)
    update_balance(current_balance)

    # Réinvestissement composé — nouveau high watermark ?
    _update_compound()

    # Circuit breaker
    _update_consecutive(pnl_net)

    # Cooldown étendu après un SL
    if "sl" in reason:
        with state_lock:
            sl_cooldowns[pos["symbol"]] = datetime.now()

    tag = ("[TP1]"  if reason == "tp1"    else
           "[TP2]"  if reason == "tp2"    else
           "[TP3]"  if reason == "tp3"    else
           "[SL]"   if "sl" in reason     else
           "[TOUT]" if reason == "timeout" else "[SELL]")

    t = {
        "side": "SELL", "symbol": pos["symbol"], "price": price,
        "quantity": qty, "pnl": pnl_net, "score": 0,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "reason": reason,
    }
    with state_lock:
        trade_history.append(t)
    add_trade(t)
    db.save_trade(t)
    notify_trade("SELL", pos["symbol"], price, qty, pnl_net,
                 current_balance - net_revenue, current_balance, reason=reason)

    total_t = session_wins + session_losses
    wr      = session_wins / total_t * 100 if total_t > 0 else 0
    log(f"{tag} {qty:.4f} {pos['symbol']} @ {price:.4f} "
        f"| PnL={pnl_net:+.4f} ({pnl_pct:+.2f}%) "
        f"| Compound ×{compound_factor:.2f} | Session PnL={session_pnl:+.2f} "
        f"WR={wr:.0f}% | Raison={reason}")
    return True

# ─── GESTION DES POSITIONS (boucle 3s / websocket) ───────────────────────────
def manage_positions():
    """
    Gère SL, TP1, TP2, TP3 et la sortie temporelle.

    Ordre de priorité :
      1. Sortie temporelle   : position stagnante > STAGNATION_EXIT_H → ferme tout
      2. SL / trailing SL   : ferme tout
      3. TP1                 : vend 50%, active breakeven + trailing
      4. TP2                 : en trending_up vend 50% du restant, sinon tout
      5. TP3                 : vend le restant (trending_up uniquement)

    get_live_price() utilisé → websocket <100ms, fallback REST.
    """
    symbols_needed = {p["symbol"] for p in current_positions
                      if p.get("qty_remaining", 0) > 0}
    prices: dict = {}
    for sym in symbols_needed:
        p = get_live_price(sym)
        if p:
            prices[sym] = p

    with state_lock:
        snapshot = list(current_positions)

    for pos in snapshot:
        if pos.get("qty_remaining", 0) <= 0:
            continue

        price = prices.get(pos["symbol"])
        if price is None:
            continue

        # ── 1. Sortie temporelle (stagnation sans TP1) ──────────────────
        elapsed_h = (datetime.now() - pos["time"]).total_seconds() / 3600
        stag_limit = 6.0 if pos.get("symbol") == BTC_SYMBOL else STAGNATION_EXIT_H
        if not pos["tp1_done"] and elapsed_h >= stag_limit:
            log(f"[INFO] {pos['symbol']} stagnant depuis {elapsed_h:.1f}h — fermeture")
            execute_sell(pos, pos["qty_remaining"], "timeout", price)
            continue

        # ── Trailing stop : mise à jour du plus haut ────────────────────
        if price > pos["highest"]:
            pos["highest"] = price

        if pos["trailing_on"]:
            new_trail = pos["highest"] * (1 - TRAILING_DISTANCE)
            pos["sl"]  = max(new_trail, pos["sl"])

        if pos["tp1_done"] and not pos["trailing_on"] and price > pos["entry"]:
            pos["trailing_on"] = True

        # ── 2. SL atteint ───────────────────────────────────────────────
        if price <= pos["sl"]:
            reason = "trailing_sl" if pos["trailing_on"] else "sl"
            execute_sell(pos, pos["qty_remaining"], reason, price)
            continue

        # ── 3. TP1 : vente 50% + breakeven ─────────────────────────────
        if not pos["tp1_done"] and price >= pos["tp1"]:
            execute_sell(pos, pos["qty_remaining"] * 0.50, "tp1", price)
            pos["tp1_done"]  = True
            pos["sl"]        = pos["entry"] * 1.003
            pos["breakeven"] = True
            log(f"[INFO] Breakeven SL → {pos['sl']:.4f}")

        # ── 4. TP2 ──────────────────────────────────────────────────────
        elif pos["tp1_done"] and not pos["tp2_done"] and price >= pos["tp2"]:
            if pos.get("tp3"):
                # Trending_up : vend seulement 50% du restant (=25% total)
                # Laisse 25% courir vers TP3
                execute_sell(pos, pos["qty_remaining"] * 0.50, "tp2", price)
                pos["tp2_done"] = True
            else:
                # Mode standard : vend tout
                execute_sell(pos, pos["qty_remaining"], "tp2", price)

        # ── 5. TP3 (trending_up uniquement) ─────────────────────────────
        elif pos["tp1_done"] and pos["tp2_done"] and not pos.get("tp3_done") \
                and pos.get("tp3") and price >= pos["tp3"]:
            execute_sell(pos, pos["qty_remaining"], "tp3", price)
            pos["tp3_done"] = True

    # Nettoyage + persistance
    with state_lock:
        current_positions[:] = [p for p in current_positions
                                 if p.get("qty_remaining", 0) > 1e-10]
    save_positions()

# ─── SIGNAL + ACHAT ──────────────────────────────────────────────────────────
def process_symbol(symbol: str):
    """Évalue le signal pour un symbole et place un ordre limite si BUY."""
    # Cooldown par symbole
    with state_lock:
        last_buy = cooldowns.get(symbol)
    if last_buy and (datetime.now() - last_buy).total_seconds() < COOLDOWN_SECONDS:
        return

    # Cooldown étendu après un SL (15 min)
    with state_lock:
        last_sl = sl_cooldowns.get(symbol)
    if last_sl and (datetime.now() - last_sl).total_seconds() < _SL_COOLDOWN_S:
        prev = _last_symbol_log.get(symbol + "_slcd")
        if not prev or (datetime.now() - prev).total_seconds() > 300:
            remaining = int(_SL_COOLDOWN_S - (datetime.now() - last_sl).total_seconds())
            log(f"[INFO] {symbol} SL-cooldown actif — {remaining}s restantes")
            _last_symbol_log[symbol + "_slcd"] = datetime.now()
        return

    # Circuit breaker
    if is_circuit_breaker_active():
        return

    df_15m = get_klines("15m", 150, symbol)
    df_1h  = get_klines("1h",  60,  symbol)
    if df_15m.empty:
        return

    result = calculate_score(df_15m, df_1h, symbol=symbol,
                             params={"buy_threshold": BUY_THRESHOLD})
    score  = result["score"]
    action = result["action"]
    regime = result["regime"]
    levels = result["levels"]

    total_t = session_wins + session_losses
    wr_str  = f"{session_wins/total_t*100:.0f}%" if total_t > 0 else "n/a"

    # Throttle : ne loguer que si action/régime change, ou toutes les 5 min
    prev_log = _last_symbol_log.get(symbol, {})
    state_changed = (prev_log.get("action") != action or prev_log.get("regime") != regime)
    log_stale     = not prev_log or (datetime.now() - prev_log["ts"]).total_seconds() > 300
    if state_changed or log_stale:
        log(f"[INFO] {symbol} Score={score:.0f} action={action} regime={regime} "
            f"| Pos={get_total_positions()}/{MAX_POSITIONS} "
            f"| Solde={current_balance:.2f} ×{compound_factor:.2f} "
            f"| WR={wr_str} PnL={session_pnl:+.2f}")
        _last_symbol_log[symbol] = {"action": action, "regime": regime, "ts": datetime.now()}

    if action != "BUY" or levels is None:
        if action == "BLOCK" and state_changed:
            log(f"[INFO] {symbol} bloqué — régime {regime}")
        return

    # Pas d'entrée en régime volatile (marché chaotique → trop de timeouts/SL)
    if regime == "volatile":
        return

    # Bloquer si les 3 dernières bougies sont toutes baissières
    last3 = df_15m.iloc[-3:]
    if all(last3["close"] < last3["open"]):
        prev_3c = _last_symbol_log.get(symbol + "_3c")
        if not prev_3c or (datetime.now() - prev_3c).total_seconds() > 300:
            log(f"[INFO] {symbol} bloque — 3 bougies baissières consecutives")
            _last_symbol_log[symbol + "_3c"] = datetime.now()
        return

    if get_total_positions() >= MAX_POSITIONS:
        return

    # 1 position max par symbole (évite les doublons après redémarrage)
    symbol_open = sum(
        1 for p in current_positions
        if p.get("symbol") == symbol and p.get("qty_remaining", 0) > 0
    )
    if symbol_open >= 1:
        return

    if is_max_drawdown_hit():
        return

    # Filtre Fear & Greed
    fg = get_fear_greed() if FEAR_GREED_FILTER else 50
    if fg <= 10:
        log(f"[INFO] {symbol} bloqué — Fear & Greed panique totale ({fg}/100)")
        return
    fg_mult = 1.0
    if fg < 20:
        fg_mult = 0.5
        log(f"[INFO] {symbol} Fear extrême ({fg}/100) — taille ×0.5")
    elif fg > 80:
        fg_mult = 0.5
        log(f"[WARNING] {symbol} Greed élevé ({fg}/100) — taille ×0.5")

    # Filtre macro BTC — réduit la taille ×0.5 au lieu de bloquer
    btc_mult = 1.0
    if BTC_FILTER and symbol != BTC_SYMBOL and is_btc_bearish():
        btc_mult = 0.5
        prev_btc = _last_symbol_log.get(symbol + "_btc")
        if not prev_btc or (datetime.now() - prev_btc).total_seconds() > 300:
            log(f"[INFO] {symbol} BTC baissier — taille ×0.5")
            _last_symbol_log[symbol + "_btc"] = datetime.now()

    price = get_live_price(symbol)
    if price is None:
        return

    # Filtre ATR : n'entrer que si le marché est assez volatil pour atteindre TP1
    atr_pct = levels.get("atr", 0) / price if price else 0
    if atr_pct < 0.005:
        prev_atr = _last_symbol_log.get(symbol + "_atr")
        if not prev_atr or (datetime.now() - prev_atr).total_seconds() > 300:
            log(f"[INFO] {symbol} bloqué — ATR trop faible ({atr_pct*100:.2f}% < 0.50%)")
            _last_symbol_log[symbol + "_atr"] = datetime.now()
        return

    qty = compute_qty(score, price, symbol, extra_mult=fg_mult * btc_mult)
    if qty <= 0:
        return

    limit_price = price * (1 - LIMIT_OFFSET)

    if place_limit_buy(symbol, qty, limit_price, score, levels, regime):
        with state_lock:
            cooldowns[symbol] = datetime.now()

# ─── BOUCLES ─────────────────────────────────────────────────────────────────
def positions_loop():
    """Vérifie SL/TP toutes les POSITIONS_SLEEP secondes (3s par défaut).
    Utilise les prix websocket si disponibles → latence quasi-nulle."""
    while bot_running:
        try:
            check_pending_orders()
            manage_positions()
        except Exception as e:
            log(f"[ERROR] positions_loop: {e}")
        time.sleep(POSITIONS_SLEEP)

def trading_loop():
    global bot_running
    while bot_running:
        try:
            for symbol in SYMBOLS:
                if not bot_running:
                    break
                process_symbol(symbol)
            time.sleep(LOOP_SLEEP)
        except Exception as e:
            log(f"[ERROR] trading_loop: {e}")
            time.sleep(30)

# ─── DÉMARRAGE ────────────────────────────────────────────────────────────────
_LOCK_FILE = os.path.join(script_dir, "bot.lock")

if __name__ == "__main__":
    # Lockfile — empêche deux instances simultanées
    _lock_fh = open(_LOCK_FILE, "w")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(_lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[ERREUR] Une instance du bot tourne déjà (bot.lock). Arrêtez-la d'abord.")
        raise SystemExit(1)
    db.init_db()

    if not api_call(exchange.load_markets):
        log("[ERROR] Impossible de charger les marchés publics Kraken")
        raise SystemExit(1)
    for sym in set(SYMBOLS + [BTC_SYMBOL]):
        _load_symbol_filters(sym)
    if any(sym not in _symbol_filters for sym in SYMBOLS + [BTC_SYMBOL]):
        log("[ERROR] Vérifiez les paires Kraken dans config.env")
        raise SystemExit(1)

    # Restaurer le solde et le high watermark depuis la DB
    saved_balance = db.kv_get("balance")
    if saved_balance is not None:
        current_balance = saved_balance
        log(f"[INFO] Solde restauré depuis DB : {current_balance:.2f} {QUOTE_CURRENCY}")
    update_balance(current_balance)

    saved_hwm = db.kv_get("high_watermark")
    if saved_hwm is not None:
        high_watermark = saved_hwm

    saved_positions = load_positions()
    if saved_positions:
        current_positions.extend(saved_positions)
        log(f"[INFO] {len(saved_positions)} position(s) restaurée(s) depuis DB")

    compound_factor = current_balance / INITIAL_BALANCE

    _session_ref["start_balance"] = current_balance
    set_positions_ref(current_positions, INITIAL_BALANCE)
    set_session_ref(_session_ref)

    log(f"[INFO] Bot v3 démarré | Symboles={','.join(SYMBOLS)} "
        f"| Capital={current_balance:.2f} {QUOTE_CURRENCY} (×{compound_factor:.2f})")
    log(f"[INFO] TP1=50% TP2=50%(trending)/100% TP3=trending_up seulement "
        f"| Trailing {TRAILING_DISTANCE*100:.1f}%")
    log(f"[INFO] Stagnation exit {STAGNATION_EXIT_H:.0f}h "
        f"| Circuit breaker {CIRCUIT_BREAKER_N} pertes → {CIRCUIT_BREAKER_H:.0f}h pause")
    log(f"[INFO] Simulation papier Kraken Spot — aucun ordre réel envoyé")
    log(f"[INFO] Ordres limites -{LIMIT_OFFSET*100:.1f}% "
        f"| Fear&Greed={FEAR_GREED_FILTER} | BTC filter={BTC_FILTER} "
        f"| Kelly={KELLY_CRITERION}")
    log(f"[INFO] Dashboard → http://localhost:{DASHBOARD_PORT}")

    threading.Thread(target=start_price_websocket,        daemon=True).start()
    threading.Thread(target=start_telegram_bot,           daemon=True).start()
    threading.Thread(target=positions_loop,               daemon=True).start()
    threading.Thread(target=start_dashboard,
                     args=(DASHBOARD_PORT,),              daemon=True).start()

    trading_loop()
