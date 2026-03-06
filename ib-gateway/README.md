# IB Gateway Docker Setup

Run Interactive Brokers Gateway in Docker using [gnzsnz/ib-gateway](https://github.com/gnzsnz/ib-gateway).

## Setup

```bash
cp .env.example .env
# Edit .env with your IBKR credentials

docker compose up -d
sleep 90  # wait for login
docker compose logs | grep "Login has completed"
```

## Port

Internal port 4002 is forwarded to external port 4004 via socat.
The bot connects to `127.0.0.1:4004`.

## Trading Mode

Set `TRADING_MODE=paper` or `TRADING_MODE=live` in `.env`.
Always keep in sync with the bot's `PAPER_TRADING` setting.
