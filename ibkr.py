"""IBKR connection wrapper using ib_insync."""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from decimal import Decimal
from typing import Any

import pandas as pd
import requests
from ib_insync import IB, Contract, Forex, MarketOrder, Stock, util

import config

logger = logging.getLogger(__name__)

# Silence ib_insync's verbose logging
util.logToConsole(level=logging.WARNING)

# ---------------------------------------------------------------------------
# FX rate cache (module-level, survives across calls within one run)
# ---------------------------------------------------------------------------
_fx_cache: dict[str, tuple[float, Decimal]] = {}  # "USD/CHF" -> (timestamp, rate)
_FX_CACHE_TTL = 300  # 5 minutes


def _fetch_fx_kraken(base: str, quote: str) -> Decimal | None:
    """Fetch FX rate from Kraken public API (no auth needed)."""
    pair = f"{base}{quote}"
    try:
        resp = requests.get(
            "https://api.kraken.com/0/public/Ticker",
            params={"pair": pair},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            logger.warning("Kraken API error: %s", data["error"])
            return None
        result = data.get("result", {})
        # Kraken may use the pair key directly or a variant
        for key, val in result.items():
            rate = float(val["c"][0])  # "c" = last trade close [price, lot_volume]
            if rate > 0:
                logger.info("FX rate from Kraken: %s = %s", pair, rate)
                return Decimal(str(rate))
    except Exception:
        logger.warning("Kraken FX fetch failed", exc_info=True)
    return None


def _fetch_fx_ecb(base: str, quote: str) -> Decimal | None:
    """Fetch FX rate from ECB daily XML feed. Computes cross-rates via EUR."""
    try:
        resp = requests.get(
            "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml",
            timeout=5,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        ns = {"ecb": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"}
        rates: dict[str, float] = {"EUR": 1.0}
        for cube in root.findall(".//ecb:Cube[@currency]", ns):
            rates[cube.attrib["currency"]] = float(cube.attrib["rate"])
        if base in rates and quote in rates:
            # EUR/base and EUR/quote -> base/quote = (EUR/quote) / (EUR/base)
            rate = rates[quote] / rates[base]
            logger.info("FX rate from ECB: %s/%s = %s", base, quote, rate)
            return Decimal(str(round(rate, 6)))
    except Exception:
        logger.warning("ECB FX fetch failed", exc_info=True)
    return None


class IBKRClient:
    """Wrapper around ib_insync for connection management, data, and orders."""

    def __init__(self) -> None:
        self.ib = IB()
        self._connected = False

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self, retries: int = 3, delay: int = 10) -> None:
        """Connect to IB Gateway with retry logic."""
        for attempt in range(1, retries + 1):
            try:
                logger.info(
                    "Connecting to IBKR %s:%s (attempt %d/%d)",
                    config.IBKR_HOST,
                    config.IBKR_PORT,
                    attempt,
                    retries,
                )
                self.ib.connect(
                    config.IBKR_HOST,
                    config.IBKR_PORT,
                    clientId=config.IBKR_CLIENT_ID,
                    timeout=20,
                )
                self._connected = True
                logger.info("Connected to IBKR successfully")
                return
            except Exception:
                logger.warning("Connection attempt %d failed", attempt, exc_info=True)
                if attempt < retries:
                    time.sleep(delay)
        raise ConnectionError(
            f"Failed to connect to IBKR after {retries} attempts"
        )

    def disconnect(self) -> None:
        """Disconnect from IB Gateway."""
        if self._connected:
            self.ib.disconnect()
            self._connected = False
            logger.info("Disconnected from IBKR")

    def is_connected(self) -> bool:
        """Check if the connection is alive."""
        return self._connected and self.ib.isConnected()

    def ensure_connected(self) -> None:
        """Reconnect if the connection was lost."""
        if not self.is_connected():
            logger.warning("Connection lost, reconnecting...")
            self.connect()

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def get_account_summary(self) -> dict[str, Any]:
        """Return key account values: NetLiquidation, TotalCashValue, Currency."""
        self.ensure_connected()
        summary_items = self.ib.accountSummary()
        result: dict[str, Any] = {}
        for item in summary_items:
            if item.tag in ("NetLiquidation", "TotalCashValue", "AvailableFunds"):
                result[f"{item.tag}_{item.currency}"] = Decimal(item.value)
            if item.tag == "NetLiquidation" and item.currency == config.REPORTING_CURRENCY:
                result["NetLiquidation"] = Decimal(item.value)
            if item.tag == "TotalCashValue" and item.currency == config.REPORTING_CURRENCY:
                result["TotalCashValue"] = Decimal(item.value)
        return result

    def get_cash_balance(self) -> Decimal:
        """Return available USD cash."""
        self.ensure_connected()
        summary_items = self.ib.accountSummary()
        for item in summary_items:
            if item.tag == "TotalCashValue" and item.currency == "USD":
                return Decimal(item.value)
        # Fallback: look through account values
        for item in summary_items:
            if item.tag == "CashBalance" and item.currency == "USD":
                return Decimal(item.value)
        return Decimal("0")

    def get_net_liquidation(self, currency: str | None = None) -> Decimal:
        """Return net liquidation value in the specified currency."""
        currency = currency or config.REPORTING_CURRENCY
        self.ensure_connected()
        summary_items = self.ib.accountSummary()
        for item in summary_items:
            if item.tag == "NetLiquidation" and item.currency == currency:
                return Decimal(item.value)
        return Decimal("0")

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def get_positions(self) -> dict[str, tuple[Decimal, Decimal]]:
        """Return current holdings as {symbol: (quantity, avg_cost)}."""
        self.ensure_connected()
        positions = self.ib.positions()
        result: dict[str, tuple[Decimal, Decimal]] = {}
        for pos in positions:
            symbol = pos.contract.symbol
            result[symbol] = (Decimal(str(pos.position)), Decimal(str(pos.avgCost)))
        return result

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def _make_contract(self, symbol: str) -> Contract:
        """Create the correct contract for US or Swiss stocks."""
        from momentum import SMI_STOCKS
        if symbol in SMI_STOCKS:
            return Stock(symbol, "EBS", "CHF")
        return Stock(symbol, "SMART", "USD")

    _hist_request_count: int = 0

    def get_historical_data(
        self, symbol: str, lookback_days: int = 180
    ) -> pd.DataFrame:
        """Fetch daily OHLCV data as a DataFrame with a 'close' column.

        Pacing: 1s between requests, 10s pause every 50 requests.
        Retries once (with 10s backoff) on empty data or connectivity loss.
        """
        self.ensure_connected()
        contract = self._make_contract(symbol)
        try:
            self.ib.qualifyContracts(contract)
        except Exception:
            logger.warning("Failed to qualify contract for %s", symbol)
            return pd.DataFrame()

        # Pacing: IBKR allows ~50 historical requests per 10 minutes
        self._hist_request_count += 1
        if self._hist_request_count % 50 == 0:
            logger.info("Pacing pause: 10s after %d requests", self._hist_request_count)
            time.sleep(10)
        else:
            time.sleep(1)

        bars = self._fetch_bars(contract, symbol)

        # Retry once on failure with longer backoff
        if not bars:
            logger.info("No data for %s, retrying in 10s...", symbol)
            time.sleep(10)
            self.ensure_connected()
            bars = self._fetch_bars(contract, symbol)

        if not bars:
            logger.warning("No historical data for %s after retry", symbol)
            return pd.DataFrame()

        df = util.df(bars)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        return df

    def _fetch_bars(self, contract: Contract, symbol: str) -> list:
        """Single attempt to fetch historical bars, catching timeouts."""
        try:
            return self.ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr="1 Y",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
            )
        except Exception:
            logger.warning("reqHistoricalData exception for %s", symbol, exc_info=True)
            return []

    def get_close_prices(self, symbol: str, lookback_days: int = 180) -> pd.Series:
        """Return a Series of closing prices for the symbol."""
        df = self.get_historical_data(symbol, lookback_days)
        if df.empty:
            return pd.Series(dtype=float)
        return df["close"]

    # ------------------------------------------------------------------
    # FX
    # ------------------------------------------------------------------

    def get_fx_rate(self, base: str = "USD", quote: str = "CHF") -> Decimal:
        """Fetch FX rate via Kraken -> ECB -> hardcoded fallback.

        Results are cached for 5 minutes to avoid re-fetching mid-rebalance.
        """
        cache_key = f"{base}/{quote}"
        now = time.time()
        if cache_key in _fx_cache:
            ts, cached_rate = _fx_cache[cache_key]
            if now - ts < _FX_CACHE_TTL:
                return cached_rate

        # 1. Kraken public API
        rate = _fetch_fx_kraken(base, quote)
        if rate is not None:
            _fx_cache[cache_key] = (now, rate)
            return rate

        # 2. ECB XML feed
        rate = _fetch_fx_ecb(base, quote)
        if rate is not None:
            _fx_cache[cache_key] = (now, rate)
            return rate

        # 3. Hardcoded fallback
        logger.warning("All FX sources failed for %s/%s, using 0.90 fallback", base, quote)
        return Decimal("0.90")

    def get_portfolio_value_in_currency(
        self, target_currency: str | None = None
    ) -> Decimal:
        """Get total portfolio value converted to target currency."""
        target_currency = target_currency or config.REPORTING_CURRENCY
        return self.get_net_liquidation(target_currency)

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def place_market_order(
        self, symbol: str, action: str, quantity: int
    ) -> dict[str, Any]:
        """Place a market order (BUY or SELL).

        In PAPER_TRADING mode, logs the order but does not place it.
        Returns order details dict.
        """
        self.ensure_connected()
        contract = self._make_contract(symbol)
        self.ib.qualifyContracts(contract)

        order_info = {
            "symbol": symbol,
            "action": action,
            "quantity": quantity,
            "paper": config.PAPER_TRADING,
        }

        if config.PAPER_TRADING:
            logger.info("[PAPER] %s %d %s", action, quantity, symbol)
            order_info["status"] = "simulated"
            return order_info

        order = MarketOrder(action, quantity)
        trade = self.ib.placeOrder(contract, order)
        logger.info("Placed order: %s %d %s", action, quantity, symbol)

        # Wait briefly for fill
        self.ib.sleep(5)
        order_info["status"] = trade.orderStatus.status
        order_info["fill_price"] = str(trade.orderStatus.avgFillPrice)
        return order_info
