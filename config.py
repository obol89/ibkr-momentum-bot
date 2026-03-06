"""Configuration loaded from .env file."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _env(key: str, default: str | None = None, required: bool = True) -> str:
    """Read an environment variable, raising if required and missing."""
    val = os.getenv(key, default)
    if required and not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val  # type: ignore[return-value]


# --- IBKR ---
IBKR_HOST: str = _env("IBKR_HOST", "127.0.0.1")
IBKR_PORT: int = int(_env("IBKR_PORT", "4004"))
IBKR_CLIENT_ID: int = int(_env("IBKR_CLIENT_ID", "1"))

# --- Telegram ---
TELEGRAM_BOT_TOKEN: str = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID: str = _env("TELEGRAM_CHAT_ID")

# --- Strategy ---
REBALANCE_DAY: int = int(_env("REBALANCE_DAY", "1"))
REBALANCE_TIME: str = _env("REBALANCE_TIME", "10:00")
LOOKBACK_DAYS: int = int(_env("LOOKBACK_DAYS", "126"))
SKIP_DAYS: int = int(_env("SKIP_DAYS", "21"))
DEFENSIVE_BUFFER: Decimal = Decimal(_env("DEFENSIVE_BUFFER", "0.03"))
PORTFOLIO_SIZE: int = int(_env("PORTFOLIO_SIZE", "20"))
MIN_POSITION_USD: Decimal = Decimal(_env("MIN_POSITION_USD", "1000"))

# --- Mode ---
PAPER_TRADING: bool = _env("PAPER_TRADING", "true").lower() == "true"
REPORTING_CURRENCY: str = _env("REPORTING_CURRENCY", "CHF")

# --- Budget ---
MONTHLY_INVESTMENT: Decimal = Decimal(_env("MONTHLY_INVESTMENT", "500"))

# --- Paths ---
DATA_DIR: Path = BASE_DIR / "data"
LEDGER_PATH: Path = DATA_DIR / "ledger.json"
LOG_DIR: Path = BASE_DIR / "logs"
PID_FILE: Path = Path("/tmp/ibkr-momentum-bot.pid")

# Ensure directories exist
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# --- Rebalance time parsed ---
REBALANCE_HOUR: int = int(REBALANCE_TIME.split(":")[0])
REBALANCE_MINUTE: int = int(REBALANCE_TIME.split(":")[1])

# Required env vars for verification
REQUIRED_ENV_VARS: list[str] = [
    "IBKR_HOST",
    "IBKR_PORT",
    "IBKR_CLIENT_ID",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
]
