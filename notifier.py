"""Telegram bot for notifications and interactive commands."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import requests as http_requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import config
import ledger

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Low-level send (pure HTTP, no async — avoids event loop conflicts with ib_insync)
# ---------------------------------------------------------------------------

_TG_API = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"


def send_message(text: str) -> None:
    """Send a Telegram message using the REST API directly.

    Uses synchronous requests to avoid event-loop conflicts with ib_insync.
    """
    try:
        resp = http_requests.post(
            f"{_TG_API}/sendMessage",
            json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": text,
            },
            timeout=10,
        )
        if not resp.ok:
            logger.error("Telegram API error %s: %s", resp.status_code, resp.text)
    except Exception:
        logger.error("Failed to send Telegram message", exc_info=True)


def send_error(error_msg: str) -> None:
    """Send an error alert via Telegram."""
    from datetime import datetime as _dt
    ts = _dt.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    text = f"\U0001f534 Bot Error \u2014 {ts}\n\n{error_msg}"
    send_message(text)


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def format_defensive_rebalance(
    rebalance_date: date,
    spy_ret: float,
    ief_ret: float,
    positions_sold: int,
    portfolio_value_chf: Decimal,
    total_pnl_chf: Decimal,
    total_pnl_pct: float,
    paper: bool = False,
) -> str:
    """Format the monthly rebalance message for defensive mode."""
    paper_tag = " [PAPER]" if paper else ""
    pnl_sign = "+" if total_pnl_chf >= 0 else ""
    return (
        f"\U0001f4c5 Monthly Rebalance \u2014 {rebalance_date.strftime('%a %d %b %Y')}{paper_tag}\n"
        f"\n"
        f"\U0001f6e1\ufe0f  DEFENSIVE MODE ACTIVATED\n"
        f"   SPY 6mo: {spy_ret:+.1%} | IEF 6mo: {ief_ret:+.1%}\n"
        f"   SPY < IEF \u2212 3% buffer \u2192 going to cash\n"
        f"\U0001f4b0 Sold all {positions_sold} positions\n"
        f"\U0001f4b5 Cash: CHF {portfolio_value_chf:,.0f}\n"
        f"\n"
        f"\U0001f4ca Portfolio\n"
        f"   Value: CHF {portfolio_value_chf:,.0f}\n"
        f"   P&L: {pnl_sign}CHF {abs(total_pnl_chf):,.0f} ({pnl_sign}{total_pnl_pct:.1f}%)"
    )


def format_momentum_rebalance(
    rebalance_date: date,
    spy_ret: float,
    ief_ret: float,
    stocks_sold: list[str],
    stocks_bought: list[str],
    top_scores: list[tuple[str, float]],
    total_holdings: int,
    cash_chf: Decimal,
    portfolio_value_chf: Decimal,
    total_pnl_chf: Decimal,
    total_pnl_pct: float,
    paper: bool = False,
) -> str:
    """Format the monthly rebalance message for momentum mode."""
    paper_tag = " [PAPER]" if paper else ""
    sold_str = ", ".join(stocks_sold) if stocks_sold else "none"
    bought_str = ", ".join(stocks_bought) if stocks_bought else "none"
    pnl_sign = "+" if total_pnl_chf >= 0 else ""

    top5 = ""
    for i, (sym, score) in enumerate(top_scores[:5], 1):
        top5 += f"   {i}. {sym}  {score:+.1%} (6mo score)\n"
    remaining = total_holdings - min(5, len(top_scores))

    return (
        f"\U0001f4c5 Monthly Rebalance \u2014 {rebalance_date.strftime('%a %d %b %Y')}{paper_tag}\n"
        f"\n"
        f"\U0001f680 MOMENTUM MODE\n"
        f"   SPY 6mo: {spy_ret:+.1%} | IEF 6mo: {ief_ret:+.1%}\n"
        f"   Momentum confirmed\n"
        f"\n"
        f"\U0001f504 Changes: {len(stocks_sold)} sold, {len(stocks_bought)} bought\n"
        f"   Sold: {sold_str}\n"
        f"   Bought: {bought_str}\n"
        f"\U0001f4cb Holdings (Top 5 by momentum):\n"
        f"{top5}"
        f"   ... +{remaining} more\n"
        f"\n"
        f"\U0001f4ca Portfolio\n"
        f"   Stocks: {total_holdings} | Cash: CHF {cash_chf:,.0f}\n"
        f"   Value: CHF {portfolio_value_chf:,.0f}\n"
        f"   P&L: {pnl_sign}CHF {abs(total_pnl_chf):,.0f} ({pnl_sign}{total_pnl_pct:.1f}%)"
    )


def format_startup(
    mode: str,
    next_rebalance: str,
    paper: bool,
    portfolio_value_chf: Decimal,
) -> str:
    """Format the startup notification."""
    paper_tag = " [PAPER]" if paper else ""
    return (
        f"\U0001f7e2 Bot started{paper_tag}\n"
        f"Mode: {mode}\n"
        f"Portfolio: CHF {portfolio_value_chf:,.0f}\n"
        f"Next rebalance: {next_rebalance}"
    )


def format_heartbeat(
    now: datetime,
    spy_ret: float,
    ief_ret: float,
    is_defensive: bool,
    portfolio_value_chf: Decimal,
    num_positions: int,
    cash_chf: Decimal,
    ibkr_connected: bool,
    next_rebalance: str,
    paper: bool,
) -> str:
    """Format the daily heartbeat message."""
    paper_tag = " [PAPER]" if paper else ""
    mode_str = "DEFENSIVE \u274c" if is_defensive else "MOMENTUM \u2705"
    conn_str = "Connected \u2705" if ibkr_connected else "DISCONNECTED \u274c"
    day_str = now.strftime("%a %d %b %Y")
    time_str = now.strftime("%H:%M UTC")

    return (
        f"\U0001f493 IBKR Bot \u2014 Daily Heartbeat\n"
        f"\U0001f4c5 {day_str} | {time_str}\n"
        f"\n"
        f"\U0001f4ca Market Signal\n"
        f"   SPY 6mo: {spy_ret:+.1%} | IEF 6mo: {ief_ret:+.1%}\n"
        f"   Mode: {mode_str}\n"
        f"\n"
        f"\U0001f4bc Portfolio{paper_tag}\n"
        f"   Value: CHF {portfolio_value_chf:,.0f}\n"
        f"   Positions: {num_positions} stocks\n"
        f"   Cash: CHF {cash_chf:,.0f}\n"
        f"\n"
        f"\U0001f50c IBKR: {conn_str}\n"
        f"\U0001f4c5 Next rebalance: {next_rebalance}"
    )


def format_status(
    positions: dict[str, tuple[Decimal, Decimal]],
    portfolio_value_chf: Decimal,
    cash_usd: Decimal,
    usd_chf_rate: Decimal,
    paper: bool,
    last_updated: str = "",
) -> str:
    """Format the /status response."""
    paper_tag = " [PAPER]" if paper else ""
    lines = [
        f"\U0001f4bc Portfolio Status{paper_tag}",
        f"",
        f"   Value: CHF {portfolio_value_chf:,.0f}",
        f"   Cash: USD {cash_usd:,.0f} (CHF {cash_usd * usd_chf_rate:,.0f})",
        f"   USD/CHF: {usd_chf_rate:.4f}",
        f"   Positions: {len(positions)}",
    ]
    if positions:
        lines.append("")
        for sym, (qty, cost) in sorted(positions.items()):
            lines.append(f"   {sym}: {qty} shares @ ${cost:.2f}")
    if last_updated:
        lines.append("")
        lines.append(last_updated)
    return "\n".join(lines)


def format_holdings(
    positions: dict[str, tuple[Decimal, Decimal]],
    momentum_scores: dict[str, float],
) -> str:
    """Format the /holdings response."""
    if not positions:
        return "No current holdings."

    lines = [f"\U0001f4cb Current Holdings", ""]
    # Sort by momentum score descending
    sorted_pos = sorted(
        positions.items(),
        key=lambda x: momentum_scores.get(x[0], 0),
        reverse=True,
    )
    for sym, (qty, cost) in sorted_pos:
        score = momentum_scores.get(sym, 0)
        lines.append(f"   {sym}: {qty} shares | score: {score:+.1%}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram command handlers (async)
# ---------------------------------------------------------------------------

# These need a reference to the bot instance (set via set_bot_reference)
_bot_ref: Any = None


def set_bot_reference(bot_instance: Any) -> None:
    """Store a reference to the main Bot class for command handlers."""
    global _bot_ref
    _bot_ref = bot_instance



async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command."""
    if str(update.effective_chat.id) != config.TELEGRAM_CHAT_ID:
        return
    if _bot_ref is None:
        await update.message.reply_text("Bot not fully initialized yet.")
        return
    await update.message.reply_text(_bot_ref.get_status_text())


