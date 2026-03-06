"""Append-only JSON trade log."""

from __future__ import annotations

import json
import logging
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import config

logger = logging.getLogger(__name__)


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder that handles Decimal types."""

    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, date):
            return o.isoformat()
        return super().default(o)


def _load_ledger() -> list[dict[str, Any]]:
    """Load existing ledger entries."""
    if not config.LEDGER_PATH.exists():
        return []
    try:
        text = config.LEDGER_PATH.read_text()
        if not text.strip():
            return []
        return json.loads(text)
    except (json.JSONDecodeError, OSError):
        logger.error("Failed to load ledger, starting fresh", exc_info=True)
        return []


def _save_ledger(entries: list[dict[str, Any]]) -> None:
    """Write ledger entries to disk."""
    config.LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.LEDGER_PATH.write_text(
        json.dumps(entries, indent=2, cls=DecimalEncoder) + "\n"
    )


def append_entry(entry: dict[str, Any]) -> None:
    """Append a single rebalance entry to the ledger."""
    entries = _load_ledger()
    entries.append(entry)
    _save_ledger(entries)
    logger.info("Ledger entry appended for %s", entry.get("date", "unknown"))


def get_entries(last_n: int | None = None) -> list[dict[str, Any]]:
    """Return ledger entries, optionally limited to the last N."""
    entries = _load_ledger()
    if last_n is not None:
        return entries[-last_n:]
    return entries


def build_entry(
    *,
    rebalance_date: date,
    mode: str,
    spy_6mo_return: float,
    ief_6mo_return: float,
    is_defensive: bool,
    stocks_sold: list[str],
    stocks_bought: list[str],
    portfolio_holdings: list[str],
    momentum_scores: dict[str, float],
    portfolio_value_usd: Decimal,
    portfolio_value_chf: Decimal,
    cash_usd: Decimal,
    usd_chf_rate: Decimal,
    total_pnl_chf: Decimal,
    total_pnl_pct: float,
) -> dict[str, Any]:
    """Build a standardized ledger entry."""
    return {
        "date": rebalance_date,
        "mode": mode,
        "spy_6mo_return": round(spy_6mo_return, 4),
        "ief_6mo_return": round(ief_6mo_return, 4),
        "defensive_buffer": float(config.DEFENSIVE_BUFFER),
        "is_defensive": is_defensive,
        "stocks_sold": stocks_sold,
        "stocks_bought": stocks_bought,
        "portfolio_holdings": portfolio_holdings,
        "momentum_scores": {k: round(v, 4) for k, v in momentum_scores.items()},
        "portfolio_value_usd": portfolio_value_usd,
        "portfolio_value_chf": portfolio_value_chf,
        "cash_usd": cash_usd,
        "usd_chf_rate": usd_chf_rate,
        "total_pnl_chf": total_pnl_chf,
        "total_pnl_pct": round(total_pnl_pct, 2),
        "paper_trading": config.PAPER_TRADING,
    }
