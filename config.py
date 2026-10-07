"""
Configuration for the BTC trading bot.

Safe defaults:
- Paper trading is ON unless PAPER_MODE=false is explicitly set.
- Live API keys are read from environment variables, never hard-coded.
- Live trading also requires LIVE_TRADING_ENABLED=YES_I_UNDERSTAND.
"""

import os


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw is None or raw.strip() == "" else float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None or raw.strip() == "" else int(raw)


# --- Market ---
EXCHANGE_ID = os.getenv("EXCHANGE_ID", "kraken")
SYMBOL = os.getenv("SYMBOL", "BTC/USD")
TIMEFRAME = os.getenv("TIMEFRAME", "1h")

# --- Strategy: Moving Average Crossover + RSI filter ---
FAST_MA = _env_int("FAST_MA", 12)
SLOW_MA = _env_int("SLOW_MA", 26)
RSI_PERIOD = _env_int("RSI_PERIOD", 14)
RSI_OVERBOUGHT = _env_float("RSI_OVERBOUGHT", 70.0)
RSI_OVERSOLD = _env_float("RSI_OVERSOLD", 30.0)  # unused, spot-only

# --- Shared risk management ---
STARTING_BALANCE_USD = _env_float("STARTING_BALANCE_USD", 10_000.0)
POSITION_SIZE_PCT = _env_float("POSITION_SIZE_PCT", 0.25)
STOP_LOSS_PCT = _env_float("STOP_LOSS_PCT", 0.03)
TAKE_PROFIT_PCT = _env_float("TAKE_PROFIT_PCT", 0.06)
FEE_PCT = _env_float("FEE_PCT", 0.0026)
POLL_SECONDS = _env_int("POLL_SECONDS", 60)

# --- Mode / live arming ---
# PAPER_MODE remains True unless PAPER_MODE=false is deliberately set.
PAPER_MODE = _env_bool("PAPER_MODE", True)
LIVE_TRADING_ENABLED = os.getenv("LIVE_TRADING_ENABLED", "").strip() == "YES_I_UNDERSTAND"

# --- Live credentials: set these as Railway environment variables ---
API_KEY = os.getenv("KRAKEN_API_KEY", "").strip()
API_SECRET = os.getenv("KRAKEN_API_SECRET", "").strip()

# --- Live-only hard safety limits ---
# Even if POSITION_SIZE_PCT calculates a larger trade, a live BUY is capped here.
LIVE_MAX_TRADE_USD = _env_float("LIVE_MAX_TRADE_USD", 25.0)
LIVE_MIN_CASH_RESERVE_USD = _env_float("LIVE_MIN_CASH_RESERVE_USD", 1.0)
MAX_NEW_TRADES_PER_DAY = _env_int("MAX_NEW_TRADES_PER_DAY", 2)
MAX_DAILY_REALIZED_LOSS_USD = _env_float("MAX_DAILY_REALIZED_LOSS_USD", 5.0)

# A live bot must persist this path across restarts/deployments. On Railway,
# mount a persistent volume and set BOT_STATE_PATH to a file on that volume.
BOT_STATE_PATH = os.getenv(
    "BOT_STATE_PATH",
    "paper_state.json" if PAPER_MODE else "live_state.json",
)

# If live state is missing but the exchange already contains BTC, the bot refuses
# to start because it cannot safely know which BTC belongs to the bot.
EXISTING_BASE_BALANCE_TOLERANCE = _env_float("EXISTING_BASE_BALANCE_TOLERANCE", 0.00000001)