async def cmd_holdings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /holdings command."""
    if str(update.effective_chat.id) != config.TELEGRAM_CHAT_ID:
        return
    if _bot_ref is None:
        await update.message.reply_text("Bot not fully initialized yet.")
        return
    await update.message.reply_text(_bot_ref.get_holdings_text())


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /history command — last 6 rebalances."""
    if str(update.effective_chat.id) != config.TELEGRAM_CHAT_ID:
        return
    entries = ledger.get_entries(last_n=6)
    if not entries:
        await update.message.reply_text("No rebalance history yet.")
        return
    lines = [f"\U0001f4dc Last 6 Rebalances"]
    for e in reversed(entries):
        mode = e.get("mode", "?")
        d = e.get("date", "?")
        sold = e.get("stocks_sold", [])
        bought = e.get("stocks_bought", [])
        lines.append(f"\n{d} | {mode.upper()}")
        if sold:
            lines.append(f"   Sold: {', '.join(sold[:5])}")
        if bought:
            lines.append(f"   Bought: {', '.join(bought[:5])}")
    await update.message.reply_text("\n".join(lines))


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /balance command."""
    if str(update.effective_chat.id) != config.TELEGRAM_CHAT_ID:
        return
    if _bot_ref is None:
        await update.message.reply_text("Bot not fully initialized yet.")
        return
    await update.message.reply_text(_bot_ref.get_balance_text())


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /next command — preview next rebalance with fresh signal data."""
    if str(update.effective_chat.id) != config.TELEGRAM_CHAT_ID:
        return
    if _bot_ref is None:
        await update.message.reply_text("Bot not fully initialized yet.")
        return

    # Schedule a signal refresh on the scheduler thread (ib_insync thread affinity)
    # and wait for it to complete before returning the result.
    done = _bot_ref.schedule_signal_refresh()
    loop = asyncio.get_event_loop()
    refreshed = await loop.run_in_executor(None, done.wait, 30)
    if not refreshed:
        logger.warning("/next: signal refresh timed out after 30s, using cached data")

    await update.message.reply_text(_bot_ref.get_next_text())


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /report command — strategy summary."""
    if str(update.effective_chat.id) != config.TELEGRAM_CHAT_ID:
        return
    if _bot_ref is None:
        await update.message.reply_text("Bot not fully initialized yet.")
        return
    await update.message.reply_text(_bot_ref.get_report_text())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    if str(update.effective_chat.id) != config.TELEGRAM_CHAT_ID:
        return
    text = (
        f"\u2753 Available Commands\n"
        f"\n"
        f"/status   \u2014 Full portfolio snapshot\n"
        f"/holdings \u2014 Current positions with momentum scores\n"
        f"/history  \u2014 Last 6 monthly rebalances\n"
        f"/balance  \u2014 IBKR account cash & value in CHF\n"
        f"/next     \u2014 Preview next rebalance signal\n"
        f"/report   \u2014 Strategy summary & backtest stats\n"
        f"/help     \u2014 Show this message\n"
        f"\n"
        f"Strategy: Dual Momentum 6mo + 3% buffer\n"
        f"Universe: S&P 500 + SMI (117 stocks)\n"
        f"Rebalance: Monthly, first trading day at 10:00 UTC"
    )
    await update.message.reply_text(text)


# ---------------------------------------------------------------------------
# Telegram polling (runs in background thread)
# ---------------------------------------------------------------------------


def _build_application() -> Application:
    """Build and configure the Telegram Application with all command handlers."""
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("holdings", cmd_holdings))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("next", cmd_next))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("help", cmd_help))
    return app


def run_telegram_in_thread() -> None:
    """Entry point for the background Telegram polling thread.

    Creates its own event loop to avoid 'set_wakeup_fd' errors from
    run_polling() which tries to install signal handlers (main thread only).
    """
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        app = _build_application()

        async def _poll() -> None:
            await app.initialize()
            await app.updater.start_polling(drop_pending_updates=True)
            await app.start()
            logger.info("Telegram polling started")
            # Keep running until thread is killed (daemon thread)
            while True:
                await asyncio.sleep(3600)

        loop.run_until_complete(_poll())
    except Exception:
        logger.error("Telegram polling thread error", exc_info=True)
