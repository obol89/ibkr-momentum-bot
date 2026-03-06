"""Portfolio construction, rebalancing logic, and order diff calculation."""

from __future__ import annotations

import logging
import math
from decimal import Decimal

import config
from ibkr import IBKRClient
from momentum import SMI_STOCKS

logger = logging.getLogger(__name__)


def compute_target_positions(
    ranked_stocks: list[tuple[str, float]],
    portfolio_value_usd: Decimal,
    n: int | None = None,
) -> dict[str, int]:
    """Compute target share counts for top-N stocks, equal weight.

    Args:
        ranked_stocks: List of (symbol, score) sorted descending.
        portfolio_value_usd: Total portfolio value in USD.
        n: Number of stocks to hold (default: config.PORTFOLIO_SIZE).

    Returns:
        Dict of {symbol: target_shares}.
    """
    n = n or config.PORTFOLIO_SIZE
    top_n = ranked_stocks[:n]
    if not top_n:
        return {}

    weight = Decimal("1") / Decimal(str(len(top_n)))
    target_value_per_stock = portfolio_value_usd * weight

    targets: dict[str, int] = {}
    for symbol, _score in top_n:
        # We need the current price to compute share count
        # This is filled in by the caller via last_prices
        targets[symbol] = 0  # placeholder

    return targets


def compute_target_shares(
    ranked_stocks: list[tuple[str, float]],
    portfolio_value_usd: Decimal,
    last_prices: dict[str, Decimal],
    n: int | None = None,
) -> dict[str, int]:
    """Compute target share counts for top-N stocks, equal weight.

    Args:
        ranked_stocks: List of (symbol, score) sorted descending.
        portfolio_value_usd: Total portfolio value in USD.
        last_prices: Dict of {symbol: last_price_usd}.
        n: Number of stocks to hold.

    Returns:
        Dict of {symbol: target_shares}.
    """
    n = n or config.PORTFOLIO_SIZE
    top_n = ranked_stocks[:n]
    if not top_n:
        return {}

    weight = Decimal("1") / Decimal(str(len(top_n)))
    target_value_per_stock = portfolio_value_usd * weight

    targets: dict[str, int] = {}
    for symbol, _score in top_n:
        price = last_prices.get(symbol)
        if not price or price <= 0:
            logger.warning("No price for %s, skipping", symbol)
            continue
        shares = int(target_value_per_stock / price)
        position_value = Decimal(str(shares)) * price
        if position_value < config.MIN_POSITION_USD:
            logger.info(
                "Skipping %s: position $%s < min $%s",
                symbol,
                position_value,
                config.MIN_POSITION_USD,
            )
            continue
        targets[symbol] = shares

    return targets


def compute_rebalance_orders(
    current_positions: dict[str, tuple[Decimal, Decimal]],
    target_shares: dict[str, int],
) -> list[tuple[str, str, int]]:
    """Compute the list of orders needed to rebalance.

    Args:
        current_positions: {symbol: (quantity, avg_cost)} from IBKR.
        target_shares: {symbol: target_quantity}.

    Returns:
        List of (symbol, action, quantity) sorted sells-first then buys.
    """
    sells: list[tuple[str, str, int]] = []
    buys: list[tuple[str, str, int]] = []

    # Current symbols held
    current_symbols = set(current_positions.keys())
    target_symbols = set(target_shares.keys())

    # Sells: stocks leaving portfolio or reducing
    for symbol in current_symbols:
        current_qty = int(current_positions[symbol][0])
        target_qty = target_shares.get(symbol, 0)
        diff = current_qty - target_qty
        if diff > 0:
            sells.append((symbol, "SELL", diff))

    # Buys: stocks entering portfolio or increasing
    for symbol in target_symbols:
        current_qty = int(current_positions.get(symbol, (Decimal("0"), Decimal("0")))[0])
        target_qty = target_shares[symbol]
        diff = target_qty - current_qty
        if diff > 0:
            buys.append((symbol, "BUY", diff))

    # Execute sells first, then buys
    return sells + buys


def execute_rebalance(
    client: IBKRClient,
    orders: list[tuple[str, str, int]],
) -> list[dict]:
    """Execute a list of rebalance orders and return execution results."""
    results: list[dict] = []
    for symbol, action, quantity in orders:
        if quantity <= 0:
            continue
        try:
            result = client.place_market_order(symbol, action, quantity)
            results.append(result)
            logger.info("Order executed: %s %d %s -> %s", action, quantity, symbol, result.get("status"))
        except Exception:
            logger.error("Failed to execute order: %s %d %s", action, quantity, symbol, exc_info=True)
            results.append({
                "symbol": symbol,
                "action": action,
                "quantity": quantity,
                "status": "error",
            })
    return results


def get_last_prices_usd(
    client: IBKRClient,
    symbols: list[str],
    price_data: dict[str, object],
    fx_rate: Decimal | None = None,
) -> dict[str, Decimal]:
    """Get last close prices in USD for all symbols.

    Swiss stocks are converted using the provided fx_rate (CHF->USD).
    """
    last_prices: dict[str, Decimal] = {}
    for symbol in symbols:
        prices = price_data.get(symbol)
        if prices is None or prices.empty:
            continue
        price_local = Decimal(str(float(prices.iloc[-1])))
        if symbol in SMI_STOCKS and fx_rate:
            # Convert CHF price to USD: USD = CHF / (USD/CHF rate)
            price_usd = price_local / fx_rate
        else:
            price_usd = price_local
        last_prices[symbol] = price_usd
    return last_prices
