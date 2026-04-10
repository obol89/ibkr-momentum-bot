"""APScheduler-based bot: monthly rotation logic, heartbeat, Telegram commands."""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pandas_market_calendars as mcal
from apscheduler.executors.pool import ThreadPoolExecutor as APSThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config
import ledger
import notifier
from ibkr import IBKRClient
from momentum import IEF, SPY, UNIVERSE, is_defensive, rank_universe
from portfolio import (
    compute_rebalance_orders,
    compute_target_shares,
    execute_rebalance,
    get_last_prices_usd,
)

logger = logging.getLogger(__name__)

_signal_refresh_lock = threading.Lock()


def get_first_trading_day(year: int, month: int) -> date:
    """Return the first trading day of the given month (NYSE calendar)."""
    nyse = mcal.get_calendar("NYSE")
    start = date(year, month, 1)
    end = date(year, month, 28)  # safe upper bound for early trading days
    schedule = nyse.schedule(start_date=start.isoformat(), end_date=end.isoformat())
    if schedule.empty:
        # Fallback: skip weekends manually
        d = start
        while d.weekday() >= 5:
            d += timedelta(days=1)
        return d
    return schedule.index[0].date()


def get_next_rebalance_date() -> date:
    """Return the next upcoming first trading day of a month."""
    today = date.today()
    ftd = get_first_trading_day(today.year, today.month)
    if ftd >= today:
        return ftd
    # Move to next month
    if today.month == 12:
        return get_first_trading_day(today.year + 1, 1)
    return get_first_trading_day(today.year, today.month + 1)


