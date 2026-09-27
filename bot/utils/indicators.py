"""
indicators.py — Bibliothèque d'indicateurs techniques.
Compatibilité : utilise les noms de colonnes Binance (open, high, low, close, volume).
"""
import pandas as pd
import numpy as np


# --- TENDANCE ---
def calculate_sma(data: pd.DataFrame, window: int = 50) -> pd.Series:
    return data["close"].rolling(window=window).mean()

def calculate_ema(data: pd.DataFrame, span: int = 21) -> pd.Series:
    return data["close"].ewm(span=span, adjust=False).mean()

def calculate_macd(data: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    ema_fast = data["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = data["close"].ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({
        "macd": macd_line,
        "signal": signal_line,
        "histogram": macd_line - signal_line,
    }, index=data.index)

def calculate_ichimoku(data: pd.DataFrame, conversion: int = 9, base: int = 26, span2_period: int = 52) -> dict:
    def midpoint(h, l):
        return (h + l) / 2
    conv = midpoint(data["high"].rolling(conversion).max(), data["low"].rolling(conversion).min())
    base_ = midpoint(data["high"].rolling(base).max(), data["low"].rolling(base).min())
    span_a = ((conv + base_) / 2).shift(base)
    span_b = midpoint(data["high"].rolling(span2_period).max(), data["low"].rolling(span2_period).min()).shift(base)
    return {
        "conversion_line": conv,
        "base_line": base_,
        "leading_span_a": span_a,
        "leading_span_b": span_b,
        "lagging_span": data["close"].shift(-base),
    }

# --- MOMENTUM ---
def calculate_rsi(data: pd.DataFrame, window: int = 14) -> pd.Series:
    delta = data["close"].diff()
    gain = delta.where(delta > 0, 0.0).rolling(window).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(window).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def calculate_stoch_rsi(rsi: pd.Series, window: int = 14) -> pd.Series:
    min_rsi = rsi.rolling(window).min()
    max_rsi = rsi.rolling(window).max()
    denom = (max_rsi - min_rsi).replace(0, np.nan)
    return (rsi - min_rsi) / denom

# --- VOLATILITÉ ---
def calculate_atr(data: pd.DataFrame, window: int = 14) -> pd.Series:
    high_low = data["high"] - data["low"]
    high_close = (data["high"] - data["close"].shift()).abs()
    low_close = (data["low"] - data["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(window).mean()

def calculate_bollinger_bands(data: pd.DataFrame, window: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    sma = data["close"].rolling(window).mean()
    std = data["close"].rolling(window).std()
    upper = sma + std * num_std
    lower = sma - std * num_std
    pct_b = (data["close"] - lower) / (upper - lower).replace(0, np.nan)
    bw = (upper - lower) / sma.replace(0, np.nan)
    return pd.DataFrame({
        "sma": sma,
        "upper_band": upper,
        "lower_band": lower,
        "pct_b": pct_b,
        "bandwidth": bw,
    }, index=data.index)

# --- VOLUME ---
def calculate_vwap(data: pd.DataFrame) -> pd.Series:
    typical = (data["high"] + data["low"] + data["close"]) / 3
    cum_vol = data["volume"].cumsum()
    return (typical * data["volume"]).cumsum() / cum_vol.replace(0, np.nan)

def calculate_obv(data: pd.DataFrame) -> pd.Series:
    direction = np.sign(data["close"].diff()).fillna(0)
    return (direction * data["volume"]).cumsum()

def calculate_volume_ratio(data: pd.DataFrame, window: int = 20) -> pd.Series:
    avg = data["volume"].rolling(window).mean()
    return data["volume"] / avg.replace(0, np.nan)