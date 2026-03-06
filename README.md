# IBKR Dual Momentum Bot

Automated stock trading bot implementing Gary Antonacci's Dual Momentum strategy via Interactive Brokers. Trades a universe of S&P 500 + Swiss Market Index (SMI) stocks with monthly rebalancing.

## Quick Start

```bash
# 1. Set up environment
cd /root/projects/ibkr-momentum-bot
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env with your IBKR and Telegram credentials

# 3. Verify
python scripts/verify.py

# 4. Test run
python main.py --run-now

# 5. Deploy
sudo cp systemd/ibkr-momentum-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ibkr-momentum-bot
sudo systemctl start ibkr-momentum-bot
sudo journalctl -u ibkr-momentum-bot -f
```

## Strategy

- **Absolute momentum**: SPY vs IEF 6-month return with 3% buffer
- **Relative momentum**: Top 20 stocks by 6-month return (skip last month)
- **Defensive**: 100% cash when SPY underperforms IEF
- **Backtest**: ~10-11% CAGR, -46% max DD, 2008 DD only -7.3%

## IB Gateway Setup

The bot connects to Interactive Brokers via [IB Gateway](https://github.com/gnzsnz/ib-gateway-docker) running in Docker. A ready-to-use docker-compose template is included in `ib-gateway/`.

```bash
# 1. Configure credentials
cd ib-gateway
cp .env.example .env
# Edit .env with your IBKR username and password

# 2. Start IB Gateway
docker compose up -d
sleep 90   # wait for login to complete
docker compose logs | grep "Login has completed"
```

### Paper vs Live Trading

IB Gateway manages paper/live switching — the bot just connects to whichever mode IB Gateway is logged into.

To switch from paper to live:
1. Update `ib-gateway/.env`: `TRADING_MODE=live`
2. Restart IB Gateway: `cd ib-gateway && docker compose restart`
3. Wait 90 seconds for login
4. Update bot `.env`: `PAPER_TRADING=false`
5. Restart bot: `sudo systemctl restart ibkr-momentum-bot`

To switch back to paper: reverse the above (`TRADING_MODE=paper`, `PAPER_TRADING=true`).

**Important:** `PAPER_TRADING` in the bot `.env` controls logging labels and order simulation only. The actual account (paper vs live) is determined by IB Gateway's `TRADING_MODE`. Always keep both in sync.

### Port Configuration

The gnzsnz/ib-gateway image uses socat to forward internal port 4002 to external port 4004. Connect the bot to port 4004 (`IBKR_PORT=4004` in `.env`).

### Market Data Subscriptions

Required subscriptions in IBKR Account Management:
- **US stocks**: included by default
- **SIX Swiss Exchange (NP,L1)**: CHF 6.50/month — required for SMI stocks

To subscribe: IBKR Account Management > Settings > Market Data Subscriptions > search "SIX Swiss Exchange"

Without the Swiss subscription, the bot will skip SMI stocks silently and run on the S&P 500 universe only (still fully functional).

## Requirements

- Python 3.10+
- Docker (for IB Gateway)
- IB Gateway running on port 4004
- Telegram bot token and chat ID
- IBKR account (paper or live)
