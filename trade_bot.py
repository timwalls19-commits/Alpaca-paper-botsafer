"""
BTC moving-average/RSI bot with paper trading by default and guarded live mode.

Safety changes in this version:
- Signals are generated from COMPLETED candles only.
- Only one signal action is allowed per completed candle.
- Live buys have an absolute USD cap and a daily new-trade limit.
- Live stop-loss and take-profit are enforced using the current ticker price.
- The bot sells only the BTC quantity it tracks as its own position.
- State is persisted to disk and an unresolved pending order freezes live trading.
- Live API keys must come from environment variables.
- Live trading requires two separate switches: PAPER_MODE=false and
  LIVE_TRADING_ENABLED=YES_I_UNDERSTAND.

This reduces operational risk; it does not make the strategy profitable or safe
from market losses, exchange outages, slippage, API failures, or gaps.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ccxt
import pandas as pd

import config
from strategy import compute_indicators, generate_signal


def utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def fresh_state() -> dict[str, Any]:
    return {
        "version": 2,
        "mode": "paper" if config.PAPER_MODE else "live",
        "paper_balance_usd": config.STARTING_BALANCE_USD,
        "bot_position_btc": 0.0,
        "entry_price": None,
        "entry_cost_usd": 0.0,
        "last_signal_candle_ms": None,
        "trade_day": utc_day(),
        "new_trades_today": 0,
        "daily_realized_pnl_usd": 0.0,
        "pending_order": None,
    }


def load_state(path: str) -> tuple[dict[str, Any], bool]:
    p = Path(path)
    if not p.exists():
        return fresh_state(), False
    try:
        state = fresh_state()
        saved = json.loads(p.read_text(encoding="utf-8"))
        state.update(saved)
        expected_mode = "paper" if config.PAPER_MODE else "live"
        if state.get("mode") != expected_mode:
            raise RuntimeError(
                f"State file mode is {state.get('mode')!r}, but this run is {expected_mode!r}. "
                "Use separate paper and live state files."
            )
        return state, True
    except Exception as exc:
        raise RuntimeError(f"Could not read state file {path}: {exc}") from exc


def save_state(path: str, state: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def reset_daily_counters_if_needed(state: dict[str, Any]) -> None:
    today = utc_day()
    if state.get("trade_day") != today:
        state["trade_day"] = today
        state["new_trades_today"] = 0
        state["daily_realized_pnl_usd"] = 0.0


def get_exchange():
    exchange_class = getattr(ccxt, config.EXCHANGE_ID)
    params: dict[str, Any] = {"enableRateLimit": True}
    if not config.PAPER_MODE:
        if not config.LIVE_TRADING_ENABLED:
            print("REFUSING LIVE MODE: set LIVE_TRADING_ENABLED=YES_I_UNDERSTAND as a Railway variable.")
            sys.exit(1)
        if not config.API_KEY or not config.API_SECRET:
            print("REFUSING LIVE MODE: KRAKEN_API_KEY/KRAKEN_API_SECRET are missing.")
            sys.exit(1)
        params.update({"apiKey": config.API_KEY, "secret": config.API_SECRET})
    exchange = exchange_class(params)
    exchange.load_markets()
    return exchange


def fetch_recent(exchange, limit: int = 200) -> pd.DataFrame:
    raw = exchange.fetch_ohlcv(config.SYMBOL, timeframe=config.TIMEFRAME, limit=limit)
    df = pd.DataFrame(raw, columns=["timestamp_ms", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    return df


def completed_candles(exchange, df: pd.DataFrame) -> pd.DataFrame:
    """Return only candles whose full timeframe has elapsed."""
    timeframe_ms = int(exchange.parse_timeframe(config.TIMEFRAME) * 1000)
    now_ms = int(exchange.milliseconds())
    closed = df[(df["timestamp_ms"] + timeframe_ms) <= now_ms].copy()
    if closed.empty:
        raise RuntimeError("No completed candles available yet.")
    return closed.reset_index(drop=True)


def current_price(exchange, fallback: float) -> float:
    ticker = exchange.fetch_ticker(config.SYMBOL)
    value = ticker.get("last") or ticker.get("close") or fallback
    return float(value)


def market_assets(exchange) -> tuple[str, str]:
    market = exchange.market(config.SYMBOL)
    return market["base"], market["quote"]


def free_balance(balance: dict[str, Any], asset: str) -> float:
    item = balance.get(asset)
    if isinstance(item, dict):
        return float(item.get("free") or 0.0)
    free_map = balance.get("free")
    if isinstance(free_map, dict):
        return float(free_map.get(asset) or 0.0)
    return 0.0


def validate_order_size(exchange, amount: float, price: float) -> float:
    market = exchange.market(config.SYMBOL)
    amount = float(exchange.amount_to_precision(config.SYMBOL, amount))
    if amount <= 0:
        raise RuntimeError("Calculated order size rounded to zero.")

    limits = market.get("limits") or {}
    min_amount = ((limits.get("amount") or {}).get("min"))
    min_cost = ((limits.get("cost") or {}).get("min"))
    if min_amount is not None and amount < float(min_amount):
        raise RuntimeError(f"Order amount {amount} is below exchange minimum {min_amount}.")
    if min_cost is not None and amount * price < float(min_cost):
        raise RuntimeError(f"Order cost ${amount * price:.2f} is below exchange minimum ${float(min_cost):.2f}.")
    return amount


def confirmed_order(exchange, order: dict[str, Any]) -> dict[str, Any]:
    """Try to obtain a confirmed fill so position state is never based on a guess."""
    current = order
    for _ in range(6):
        filled = float(current.get("filled") or 0.0)
        status = str(current.get("status") or "").lower()
        if filled > 0 and status in {"closed", "canceled"}:
            return current
        order_id = current.get("id")
        if not order_id or not exchange.has.get("fetchOrder"):
            break
        time.sleep(2)
        current = exchange.fetch_order(order_id, config.SYMBOL)

    filled = float(current.get("filled") or 0.0)
    status = str(current.get("status") or "").lower()
    if filled <= 0 or status not in {"closed", "canceled"}:
        raise RuntimeError(
            "Order was submitted but a terminal fill status could not be confirmed. "
            "Pending-order lock will remain set; inspect the exchange before restarting."
        )
    return current


def fill_details(order: dict[str, Any], fallback_price: float) -> tuple[float, float, float]:
    filled = float(order.get("filled") or 0.0)
    average = float(order.get("average") or order.get("price") or fallback_price)
    cost = float(order.get("cost") or (filled * average))
    fee = order.get("fee") or {}
    fee_cost = float(fee.get("cost") or 0.0) if isinstance(fee, dict) else 0.0
    return filled, average, cost + fee_cost


def paper_buy(state: dict[str, Any], price: float) -> None:
    spend = float(state["paper_balance_usd"]) * config.POSITION_SIZE_PCT
    fee = spend * config.FEE_PCT
    qty = (spend - fee) / price
    state["paper_balance_usd"] -= spend
    state["bot_position_btc"] = qty
    state["entry_price"] = price
    state["entry_cost_usd"] = spend
    state["new_trades_today"] += 1
    print(f"[PAPER] BUY {qty:.6f} BTC @ ${price:,.2f} | cash left: ${state['paper_balance_usd']:,.2f}")


def paper_sell(state: dict[str, Any], price: float, reason: str) -> None:
    qty = float(state["bot_position_btc"])
    proceeds = qty * price * (1 - config.FEE_PCT)
    pnl = proceeds - float(state.get("entry_cost_usd") or 0.0)
    state["paper_balance_usd"] += proceeds
    state["daily_realized_pnl_usd"] += pnl
    print(
        f"[PAPER] SELL {qty:.6f} BTC @ ${price:,.2f} ({reason}) "
        f"| trade P/L: ${pnl:+,.2f} | cash: ${state['paper_balance_usd']:,.2f}"
    )
    state["bot_position_btc"] = 0.0
    state["entry_price"] = None
    state["entry_cost_usd"] = 0.0


def live_buy(exchange, state: dict[str, Any], price: float, candle_ms: int) -> None:
    if state["new_trades_today"] >= config.MAX_NEW_TRADES_PER_DAY:
        print("[LIVE] Daily new-trade limit reached; BUY blocked.")
        return
    if state["daily_realized_pnl_usd"] <= -abs(config.MAX_DAILY_REALIZED_LOSS_USD):
        print("[LIVE] Daily realized-loss limit reached; BUY blocked.")
        return
    if float(state["bot_position_btc"]) > 0:
        print("[LIVE] Bot already has an open position; BUY blocked.")
        return

    base, quote = market_assets(exchange)
    balance = exchange.fetch_balance()
    quote_free = free_balance(balance, quote)
    available_after_reserve = max(0.0, quote_free - config.LIVE_MIN_CASH_RESERVE_USD)
    spend = min(available_after_reserve * config.POSITION_SIZE_PCT, config.LIVE_MAX_TRADE_USD)
    if spend <= 0:
        print(f"[LIVE] No spendable {quote} balance after cash reserve; BUY blocked.")
        return

    amount = validate_order_size(exchange, spend / price, price)
    state["pending_order"] = {"side": "buy", "candle_ms": candle_ms, "started_at": time.time()}
    save_state(config.BOT_STATE_PATH, state)

    order = exchange.create_market_buy_order(config.SYMBOL, amount)
    order = confirmed_order(exchange, order)
    filled, average, total_cost = fill_details(order, price)

    state["bot_position_btc"] = filled
    state["entry_price"] = average
    state["entry_cost_usd"] = total_cost
    state["new_trades_today"] += 1
    state["pending_order"] = None
    save_state(config.BOT_STATE_PATH, state)
    print(f"[LIVE] BUY filled {filled:.8f} {base} @ ~${average:,.2f} | tracked cost: ${total_cost:,.2f}")


def live_sell(exchange, state: dict[str, Any], price: float, reason: str, candle_ms: int | None) -> None:
    tracked_qty = float(state["bot_position_btc"])
    if tracked_qty <= 0:
        print("[LIVE] No bot-tracked position; SELL ignored.")
        return

    base, _ = market_assets(exchange)
    balance = exchange.fetch_balance()
    base_free = free_balance(balance, base)
    amount = min(tracked_qty, base_free)
    amount = validate_order_size(exchange, amount, price)

    state["pending_order"] = {"side": "sell", "candle_ms": candle_ms, "reason": reason, "started_at": time.time()}
    save_state(config.BOT_STATE_PATH, state)

    order = exchange.create_market_sell_order(config.SYMBOL, amount)
    order = confirmed_order(exchange, order)
    filled, average, _ = fill_details(order, price)

    # For a sell, order cost is the gross quote proceeds. fill_details may add a
    # reported fee, so calculate conservative proceeds directly and subtract fee.
    gross = float(order.get("cost") or (filled * average))
    fee = order.get("fee") or {}
    fee_cost = float(fee.get("cost") or 0.0) if isinstance(fee, dict) else 0.0
    proceeds = gross - fee_cost
    allocated_entry_cost = float(state.get("entry_cost_usd") or 0.0) * min(1.0, filled / tracked_qty)
    pnl = proceeds - allocated_entry_cost

    remaining = max(0.0, tracked_qty - filled)
    state["daily_realized_pnl_usd"] += pnl
    state["bot_position_btc"] = remaining
    if remaining <= config.EXISTING_BASE_BALANCE_TOLERANCE:
        state["bot_position_btc"] = 0.0
        state["entry_price"] = None
        state["entry_cost_usd"] = 0.0
    else:
        state["entry_cost_usd"] = max(0.0, float(state["entry_cost_usd"]) - allocated_entry_cost)
    state["pending_order"] = None
    save_state(config.BOT_STATE_PATH, state)
    print(
        f"[LIVE] SELL filled {filled:.8f} {base} @ ~${average:,.2f} ({reason}) "
        f"| trade P/L: ${pnl:+,.2f}"
    )


def paper_total_value(state: dict[str, Any], price: float) -> float:
    return float(state["paper_balance_usd"]) + float(state["bot_position_btc"]) * price


def validate_live_startup(exchange, state: dict[str, Any], state_existed: bool) -> None:
    if state.get("pending_order"):
        raise RuntimeError(
            "State contains an unresolved pending order. Live trading is locked. "
            "Check Kraken order history and resolve the state before running again."
        )

    base, _ = market_assets(exchange)
    balance = exchange.fetch_balance()
    base_free = free_balance(balance, base)
    tracked = float(state.get("bot_position_btc") or 0.0)

    if not state_existed and base_free > config.EXISTING_BASE_BALANCE_TOLERANCE:
        raise RuntimeError(
            f"No prior live state file exists, but the account has {base_free:.8f} {base}. "
            "Refusing to guess whether that asset belongs to this bot."
        )

    if tracked > base_free + config.EXISTING_BASE_BALANCE_TOLERANCE:
        raise RuntimeError(
            f"State says the bot owns {tracked:.8f} {base}, but only {base_free:.8f} is free. "
            "Refusing to trade until the mismatch is resolved."
        )


def main() -> None:
    mode = "PAPER (simulated)" if config.PAPER_MODE else "LIVE (REAL MONEY)"
    print(f"Starting SAFE v2 bot in {mode} mode on {config.EXCHANGE_ID} {config.SYMBOL} [{config.TIMEFRAME}]")
    print("Signals use completed candles only.")

    exchange = get_exchange()
    state, state_existed = load_state(config.BOT_STATE_PATH)
    reset_daily_counters_if_needed(state)
    save_state(config.BOT_STATE_PATH, state)

    if not config.PAPER_MODE:
        print(f"LIVE hard cap per BUY: ${config.LIVE_MAX_TRADE_USD:,.2f}")
        print(f"LIVE max new trades/day: {config.MAX_NEW_TRADES_PER_DAY}")
        print(f"LIVE daily realized-loss stop: ${config.MAX_DAILY_REALIZED_LOSS_USD:,.2f}")
        print(f"State file: {config.BOT_STATE_PATH}")
        validate_live_startup(exchange, state, state_existed)

    while True:
        try:
            reset_daily_counters_if_needed(state)

            raw_df = fetch_recent(exchange)
            closed_df = completed_candles(exchange, raw_df)
            closed_df = compute_indicators(closed_df)
            i = len(closed_df) - 1
            signal_candle_ms = int(closed_df.iloc[i]["timestamp_ms"])
            closed_price = float(closed_df.iloc[i]["close"])
            signal = generate_signal(closed_df, i)
            price = current_price(exchange, closed_price)

            # Risk exits may act immediately on current market price. Strategy
            # signals themselves are taken only from the completed candle above.
            position_qty = float(state["bot_position_btc"])
            entry_price = state.get("entry_price")
            risk_exit_done = False
            if position_qty > 0 and entry_price:
                change = (price - float(entry_price)) / float(entry_price)
                if change <= -config.STOP_LOSS_PCT:
                    if config.PAPER_MODE:
                        paper_sell(state, price, "stop-loss")
                    else:
                        live_sell(exchange, state, price, "stop-loss", None)
                    risk_exit_done = True
                elif change >= config.TAKE_PROFIT_PCT:
                    if config.PAPER_MODE:
                        paper_sell(state, price, "take-profit")
                    else:
                        live_sell(exchange, state, price, "take-profit", None)
                    risk_exit_done = True

            is_new_signal_candle = state.get("last_signal_candle_ms") != signal_candle_ms
            if is_new_signal_candle and not risk_exit_done:
                if signal == "sell" and float(state["bot_position_btc"]) > 0:
                    if config.PAPER_MODE:
                        paper_sell(state, price, "signal")
                    else:
                        live_sell(exchange, state, price, "signal", signal_candle_ms)
                elif signal == "buy" and float(state["bot_position_btc"]) == 0:
                    if state["new_trades_today"] >= config.MAX_NEW_TRADES_PER_DAY:
                        print("Daily new-trade limit reached; BUY skipped.")
                    elif state["daily_realized_pnl_usd"] <= -abs(config.MAX_DAILY_REALIZED_LOSS_USD):
                        print("Daily realized-loss limit reached; BUY skipped.")
                    elif config.PAPER_MODE:
                        paper_buy(state, price)
                    else:
                        live_buy(exchange, state, price, signal_candle_ms)

                # Mark this completed candle processed only after its action/decision.
                state["last_signal_candle_ms"] = signal_candle_ms

            save_state(config.BOT_STATE_PATH, state)

            candle_time = pd.to_datetime(signal_candle_ms, unit="ms", utc=True)
            if config.PAPER_MODE:
                print(
                    f"  price=${price:,.2f}  closed_candle={candle_time}  signal={signal}  "
                    f"position={state['bot_position_btc']:.6f} BTC  "
                    f"total_value=${paper_total_value(state, price):,.2f}"
                )
            else:
                print(
                    f"  price=${price:,.2f}  closed_candle={candle_time}  signal={signal}  "
                    f"bot_position={state['bot_position_btc']:.8f} BTC  "
                    f"trades_today={state['new_trades_today']}  "
                    f"daily_realized_P/L=${state['daily_realized_pnl_usd']:+,.2f}"
                )

        except Exception as exc:
            print(f"Error in trading loop: {type(exc).__name__}: {exc}")
            if not config.PAPER_MODE and state.get("pending_order"):
                print("LIVE SAFETY LOCK: unresolved pending order detected. Exiting for manual review.")
                sys.exit(2)

        time.sleep(config.POLL_SECONDS)


if __name__ == "__main__":
    main()
