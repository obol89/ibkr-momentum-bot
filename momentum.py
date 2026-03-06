"""Momentum scoring, universe definition, and signal generation."""

from __future__ import annotations

import logging
from decimal import Decimal

import pandas as pd

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------

SP500_STOCKS: list[str] = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "TSLA", "BRK B",
    "AVGO", "JPM", "LLY", "UNH", "XOM", "V", "MA", "PG", "COST", "HD", "JNJ",
    "NFLX", "ABBV", "BAC", "CRM", "WMT", "KO", "PEP", "AMD", "ORCL", "MCD",
    "ACN", "TXN", "CSCO", "ADBE", "DHR", "TMO", "INTC", "WFC", "ABT", "NEE",
    "RTX", "INTU", "AMGN", "QCOM", "AMAT", "IBM", "GS", "MS", "CAT", "LOW",
    "T", "SPGI", "HON", "C", "DE", "BKNG", "MDLZ", "ADI", "GILD", "BLK",
    "SYK", "PLD", "CI", "ISRG", "CB", "VRTX", "MO", "CVS", "SCHW", "TJX",
    "REGN", "ELV", "EOG", "DUK", "SLB", "SO", "BSX", "HUM", "ZTS", "ICE",
    "CME", "EQIX", "PH", "KLAC", "SNPS", "CDNS", "MCHP", "MU", "APH", "TEL",
    "ETN", "ITW", "EMR", "FDX", "GD", "NOC", "LMT",
]

SMI_STOCKS: list[str] = [
    "NESN", "ROG", "NOVN", "UHR", "ABB", "ZURN", "SREN", "LONN", "AMS",
    "GEBN", "GIVN", "CFR", "SCMN", "BAER", "SLHN", "PGHN", "TEMN", "VACN",
    "SPSN", "LISN",
]

UNIVERSE: list[str] = SP500_STOCKS + SMI_STOCKS

# Benchmark symbols for absolute momentum check
SPY = "SPY"
IEF = "IEF"

# ---------------------------------------------------------------------------
# Momentum scoring
# ---------------------------------------------------------------------------


def compute_momentum_score(
    prices: pd.Series, lookback: int, skip: int
) -> float | None:
    """Return price return from -lookback to -skip days ago (skip last month).

    Args:
        prices: Series of closing prices, ordered chronologically.
        lookback: Number of trading days to look back (e.g. 126 for 6 months).
        skip: Number of recent trading days to skip (e.g. 21 for 1 month).

    Returns:
        Fractional return, or None if insufficient data.
    """
    if len(prices) < lookback:
        return None
    price_start = prices.iloc[-lookback]
    price_end = prices.iloc[-skip]
    if price_start <= 0:
        return None
    return float(price_end / price_start - 1)


def is_defensive(
    spy_prices: pd.Series,
    ief_prices: pd.Series,
    buffer: Decimal | None = None,
) -> tuple[bool, float, float]:
    """Absolute momentum check: go defensive if SPY underperforms IEF by buffer.

    Returns:
        (is_defensive, spy_6mo_return, ief_6mo_return)
    """
    if buffer is None:
        buffer = config.DEFENSIVE_BUFFER

    # For absolute momentum, use full lookback with skip=1 (most recent day)
    spy_ret = compute_momentum_score(spy_prices, lookback=config.LOOKBACK_DAYS, skip=1)
    ief_ret = compute_momentum_score(ief_prices, lookback=config.LOOKBACK_DAYS, skip=1)

    if spy_ret is None or ief_ret is None:
        logger.error("Cannot compute defensive signal: insufficient data")
        return True, 0.0, 0.0  # Default to defensive if data missing

    defensive = spy_ret < (ief_ret - float(buffer))
    logger.info(
        "Defensive check: SPY 6mo=%.2f%% | IEF 6mo=%.2f%% | buffer=%.1f%% | defensive=%s",
        spy_ret * 100,
        ief_ret * 100,
        float(buffer) * 100,
        defensive,
    )
    return defensive, spy_ret, ief_ret


def quality_filter(symbol: str, prices: pd.Series) -> bool:
    """Exclude illiquid, delisted, or anomalous stocks.

    Checks:
        - Last price >= $5
        - At least 240 trading days (~1 year) of history
          (IBKR "1 Y" returns ~251 bars, so 252 is too strict)
        - Annual return not > 300% or < -50% (anomalous)
    """
    if prices.empty or len(prices) < 240:
        return False
    if prices.iloc[-1] < 5:
        return False
    annual_return = prices.iloc[-1] / prices.iloc[-240] - 1
    if annual_return > 3.0 or annual_return < -0.5:
        return False
    return True


def rank_universe(
    price_data: dict[str, pd.Series],
) -> list[tuple[str, float]]:
    """Score and rank all universe stocks by momentum.

    Args:
        price_data: Dict mapping symbol -> closing price Series.

    Returns:
        List of (symbol, momentum_score) sorted descending by score,
        filtered by quality.
    """
    scored: list[tuple[str, float]] = []
    no_data = 0
    filtered = 0
    for symbol in UNIVERSE:
        prices = price_data.get(symbol)
        if prices is None or prices.empty:
            no_data += 1
            continue
        if not quality_filter(symbol, prices):
            logger.debug("Quality filter excluded: %s (len=%d)", symbol, len(prices))
            filtered += 1
            continue
        score = compute_momentum_score(
            prices, lookback=config.LOOKBACK_DAYS, skip=config.SKIP_DAYS
        )
        if score is not None:
            scored.append((symbol, score))

    logger.info(
        "Ranking: %d scored, %d no data, %d quality-filtered out of %d universe",
        len(scored), no_data, filtered, len(UNIVERSE),
    )
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored
