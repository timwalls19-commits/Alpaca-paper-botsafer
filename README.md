# BTC Trading Bot — Safe v2 (Paper Mode by Default)

This version keeps the same 12/26 moving-average crossover + RSI strategy, but adds operational safeguards before any future real-money use.

## Important

The default is still **paper trading**. Do not switch to live just because the code has safeguards. Paper results can differ from real fills, and this strategy can lose money.

## Safety improvements

- Strategy signals use **completed candles only**.
- A signal candle is processed only once, preventing repeated buys/sells from the same crossover.
- Stop-loss and take-profit now exist in both paper and live logic.
- Live buys are capped by an absolute dollar limit (`LIVE_MAX_TRADE_USD`, default `$25`).
- Daily new-trade limit (`MAX_NEW_TRADES_PER_DAY`, default `2`).
- Daily realized-loss lock (`MAX_DAILY_REALIZED_LOSS_USD`, default `$5`).
- Live sells are limited to the BTC amount tracked as the bot's own position.
- Bot state is saved to disk.
- An unresolved/pending live order locks the bot instead of guessing whether an order filled.
- API credentials come from environment variables, not source code.
- Live mode requires both `PAPER_MODE=false` and `LIVE_TRADING_ENABLED=YES_I_UNDERSTAND`.

## Railway start command

```text
python trade_bot.py
```

## Paper mode

You do not need API keys. With no Railway variables set, `PAPER_MODE` defaults to true.

```bash
pip install -r requirements.txt
python trade_bot.py
```

The state file defaults to `paper_state.json`. If the container is redeployed without persistent storage, that file can be lost and the paper balance can reset.

## Before any future live-money switch

Do **all** of these first:

1. Keep paper testing long enough to evaluate multiple complete trades.
2. Create a Kraken API key with **trading permission only**. Do not enable withdrawal permission.
3. Add a persistent Railway volume and point `BOT_STATE_PATH` to a file on that volume (for example `/data/live_state.json`). Live mode should not be used without persistent state.
4. Add API credentials as Railway variables, never into `config.py`:
   - `KRAKEN_API_KEY`
   - `KRAKEN_API_SECRET`
5. Set conservative live limits before starting:
   - `LIVE_MAX_TRADE_USD=25`
   - `MAX_NEW_TRADES_PER_DAY=2`
   - `MAX_DAILY_REALIZED_LOSS_USD=5`
6. Only after reviewing everything, set both:
   - `PAPER_MODE=false`
   - `LIVE_TRADING_ENABLED=YES_I_UNDERSTAND`

If the state file is missing while BTC already exists in the account, the bot intentionally refuses to start live rather than guessing what BTC belongs to it.

## Files

- `config.py` — strategy settings, environment variables, safety limits
- `strategy.py` — MA/RSI signal logic
- `backtest.py` — historical strategy test
- `trade_bot.py` — paper/live loop with safeguards
- `requirements.txt` — dependencies

No software safeguard can eliminate trading risk, slippage, exchange/API failures, gaps, or strategy losses.