class MomentumBot:
    """Main bot orchestrating IBKR connection, scheduling, and Telegram."""

    def __init__(self) -> None:
        self.client = IBKRClient()
        # Single-thread executor: all jobs run on the SAME pool thread.
        # ib_insync requires thread affinity — the IB object must be used
        # from the thread it was connected on.  By connecting inside a
        # scheduler job (see start()) and using max_workers=1, every
        # subsequent job (refresh, heartbeat, rebalance) shares that thread.
        self.scheduler = BackgroundScheduler(
            timezone="UTC",
            executors={"default": APSThreadPoolExecutor(max_workers=1)},
        )
        self._last_momentum_scores: dict[str, float] = {}
        # Cached state served by Telegram command handlers (no IBKR calls
        # from command handlers — ib_insync must be used from its own thread).
        self._state: dict[str, Any] = {
            "portfolio_value_chf": Decimal("0"),
            "cash_usd": Decimal("0"),
            "usd_chf_rate": Decimal("0.9"),
            "positions": {},
            "account_summary": {},
            "last_spy_ret": 0.0,
            "last_ief_ret": 0.0,
            "is_defensive": False,
            "last_updated": None,
            "signal_updated": None,
        }

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def start(self, run_now: bool = False) -> None:
        """Initialize connections, schedule jobs, send startup notification."""
        logger.info("Starting IBKR Momentum Bot (paper=%s)", config.PAPER_TRADING)

        # Set up Telegram command handlers
        notifier.set_bot_reference(self)

        # Start the scheduler FIRST — all IBKR work happens on its pool
        # thread to maintain ib_insync thread affinity.
        self.scheduler.start()

        # Connect to IBKR from the scheduler's pool thread.
        # With max_workers=1, this thread will be reused for ALL subsequent
        # jobs (refresh, heartbeat, rebalance), satisfying thread affinity.
        init_done = threading.Event()
        init_error: list[BaseException | None] = [None]

        def _init_ibkr() -> None:
            try:
                # Pool threads don't have an asyncio event loop by default
                # (Python 3.10+).  ib_insync needs one for all its sync
                # wrappers.  Setting it once here is enough because
                # max_workers=1 reuses this same thread for every job.
                import asyncio
                asyncio.set_event_loop(asyncio.new_event_loop())

                self.client.connect()
                # Only fetch account state (positions, cash, etc.) during
                # init — NOT the signal refresh.  reqHistoricalData can
                # take 4+ minutes to fail when IBKR data service is down,
                # which would exceed the init timeout and crash the bot.
                # Signal refresh will happen on the first scheduled interval.
                self._refresh_account_state()
            except Exception as exc:
                init_error[0] = exc
            finally:
                init_done.set()

        self.scheduler.add_job(_init_ibkr, trigger="date", id="ibkr_init")
        if not init_done.wait(timeout=60):
            raise RuntimeError("IBKR initialization timed out (60s)")
        if init_error[0]:
            raise init_error[0]

        # Schedule recurring jobs (all run on the same pool thread)
        self.scheduler.add_job(
            self._check_and_rebalance,
            CronTrigger(
                day="1-7",
                hour=config.REBALANCE_HOUR,
                minute=config.REBALANCE_MINUTE,
            ),
            id="rebalance",
            name="Monthly rebalance check",
            misfire_grace_time=3600,
        )

        self.scheduler.add_job(
            self._heartbeat,
            CronTrigger(hour=config.HEARTBEAT_HOUR, minute=config.HEARTBEAT_MINUTE),
            id="heartbeat",
            name="Daily heartbeat",
            misfire_grace_time=3600,
        )

        self.scheduler.add_job(
            self.refresh_state,
            "interval",
            minutes=5,
            id="refresh_state",
            name="Refresh cached state",
            misfire_grace_time=60,
        )

        logger.info("Scheduler started with %d jobs", len(self.scheduler.get_jobs()))

        # Send startup notification (uses cached state from _init_ibkr)
        next_rebal = get_next_rebalance_date()
        portfolio_value = self._state["portfolio_value_chf"]

        current_mode = "PAPER" if config.PAPER_TRADING else "LIVE"
        startup_msg = notifier.format_startup(
            mode=current_mode,
            next_rebalance=f"{next_rebal.isoformat()} at {config.REBALANCE_TIME} UTC",
            paper=config.PAPER_TRADING,
            portfolio_value_chf=portfolio_value,
        )
        notifier.send_message(startup_msg)

        if run_now:
            logger.info("--run-now flag set, executing immediate rebalance")
            rebal_done = threading.Event()

            def _do_rebalance() -> None:
                self._run_rebalance()
                rebal_done.set()

            self.scheduler.add_job(
                _do_rebalance, trigger="date", id="immediate_rebalance"
            )
            rebal_done.wait(timeout=600)  # rebalance can take ~10 min

        # Start Telegram polling in background thread (own event loop)
        tg_thread = threading.Thread(
            target=notifier.run_telegram_in_thread, daemon=True
        )
        tg_thread.start()

    def stop(self) -> None:
        """Graceful shutdown."""
        logger.info("Stopping bot...")
        self.scheduler.shutdown(wait=False)
        self.client.disconnect()
        logger.info("Bot stopped")

    # ------------------------------------------------------------------
    # Scheduled jobs
    # ------------------------------------------------------------------

    def _check_and_rebalance(self) -> None:
        """Check if today is the first trading day of the month; if so, rebalance."""
        today = date.today()
        ftd = get_first_trading_day(today.year, today.month)
        if today != ftd:
            logger.debug(
                "Not first trading day (today=%s, ftd=%s), skipping", today, ftd
            )
            return
        logger.info("First trading day of month detected, running rebalance")
        self._run_rebalance()

    def _heartbeat(self) -> None:
        """Daily heartbeat: skip on rebalance day, otherwise send status to Telegram."""
        today = date.today()
        ftd = get_first_trading_day(today.year, today.month)
        if today == ftd:
            logger.info("Heartbeat skipped: rebalance day (full message already sent)")
            return

        # Verify IBKR connection
        ibkr_connected = False
        try:
            self.client.ensure_connected()
            ibkr_connected = self.client.is_connected()
            logger.info("Heartbeat OK: IBKR connected")
        except Exception:
            logger.error("Heartbeat FAILED: IBKR connection lost", exc_info=True)
            notifier.send_error("IBKR connection lost! Heartbeat check failed.")

        # Refresh state and fetch fresh signal before sending heartbeat
        self.refresh_state()
        # refresh_state calls refresh_signal, but if it failed silently,
        # try once more as a dedicated call (heartbeat runs on scheduler thread).
        if self._state["signal_updated"] is None or (
            datetime.utcnow() - self._state["signal_updated"]
        ).total_seconds() > 600:
            logger.info("Heartbeat: signal stale, forcing dedicated refresh")
            self.refresh_signal()

        s = self._state
        cash_chf = s["cash_usd"] * s["usd_chf_rate"]
        next_rebal = get_next_rebalance_date()

        msg = notifier.format_heartbeat(
            now=datetime.utcnow(),
            spy_ret=s["last_spy_ret"],
            ief_ret=s["last_ief_ret"],
            is_defensive=s["is_defensive"],
            portfolio_value_chf=s["portfolio_value_chf"],
            num_positions=len(s["positions"]),
            cash_chf=cash_chf,
            ibkr_connected=ibkr_connected,
            next_rebalance=next_rebal.isoformat(),
            paper=config.PAPER_TRADING,
        )
        notifier.send_message(msg)

    def _refresh_account_state(self) -> None:
        """Fetch account data (positions, cash, FX) from IBKR — no historical data.

        Fast operation that only queries account state. Used during init
        and called by refresh_state().
        """
        try:
            self.client.ensure_connected()
            self._state["positions"] = self.client.get_positions()
            self._state["portfolio_value_chf"] = self.client.get_portfolio_value_in_currency("CHF")
            self._state["cash_usd"] = self.client.get_cash_balance()
            self._state["usd_chf_rate"] = self.client.get_fx_rate("USD", "CHF")
            self._state["account_summary"] = self.client.get_account_summary()
            self._state["last_updated"] = datetime.utcnow()
            logger.debug("State refreshed: CHF %s, %d positions",
                         self._state["portfolio_value_chf"],
                         len(self._state["positions"]))
        except Exception:
            logger.warning("State refresh failed", exc_info=True)

    def refresh_state(self) -> None:
        """Fetch live data from IBKR and update cached _state.

        Called on a 5-minute interval and after each rebalance.
        Must run in the main/scheduler thread (same as IBKR connection).
        """
        self._refresh_account_state()
        self.refresh_signal()

    def refresh_signal(self) -> None:
        """Fetch live SPY/IEF prices from IBKR and recompute the momentum signal.

        Failures do not reset existing values to 0.0 (keeps previous values).
        Must run on the scheduler/main thread (ib_insync thread affinity).
        """
        with _signal_refresh_lock:
            try:
                self.client.ensure_connected()
                spy_prices = self.client.get_close_prices(SPY, lookback_days=180)
                ief_prices = self.client.get_close_prices(IEF, lookback_days=180)
                if not spy_prices.empty and not ief_prices.empty:
                    defensive, spy_ret, ief_ret = is_defensive(spy_prices, ief_prices)
                    self._state["last_spy_ret"] = spy_ret
                    self._state["last_ief_ret"] = ief_ret
                    self._state["is_defensive"] = defensive
                    self._state["signal_updated"] = datetime.utcnow()
                    logger.debug("Signal refreshed: SPY %.1f%% IEF %.1f%% defensive=%s",
                                 spy_ret * 100, ief_ret * 100, defensive)
                else:
                    logger.warning("Signal refresh: empty price data for SPY or IEF")
            except Exception:
                logger.warning("Signal refresh failed (keeping previous values)", exc_info=True)

    # ------------------------------------------------------------------
    # Core rebalance logic
    # ------------------------------------------------------------------

    def _run_rebalance(self) -> None:
        """Execute the full monthly rebalance."""
        try:
            self.client.ensure_connected()
            today = date.today()
            logger.info("Starting rebalance for %s", today)

            # 1. Fetch historical data for benchmarks
            spy_prices = self.client.get_close_prices(SPY, lookback_days=180)
            ief_prices = self.client.get_close_prices(IEF, lookback_days=180)

            # 2. Check defensive signal
            defensive, spy_ret, ief_ret = is_defensive(spy_prices, ief_prices)

            # Store signal in cached state for /next command
            self._state["last_spy_ret"] = spy_ret
            self._state["last_ief_ret"] = ief_ret
            self._state["is_defensive"] = defensive
            self._state["signal_updated"] = datetime.utcnow()

            # 3. Get current positions and account info
            current_positions = self.client.get_positions()
            fx_rate = self.client.get_fx_rate("USD", "CHF")
            portfolio_value_chf = self.client.get_portfolio_value_in_currency("CHF")
            cash_usd = self.client.get_cash_balance()

            if defensive:
                self._handle_defensive(
                    today, spy_ret, ief_ret, current_positions,
                    portfolio_value_chf, cash_usd, fx_rate,
                )
            else:
                self._handle_momentum(
                    today, spy_ret, ief_ret, current_positions,
                    portfolio_value_chf, cash_usd, fx_rate,
                )

        except Exception:
            logger.error("Rebalance failed", exc_info=True)
            notifier.send_error("Monthly rebalance failed! Check logs.")

    def _handle_defensive(
        self,
        today: date,
        spy_ret: float,
        ief_ret: float,
        current_positions: dict[str, tuple[Decimal, Decimal]],
        portfolio_value_chf: Decimal,
        cash_usd: Decimal,
        fx_rate: Decimal,
    ) -> None:
        """Sell all positions and go to cash."""
        logger.info("DEFENSIVE mode: selling all %d positions", len(current_positions))

        positions_sold = len(current_positions)
        stocks_sold = list(current_positions.keys())

        # Generate sell orders for all positions
        sell_orders = [
            (sym, "SELL", int(qty))
            for sym, (qty, _) in current_positions.items()
            if int(qty) > 0
        ]
        if sell_orders:
            execute_rebalance(self.client, sell_orders)

        # Estimate P&L (simplified: compare to initial investment)
        total_pnl_chf = Decimal("0")
        total_pnl_pct = 0.0

        # Log to ledger
        entry = ledger.build_entry(
            rebalance_date=today,
            mode="defensive",
            spy_6mo_return=spy_ret,
            ief_6mo_return=ief_ret,
            is_defensive=True,
            stocks_sold=stocks_sold,
            stocks_bought=[],
            portfolio_holdings=[],
            momentum_scores={},
            portfolio_value_usd=portfolio_value_chf / fx_rate if fx_rate else Decimal("0"),
            portfolio_value_chf=portfolio_value_chf,
            cash_usd=cash_usd,
            usd_chf_rate=fx_rate,
            total_pnl_chf=total_pnl_chf,
            total_pnl_pct=total_pnl_pct,
        )
        ledger.append_entry(entry)

        # Send Telegram notification
        msg = notifier.format_defensive_rebalance(
            rebalance_date=today,
            spy_ret=spy_ret,
            ief_ret=ief_ret,
            positions_sold=positions_sold,
            portfolio_value_chf=portfolio_value_chf,
            total_pnl_chf=total_pnl_chf,
            total_pnl_pct=total_pnl_pct,
            paper=config.PAPER_TRADING,
        )
        notifier.send_message(msg)

        # Refresh cached state after rebalance
        self.refresh_state()

    def _handle_momentum(
        self,
        today: date,
        spy_ret: float,
        ief_ret: float,
        current_positions: dict[str, tuple[Decimal, Decimal]],
        portfolio_value_chf: Decimal,
        cash_usd: Decimal,
        fx_rate: Decimal,
    ) -> None:
        """Score universe, select top N, rebalance."""
        logger.info("MOMENTUM mode: scoring universe")

        # Fetch historical data for all universe stocks
        price_data: dict[str, Any] = {}
        failed: list[str] = []
        total = len(UNIVERSE)
        for i, symbol in enumerate(UNIVERSE, 1):
            try:
                prices = self.client.get_close_prices(symbol, lookback_days=300)
                if not prices.empty:
                    price_data[symbol] = prices
                else:
                    failed.append(symbol)
            except Exception:
                failed.append(symbol)
                logger.warning("Failed to fetch data for %s", symbol, exc_info=True)
            if i % 20 == 0:
                logger.info("Fetched %d/%d stocks (%d with data)", i, total, len(price_data))

        logger.info(
            "Data fetch complete: %d/%d stocks have data, %d failed",
            len(price_data), total, len(failed),
        )
        if failed:
            logger.info("Failed symbols: %s", ", ".join(failed[:20]))

        # Rank stocks
        ranked = rank_universe(price_data)
        logger.info("Ranked %d stocks, top 5: %s", len(ranked), ranked[:5])

        # Store momentum scores for commands
        self._last_momentum_scores = {sym: score for sym, score in ranked}

        # Get last prices in USD for position sizing
        last_prices = get_last_prices_usd(
            self.client,
            [sym for sym, _ in ranked],
            price_data,
            fx_rate,
        )

        # Compute target shares using portfolio value in USD
        portfolio_value_usd = portfolio_value_chf / fx_rate if fx_rate else Decimal("0")
        target_shares = compute_target_shares(
            ranked, portfolio_value_usd, last_prices
        )

        # Compute rebalance orders
        orders = compute_rebalance_orders(current_positions, target_shares)

        stocks_sold = [sym for sym, action, _ in orders if action == "SELL"]
        stocks_bought = [sym for sym, action, _ in orders if action == "BUY"]

        logger.info(
            "Rebalance: %d sells, %d buys", len(stocks_sold), len(stocks_bought)
        )

        # Execute orders
        if orders:
            execute_rebalance(self.client, orders)

        # Build ledger entry
        holdings = list(target_shares.keys())
        scores_for_holdings = {
            sym: self._last_momentum_scores.get(sym, 0) for sym in holdings
        }
        top_scores = sorted(
            scores_for_holdings.items(), key=lambda x: x[1], reverse=True
        )

        entry = ledger.build_entry(
            rebalance_date=today,
            mode="momentum",
            spy_6mo_return=spy_ret,
            ief_6mo_return=ief_ret,
            is_defensive=False,
            stocks_sold=stocks_sold,
            stocks_bought=stocks_bought,
            portfolio_holdings=holdings,
            momentum_scores=scores_for_holdings,
            portfolio_value_usd=portfolio_value_usd,
            portfolio_value_chf=portfolio_value_chf,
            cash_usd=cash_usd,
            usd_chf_rate=fx_rate,
            total_pnl_chf=Decimal("0"),
            total_pnl_pct=0.0,
        )
        ledger.append_entry(entry)

        # Telegram notification
        # Compute cash in CHF
        cash_chf = cash_usd * fx_rate if fx_rate else Decimal("0")
        msg = notifier.format_momentum_rebalance(
            rebalance_date=today,
            spy_ret=spy_ret,
            ief_ret=ief_ret,
            stocks_sold=stocks_sold,
            stocks_bought=stocks_bought,
            top_scores=top_scores[:10],
            total_holdings=len(holdings),
            cash_chf=cash_chf,
            portfolio_value_chf=portfolio_value_chf,
            total_pnl_chf=Decimal("0"),
            total_pnl_pct=0.0,
            paper=config.PAPER_TRADING,
        )
        notifier.send_message(msg)

        # Refresh cached state after rebalance
        self.refresh_state()

    # ------------------------------------------------------------------
    # Telegram command helpers (read from cached _state — no IBKR calls)
    # ------------------------------------------------------------------

    def _last_updated_str(self) -> str:
        """Format 'Last updated: X minutes ago' from _state."""
        ts = self._state.get("last_updated")
        if ts is None:
            return "Last updated: never"
        delta = datetime.utcnow() - ts
        minutes = int(delta.total_seconds() / 60)
        if minutes < 1:
            return "Last updated: just now"
        return f"Last updated: {minutes}m ago"

    def _signal_updated_str(self) -> str:
        """Format 'Signal updated: X minutes ago' from _state."""
        ts = self._state.get("signal_updated")
        if ts is None:
            return "Signal updated: never"
        delta = datetime.utcnow() - ts
        minutes = int(delta.total_seconds() / 60)
        if minutes < 1:
            return "Signal updated: just now"
        return f"Signal updated: {minutes}m ago"

    def get_status_text(self) -> str:
        """Generate text for /status command from cached state."""
        s = self._state
        return notifier.format_status(
            s["positions"], s["portfolio_value_chf"], s["cash_usd"],
            s["usd_chf_rate"], config.PAPER_TRADING,
            last_updated=self._last_updated_str(),
        )

    def get_holdings_text(self) -> str:
        """Generate text for /holdings command from cached state."""
        return notifier.format_holdings(
            self._state["positions"], self._last_momentum_scores
        )

    def get_balance_text(self) -> str:
        """Generate text for /balance command from cached state."""
        summary = self._state["account_summary"]
        lines = ["\U0001f3e6 Account Balance", ""]
        for key, val in sorted(summary.items()):
            lines.append(f"   {key}: {val:,.2f}")
        lines.append("")
        lines.append(self._last_updated_str())
        return "\n".join(lines)

    def get_next_text(self) -> str:
        """Generate text for /next command from cached state."""
        next_date = get_next_rebalance_date()
        s = self._state
        spy_ret = s["last_spy_ret"]
        ief_ret = s["last_ief_ret"]
        if s["is_defensive"]:
            signal = "DEFENSIVE (would go to cash)"
        else:
            signal = "MOMENTUM (would invest)"

        return (
            f"\U0001f4c5 Next Rebalance Preview\n"
            f"\n"
            f"   Date: {next_date.strftime('%a %d %b %Y')} at {config.REBALANCE_TIME} UTC\n"
            f"   SPY 6mo: {spy_ret:+.1%} | IEF 6mo: {ief_ret:+.1%}\n"
            f"   Signal: {signal}\n"
            f"\n"
            f"{self._signal_updated_str()}"
        )

    def schedule_signal_refresh(self) -> threading.Event:
        """Schedule a signal refresh on the scheduler thread and return an Event.

        The caller can wait on the event to know when the refresh completes.
        This allows Telegram command handlers (which run on a different thread)
        to trigger a fresh signal fetch without violating ib_insync thread affinity.
        """
        done = threading.Event()

        def _do_refresh() -> None:
            self.refresh_signal()
            done.set()

        self.scheduler.add_job(
            _do_refresh,
            trigger="date",  # run immediately
            id="signal_refresh_ondemand",
            replace_existing=True,
            misfire_grace_time=30,
        )
        return done

    def get_report_text(self) -> str:
        """Generate text for /report command — strategy summary."""
        next_date = get_next_rebalance_date()
        entries = ledger.get_entries()
        last_mode = entries[-1]["mode"].upper() if entries else "N/A"
        return (
            f"\U0001f4ca Strategy Report\n"
            f"\n"
            f"   Strategy: Dual Momentum 6mo + 3% buffer\n"
            f"   Backtest: 20yr, ~10-11% CAGR, -46% max DD\n"
            f"   2008 result: -7.3% DD (vs SPY -55%)\n"
            f"   Current mode: {last_mode}\n"
            f"   Total rebalances: {len(entries)}\n"
            f"   Paper trading: {config.PAPER_TRADING}\n"
            f"   Next rebalance: {next_date.strftime('%a %d %b %Y')} at {config.REBALANCE_TIME} UTC"
        )
