"""
Strategy: Moving Average Crossover with an RSI filter.

Logic:
- BUY when the fast MA crosses above the slow MA (a "golden cross"),
  but only if RSI is not already overbought (avoids chasing pumps).
- SELL (exit the long position) when the fast MA crosses below the
  slow MA (a "death cross"), or when stop-loss / take-profit is hit
  (handled by the trader/backtester, not here).

This is a simple, well-known, publicly documented strategy. It is
NOT guaranteed to be profitable. Markets trend only part of the time;
this strategy tends to lose money (via whipsaw trades) in choppy,
sideways markets.
"""

import pandas as pd
import config


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add fast MA, slow MA, and RSI columns to an OHLCV dataframe."""
    df = df.copy()
    df["fast_ma"] = df["close"].rolling(config.FAST_MA).mean()
    df["slow_ma"] = df["close"].rolling(config.SLOW_MA).mean()

    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(config.RSI_PERIOD).mean()
    avg_loss = loss.rolling(config.RSI_PERIOD).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-9)
    df["rsi"] = 100 - (100 / (1 + rs))

    return df


def generate_signal(df: pd.DataFrame, i: int) -> str:
    """
    Return 'buy', 'sell', or 'hold' for candle index i.
    Requires i >= 1 so we can compare against the previous candle
    (to detect a crossover, not just a static state).
    """
    if i < 1:
        return "hold"

    prev = df.iloc[i - 1]
    cur = df.iloc[i]

    if pd.isna(prev["slow_ma"]) or pd.isna(cur["slow_ma"]) or pd.isna(cur["rsi"]):
        return "hold"

    crossed_up = prev["fast_ma"] <= prev["slow_ma"] and cur["fast_ma"] > cur["slow_ma"]
    crossed_down = prev["fast_ma"] >= prev["slow_ma"] and cur["fast_ma"] < cur["slow_ma"]

    if crossed_up and cur["rsi"] < config.RSI_OVERBOUGHT:
        return "buy"
    if crossed_down:
        return "sell"
    return "hold"
