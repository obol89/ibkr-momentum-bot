# IBKR Dual Momentum Bot

## Strategy
Dual Momentum (Antonacci) applied to US (S&P 500) + Swiss (SMI) stock universe.
Chosen after 20-year backtest (2005-2026) across 600+ assets.

**Backtested performance:** ~10-11% CAGR, -46% max DD, Sharpe ~0.65
**Key result:** 2008 drawdown only -7.3% (vs SPY -55%) due to defensive switching.

### Logic
1. **Absolute momentum** (monthly, first trading day):
   - If SPY 6mo return < IEF 6mo return - 3% buffer -> 100% cash (defensive)
   - The 3% buffer prevents whipsaw in choppy markets
2. **Relative momentum** (if not defensive):
   - Score each stock: price return from -126 to -21 days (skip last month)
   - Buy top 20 stocks by momentum score, equal weight
   - Quality filter: price > $5, 1yr history, exclude >300% or <-50% 12mo returns
3. **Defensive asset:** 100% cash (not bonds - avoids 2022-style bond drawdown)
4. **Rebalance:** Monthly on first trading day

### Parameters
- Lookback: 126 trading days (6 months)
- Skip: 21 trading days (1 month) - standard Dual Momentum practice
- Buffer: 3% on absolute momentum threshold
- Portfolio: N=20, equal weight
- Min position: $1,000 USD

## Architecture

```
main.py       -> PID lock, signal handling, logging, entry point
bot.py        -> APScheduler, monthly rotation orchestration, Telegram commands
ibkr.py       -> ib_insync wrapper (connect, orders, positions, data)
momentum.py   -> momentum scoring, universe definition, signal generation
portfolio.py  -> portfolio construction, rebalancing, order diff
notifier.py   -> Telegram notifications + command handlers
ledger.py     -> append-only JSON trade log
config.py     -> all configuration from .env
```

## Key Technical Decisions

- **Port 4004**: socat-forwarded from IB Gateway Docker container (~/.ib-gateway/docker-compose.yml)
- **Cash defensive**: not bonds - avoids 2022-style bond drawdown where IEF lost ~15%
- **6-month lookback + 3% buffer**: chosen after parameter optimization in 20yr backtest
- **Skip last month**: standard Dual Momentum practice to avoid short-term reversal
- **IBKR paper account**: confirmed ~CHF 1M value for testing
- **Swiss stocks**: trade on EBS exchange with CHF currency, converted via IBKR FX rates

## Configuration

- **Restart required**: IBKR connection settings, PAPER_TRADING, Telegram tokens
- **Hot-reload (no restart)**: MONTHLY_INVESTMENT, PORTFOLIO_SIZE (read from .env each run)

## Commands

```bash
# Run immediately (test mode)
python main.py --run-now

# Pre-flight checks
python scripts/verify.py

# View logs
journalctl -u ibkr-momentum-bot -f
tail -f logs/bot.log

# Service management
sudo systemctl start ibkr-momentum-bot
sudo systemctl stop ibkr-momentum-bot
sudo systemctl status ibkr-momentum-bot
```

## Scheduled Messages

- **Daily heartbeat**: 08:00 UTC, skipped on rebalance day (first trading day of month)
  - Portfolio value, position count, cash, SPY/IEF signal, IBKR connection status
  - Sends error alert if IBKR connection is down
- **Monthly rebalance**: first trading day of month at configured REBALANCE_TIME
  - Full rebalance report with buys/sells/holdings

## Telegram Commands

- `/status`   - full portfolio snapshot
- `/holdings` - current positions with momentum scores
- `/history`  - last 6 monthly rebalances
- `/balance`  - IBKR account summary
- `/next`     - preview next rebalance signal
- `/report`   - strategy summary and backtest stats

## Troubleshooting

- **Telegram /help (or any command) responds multiple times**: Always caused by multiple
  bot instances running simultaneously, each with its own Telegram poller. Fix:
  ```bash
  sudo systemctl stop ibkr-momentum-bot
  pkill -f "python main.py"
  sudo systemctl start ibkr-momentum-bot
  ```
  The PID lock in main.py now auto-kills stale instances on startup, but manual cleanup
  may be needed if processes were started outside systemd.

- **ib_insync thread affinity**: The IB object must be used from the thread it was
  connected in. Telegram command handlers must NOT call IBKR methods directly.
  Instead, bot.py caches state via `refresh_state()` (runs every 5 min on the
  scheduler thread) and command handlers read from the cache.

## Claude Code Session Rules

**NEVER run these commands during any Claude Code session:**
- systemctl restart/stop/start (any service)
- pkill, killall, kill (any process)
- sudo commands that affect running services

**After making code changes:**
- Tell the user which service needs restarting
- The user will run the restart manually
- Never restart services autonomously

**To test changes:**
- Tell the user which command to run (e.g. python main.py --run-now)
- Never execute test runs autonomously

## IB Gateway

- **Production location**: `~/ib-gateway/` (separate from bot code, contains real `.env`)
- **Repo template**: `ib-gateway/` subdirectory in this repo (docker-compose.yml + .env.example)
- Port: 4004 (socat-forwarded, internal 4002 -> external 4004)
- Paper account confirmed working
- **Paper/live switching** requires updating BOTH:
  1. IB Gateway `TRADING_MODE` in `~/ib-gateway/.env`
  2. Bot `PAPER_TRADING` in bot `.env`
  These must always be in sync — IB Gateway controls which account is used,
  bot's PAPER_TRADING controls logging labels only.
