"""
strategies.py — Moteur de scoring multi-indicateurs + détection de régime.

Nouveautés v2 :
  • Chandeliers japonais : Hammer, Engulfing, Doji, Morning Star (+10/+15 pts)
  • Paramètres injectables via dict `params` (pour Optuna / backtest)
  • buy_threshold configurable (défaut 60)
  • Argument quiet=True pour backtests silencieux
"""
import numpy as np
import pandas as pd


# ─── HELPERS INDICATEURS ─────────────────────────────────────────────────────

def _rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    g = d.where(d > 0, 0.0).ewm(alpha=1 / n, adjust=False).mean()
    l = (-d.where(d < 0, 0.0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + g / l.replace(0, 1e-10))


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"]  - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def _bb(s: pd.Series, n: int = 20) -> dict:
    sma   = s.rolling(n).mean()
    std   = s.rolling(n).std()
    upper = sma + 2 * std
    lower = sma - 2 * std
    pct_b = (s - lower) / (upper - lower).replace(0, np.nan)
    bw    = (upper - lower) / sma.replace(0, np.nan)
    return {"upper": upper, "lower": lower, "pct_b": pct_b, "bw": bw, "sma": sma}


def _macd(s: pd.Series, f: int = 12, sl: int = 26, sig: int = 9) -> dict:
    m      = _ema(s, f) - _ema(s, sl)
    signal = _ema(m, sig)
    return {"hist": m - signal, "macd": m, "signal": signal}


def _stoch_rsi(rsi: pd.Series, n: int = 14) -> pd.Series:
    lo = rsi.rolling(n).min()
    hi = rsi.rolling(n).max()
    return (rsi - lo) / (hi - lo).replace(0, np.nan)


def _vol_ratio(df: pd.DataFrame, n: int = 20) -> float:
    avg = df["volume"].rolling(n).mean().iloc[-1]
    return df["volume"].iloc[-1] / avg if avg > 0 else 1.0


def _adx(df: pd.DataFrame, n: int = 14) -> float:
    hi, lo = df["high"], df["low"]
    up     = hi.diff()
    down   = -lo.diff()
    pdm    = up.where((up > down) & (up > 0), 0.0)
    ndm    = down.where((down > up) & (down > 0), 0.0)
    atr_   = _atr(df, n)
    pdi    = 100 * pdm.ewm(alpha=1 / n, adjust=False).mean() / atr_.replace(0, np.nan)
    ndi    = 100 * ndm.ewm(alpha=1 / n, adjust=False).mean() / atr_.replace(0, np.nan)
    dx     = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean().iloc[-1]


# ─── CHANDELIERS JAPONAIS ─────────────────────────────────────────────────────

def _detect_candlestick_patterns(df: pd.DataFrame) -> tuple:
    """
    Détecte les patterns de retournement sur les 3 dernières bougies.
    Retourne (bonus_points: float, raisons: list[str]).

    Patterns haussiers (+pts) : Hammer, Engulfing haussier, Morning Star, Doji
    Patterns baissiers (−pts) : Engulfing baissier
    """
    if len(df) < 3:
        return 0.0, []

    bonus   = 0.0
    reasons = []

    c2 = df.iloc[-3]
    c1 = df.iloc[-2]
    c0 = df.iloc[-1]

    def body(c):       return abs(c["close"] - c["open"])
    def full_range(c): return max(c["high"] - c["low"], 1e-10)
    def upper_sh(c):   return c["high"] - max(c["close"], c["open"])
    def lower_sh(c):   return min(c["close"], c["open"]) - c["low"]
    def is_green(c):   return c["close"] > c["open"]
    def is_red(c):     return c["close"] < c["open"]

    b0  = body(c0); r0 = full_range(c0)
    ls0 = lower_sh(c0); us0 = upper_sh(c0)
    b1  = body(c1)
    b2  = body(c2)

    # ── Hammer : ombre basse ≥ 2× corps, ombre haute < 30% corps, bougie verte
    if is_green(c0) and b0 > 0 and ls0 >= 2 * b0 and us0 <= 0.30 * b0:
        bonus += 10
        reasons.append("Hammer haussier")

    # ── Bullish Engulfing : c1 rouge, c0 verte englobe corps de c1
    if (is_red(c1) and is_green(c0) and b0 > 0 and b1 > 0
            and c0["open"] <= c1["close"] and c0["close"] >= c1["open"]):
        bonus += 10
        reasons.append("Engulfing haussier")

    # ── Bearish Engulfing : c1 verte, c0 rouge englobe corps de c1
    if (is_green(c1) and is_red(c0) and b0 > 0 and b1 > 0
            and c0["open"] >= c1["close"] and c0["close"] <= c1["open"]):
        bonus -= 10
        reasons.append("Engulfing baissier")

    # ── Doji : corps < 10% du range → indécision / possible retournement
    if b0 < 0.10 * r0:
        bonus += 3
        reasons.append("Doji")

    # ── Morning Star : c2 rouge fort, c1 petit corps, c0 verte > mi-c2
    c2_mid = (c2["open"] + c2["close"]) / 2
    if (is_red(c2) and b2 > 0
            and body(c1) < 0.30 * b2
            and is_green(c0) and c0["close"] > c2_mid):
        bonus += 15
        reasons.append("Morning Star")

    return bonus, reasons


# ─── DÉTECTION DU RÉGIME DE MARCHÉ ───────────────────────────────────────────

def detect_market_regime(df: pd.DataFrame, params: dict = None) -> str:
    """
    'trending_up' | 'trending_down' | 'ranging' | 'volatile'

    trending_down exige 3 confirmations simultanées pour éviter les faux négatifs.
    params : dict optionnel — adx_period (défaut 14).
    """
    p      = params or {}
    adx_n  = p.get("adx_period", 14)

    close      = df["close"]
    adx        = _adx(df, adx_n)
    ema21      = _ema(close, 21)
    ema50_last = _ema(close, 50).iloc[-1]
    price      = close.iloc[-1]
    bb         = _bb(close)
    bw         = bb["bw"].iloc[-1]
    bw_avg     = bb["bw"].rolling(50).mean().iloc[-1]

    if not np.isnan(bw_avg) and bw > bw_avg * 2.0:
        return "volatile"

    ema21_slope = (
        (ema21.iloc[-1] - ema21.iloc[-5]) / ema21.iloc[-5]
        if len(df) >= 5 else 0.0
    )

    bear = [adx > 28, price < ema50_last, ema21_slope < -0.001]
    if all(bear):
        return "trending_down"

    bull = [adx > 22, price > ema50_last, ema21_slope > 0]
    if sum(bull) >= 2:
        return "trending_up"

    return "ranging"


# ─── SCORING ─────────────────────────────────────────────────────────────────

def _score_signal(df: pd.DataFrame, regime: str, params: dict = None) -> tuple:
    """
    Score 0-100. Pondération adaptée au régime.
    params : rsi_period, bb_period (optionnels).
    Retourne (score: float, raisons: list[str]).
    """
    p      = params or {}
    rsi_n  = p.get("rsi_period", 14)
    bb_n   = p.get("bb_period",  20)

    close  = df["close"]
    score  = 50.0
    rsns   = []

    rsi_s  = _rsi(close, rsi_n)
    rsi    = rsi_s.iloc[-1]
    stoch  = _stoch_rsi(rsi_s).iloc[-1]
    macd_d = _macd(close)
    hist   = macd_d["hist"]
    h0, h1 = hist.iloc[-1], hist.iloc[-2]
    bb     = _bb(close, bb_n)
    pct_b  = bb["pct_b"].iloc[-1]
    ema9   = _ema(close, 9).iloc[-1]
    ema21  = _ema(close, 21).iloc[-1]
    ema50  = _ema(close, 50).iloc[-1]
    price  = close.iloc[-1]
    vol_r  = _vol_ratio(df)

    # Poids selon régime
    w_rsi  = 35 if regime == "ranging"     else 20
    w_bb   = 25 if regime == "ranging"     else 10
    w_macd = 15 if regime == "ranging"     else 30
    w_ema  = 10 if regime == "ranging"     else 25

    # 1. RSI
    if   rsi < 25: score += w_rsi;        rsns.append(f"RSI={rsi:.1f} survente extreme")
    elif rsi < 35: score += w_rsi * 0.65; rsns.append(f"RSI={rsi:.1f} survente")
    elif rsi < 45: score += w_rsi * 0.20; rsns.append(f"RSI={rsi:.1f} neutre-bas")
    elif rsi > 75: score -= w_rsi;        rsns.append(f"RSI={rsi:.1f} surachat extreme")
    elif rsi > 65: score -= w_rsi * 0.65; rsns.append(f"RSI={rsi:.1f} surachat")
    elif rsi > 55: score -= w_rsi * 0.20; rsns.append(f"RSI={rsi:.1f} neutre-haut")

    # 2. StochRSI
    if not np.isnan(stoch):
        if   stoch < 0.15: score += 8; rsns.append("StochRSI bas")
        elif stoch > 0.85: score -= 8; rsns.append("StochRSI haut")

    # 3. Bollinger %B
    if not np.isnan(pct_b):
        if   pct_b < 0.02: score += w_bb;        rsns.append("BB touche bande basse")
        elif pct_b < 0.20: score += w_bb * 0.55;  rsns.append(f"BB%B={pct_b:.2f} zone basse")
        elif pct_b > 0.98: score -= w_bb;         rsns.append("BB touche bande haute")
        elif pct_b > 0.80: score -= w_bb * 0.55;  rsns.append(f"BB%B={pct_b:.2f} zone haute")

    # 4. MACD histogram
    cross_up   = h0 > 0 and h1 <= 0
    cross_down = h0 < 0 and h1 >= 0
    if   cross_up:   score += w_macd;         rsns.append("MACD croisement haussier")
    elif h0 > 0:     score += w_macd * 0.35;  rsns.append("MACD>0")
    elif cross_down: score -= w_macd;         rsns.append("MACD croisement baissier")
    elif h0 < 0:     score -= w_macd * 0.35;  rsns.append("MACD<0")

    # 5. EMA trend filter
    if   ema9 > ema21 and price > ema50: score += w_ema;        rsns.append("EMA alignees hausse")
    elif ema9 > ema21:                   score += w_ema * 0.40; rsns.append("EMA9>EMA21")
    elif ema9 < ema21 and price < ema50: score -= w_ema;        rsns.append("EMA alignees baisse")
    elif ema9 < ema21:                   score -= w_ema * 0.40; rsns.append("EMA9<EMA21")

    # 6. Volume spike
    if not np.isnan(vol_r) and vol_r > 2.0:
        bias = 6 if score > 50 else -6
        score += bias
        rsns.append(f"Vol x{vol_r:.1f}")

    # 7. VWAP
    try:
        typical = (df["high"] + df["low"] + df["close"]) / 3
        vwap = (typical * df["volume"]).cumsum() / df["volume"].cumsum().replace(0, np.nan)
        vwap_last = vwap.iloc[-1]
        if not np.isnan(vwap_last):
            diff_pct = (price - vwap_last) / vwap_last
            if diff_pct < -0.005:         # prix 0.5% sous VWAP → contexte acheteur
                score += 8;  rsns.append(f"Prix sous VWAP ({diff_pct*100:.1f}%)")
            elif diff_pct > 0.010:        # prix 1% au-dessus VWAP → possible exhaustion
                score -= 5;  rsns.append("Prix très au-dessus VWAP")
            elif diff_pct > 0:
                score += 3;  rsns.append("Prix au-dessus VWAP")
    except Exception:
        pass

    # 8. OBV divergence
    try:
        direction = np.sign(df["close"].diff()).fillna(0)
        obv = (direction * df["volume"]).cumsum()
        if len(obv) >= 11:
            obv_chg  = (obv.iloc[-1] - obv.iloc[-11]) / (abs(obv.iloc[-11]) + 1e-10)
            px_chg   = (close.iloc[-1] - close.iloc[-11]) / close.iloc[-11]
            if obv_chg > 0.02 and px_chg < -0.005:    # OBV monte, prix baisse → accumulation
                score += 10; rsns.append("OBV divergence haussière")
            elif obv_chg > 0 and px_chg > 0:           # confirmation haussière
                score += 6;  rsns.append("OBV confirme hausse")
            elif obv_chg < -0.02 and px_chg > 0.005:  # OBV baisse, prix monte → distribution
                score -= 8;  rsns.append("OBV divergence baissière")
            elif obv_chg < 0:
                score -= 4;  rsns.append("OBV baissier")
    except Exception:
        pass

    # 9. Chandeliers japonais
    candle_bonus, candle_rsns = _detect_candlestick_patterns(df)
    score += candle_bonus
    rsns.extend(candle_rsns)

    # Malus régime
    if regime == "trending_down": score -= 20; rsns.append("tendance baissiere")
    if regime == "volatile":      score -= 12; rsns.append("marche volatile")

    return round(max(0.0, min(100.0, score)), 1), rsns


# ─── NIVEAUX D'ORDRE ─────────────────────────────────────────────────────────

def compute_levels(
    df: pd.DataFrame,
    entry: float,
    fees_pct: float = 0.00075,
    regime: str = "ranging",
    params: dict = None,
) -> dict:
    """
    SL/TP calculés à partir de l'ATR.
    params : atr_period, atr_sl_mult, atr_tp1_mult, atr_tp2_mult (optionnels).
    """
    p        = params or {}
    atr_n    = p.get("atr_period",   14)
    sl_mult  = p.get("atr_sl_mult",  1.2)
    tp1_mult = p.get("atr_tp1_mult", 0.8)
    tp2_mult = p.get("atr_tp2_mult", 2.0)

    atr  = _atr(df, atr_n).iloc[-1]
    mult = 1.3 if regime == "trending_up" else 1.0

    # fee_floor couvre frais aller-retour + 0.80% profit minimum → TP1 ≥ ~1.0%
    fee_floor = entry * (1 + 2 * fees_pct + 0.008)

    # SL : ATR-based, plafonné à 0.8% — plus serré pour améliorer le R:R
    sl = entry - min(max(sl_mult * atr * mult, entry * 0.005), entry * 0.008)

    # TP1 : min ~1.0%, max 1.8% — R:R ≥ 1.5:1 par rapport au SL
    tp1_raw = entry + max(tp1_mult * atr * mult, entry * 0.010)
    tp1 = max(min(tp1_raw, entry * 1.018), fee_floor)

    # TP2 : plafonné à 3%
    tp2_raw = entry + max(tp2_mult * atr * mult, entry * 0.015)
    tp2 = max(min(tp2_raw, entry * 1.030), tp1 * 1.005)

    # TP3 uniquement en trending_up, plafonné à 5%
    tp3 = None
    if regime == "trending_up":
        tp3_mult = p.get("atr_tp3_mult", 4.0)
        tp3_raw = entry + max(tp3_mult * atr * mult, entry * 0.030)
        tp3 = max(min(tp3_raw, entry * 1.050), tp2 * 1.005)

    # entry est stocké pour permettre de recaler les niveaux au prix réel de fill
    return {"sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3, "atr": atr, "entry": entry}


# ─── POINT D'ENTRÉE PRINCIPAL ─────────────────────────────────────────────────

def calculate_score(
    df: pd.DataFrame,
    df_1h: pd.DataFrame = None,
    symbol: str = "SOL/EUR",
    params: dict = None,
    quiet: bool = False,
) -> dict:
    """
    Retourne :
      { score, action, regime, reasons, levels }

    params : dict optionnel transmis à tous les sous-calculs (pour Optuna).
    quiet  : si True, supprime le print de score (utile en backtest).
    """
    if df.empty or len(df) < 50:
        return {
            "score": 50, "action": "HOLD", "regime": "unknown",
            "reasons": ["Donnees insuffisantes"], "levels": None,
        }

    p         = params or {}
    threshold = p.get("buy_threshold", 60)

    try:
        regime = detect_market_regime(df, params=params)
        score, reasons = _score_signal(df, regime, params=params)

        # Filtre macro 1h
        if df_1h is not None and not df_1h.empty and len(df_1h) >= 21:
            ema21_1h = _ema(df_1h["close"], 21)
            trend_1h = "up" if ema21_1h.iloc[-1] > ema21_1h.iloc[-5] else "down"
            if trend_1h == "down":
                score = max(score - 15, 0)
                reasons.append("1h baissiere -15pts")
            else:
                reasons.append("1h haussiere")

        if not quiet:
            print(f"[SCORE] {score:.0f}/100 | regime={regime} | "
                  f"{' | '.join(reasons[:4])}")

        if regime == "trending_down":
            action = "BLOCK"
        elif score >= threshold:
            action = "BUY"
        else:
            action = "HOLD"

        entry  = df["close"].iloc[-1]
        levels = (
            compute_levels(df, entry, regime=regime, params=params)
            if action == "BUY" else None
        )

        return {
            "score":   score,
            "action":  action,
            "regime":  regime,
            "reasons": reasons,
            "levels":  levels,
        }

    except Exception as e:
        if not quiet:
            print(f"[ERROR SCORE] {e}")
        return {
            "score": 50, "action": "HOLD", "regime": "unknown",
            "reasons": [f"Erreur: {e}"], "levels": None,
        }
