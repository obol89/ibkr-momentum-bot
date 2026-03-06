#!/usr/bin/env python3
"""Pre-flight verification script for IBKR Momentum Bot.

Checks:
1. .env exists with all required keys
2. IBKR connection works
3. SPY/IEF historical data is fetchable
4. Current defensive signal
5. Sample universe stock data + momentum scores
6. Telegram test message
7. Next rebalance date
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
from decimal import Decimal

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

passed = 0
failed = 0


def check(name: str, fn) -> bool:
    """Run a check function, print result."""
    global passed, failed
    try:
        result = fn()
        if result:
            print(f"  [PASS] {name}")
            passed += 1
            return True
        else:
            print(f"  [FAIL] {name}")
            failed += 1
            return False
    except Exception as e:
        print(f"  [FAIL] {name}: {e}")
        failed += 1
        return False


def main() -> None:
    global passed, failed

    print("=" * 50)
    print("IBKR Momentum Bot - Pre-flight Verification")
    print("=" * 50)
    print()

    # ------------------------------------------------------------------
    # 1. Environment variables
    # ------------------------------------------------------------------
    print("1. Environment Configuration")
    import config as cfg

    env_path = cfg.BASE_DIR / ".env"
    check(".env file exists", lambda: env_path.exists())

    required_vars = cfg.REQUIRED_ENV_VARS
    for var in required_vars:
        val = os.getenv(var, "")
        check(
            f"  {var} is set",
            lambda v=val, vn=var: bool(v) and v not in ("your-telegram-bot-token-here", "your-telegram-chat-id-here"),
        )

    print(f"  Paper trading: {cfg.PAPER_TRADING}")
    print(f"  Reporting currency: {cfg.REPORTING_CURRENCY}")
    print()

    # ------------------------------------------------------------------
    # 2. IBKR Connection
    # ------------------------------------------------------------------
    print("2. IBKR Connection")
    from ibkr import IBKRClient

    client = IBKRClient()
    connected = check(
        f"Connect to {cfg.IBKR_HOST}:{cfg.IBKR_PORT}",
        lambda: (client.connect(), True)[1],
    )

    if connected:
        summary = client.get_account_summary()
        net_liq = summary.get("NetLiquidation", Decimal("0"))
        print(f"  Account NetLiquidation ({cfg.REPORTING_CURRENCY}): {net_liq:,.2f}")
        check("Account summary readable", lambda: net_liq > 0)

        cash = client.get_cash_balance()
        print(f"  USD Cash: {cash:,.2f}")

        fx = client.get_fx_rate("USD", "CHF")
        print(f"  USD/CHF rate: {fx:.4f}")
        check("FX rate available", lambda: fx > 0)
    else:
        print("  Skipping account checks (not connected)")
    print()

    # ------------------------------------------------------------------
    # 3. SPY + IEF Historical Data
    # ------------------------------------------------------------------
    print("3. Benchmark Data (SPY & IEF)")
    if connected:
        spy_prices = client.get_close_prices("SPY", lookback_days=180)
        check(f"SPY data: {len(spy_prices)} bars", lambda: len(spy_prices) >= 126)

        ief_prices = client.get_close_prices("IEF", lookback_days=180)
        check(f"IEF data: {len(ief_prices)} bars", lambda: len(ief_prices) >= 126)
    else:
        print("  Skipping (not connected)")
    print()

    # ------------------------------------------------------------------
    # 4. Defensive Signal
    # ------------------------------------------------------------------
    print("4. Current Defensive Signal")
    if connected and len(spy_prices) >= 126:
        from momentum import is_defensive

        defensive, spy_ret, ief_ret = is_defensive(spy_prices, ief_prices)
        print(f"  SPY 6mo return: {spy_ret:+.2%}")
        print(f"  IEF 6mo return: {ief_ret:+.2%}")
        print(f"  Buffer: {cfg.DEFENSIVE_BUFFER}")
        print(f"  Threshold: IEF - buffer = {ief_ret - float(cfg.DEFENSIVE_BUFFER):+.2%}")
        signal = "DEFENSIVE (go to cash)" if defensive else "MOMENTUM (invest)"
        print(f"  Signal: {signal}")
        check("Defensive signal computed", lambda: True)
    else:
        print("  Skipping (insufficient data)")
    print()

    # ------------------------------------------------------------------
    # 5. Sample Universe Stocks
    # ------------------------------------------------------------------
    print("5. Sample Universe Stocks")
    sample_stocks = ["AAPL", "MSFT", "NESN", "ROG", "NOVN"]
    if connected:
        from momentum import compute_momentum_score

        for sym in sample_stocks:
            try:
                prices = client.get_close_prices(sym, lookback_days=300)
                score = compute_momentum_score(
                    prices, lookback=cfg.LOOKBACK_DAYS, skip=cfg.SKIP_DAYS
                )
                score_str = f"{score:+.2%}" if score is not None else "N/A"
                check(
                    f"{sym}: {len(prices)} bars, score={score_str}",
                    lambda p=prices: len(p) > 0,
                )
            except Exception as e:
                check(f"{sym}: fetch failed", lambda: False)
    else:
        print("  Skipping (not connected)")
    print()

    # ------------------------------------------------------------------
    # 6. Telegram Test
    # ------------------------------------------------------------------
    print("6. Telegram Notification")
    try:
        from notifier import send_message

        summary_lines = [
            "IBKR Momentum Bot - Verification",
            f"IBKR: {'connected' if connected else 'FAILED'}",
        ]
        if connected:
            summary_lines.append(f"Account: {cfg.REPORTING_CURRENCY} {net_liq:,.0f}")
            if 'spy_ret' in dir():
                summary_lines.append(f"Signal: {'DEFENSIVE' if defensive else 'MOMENTUM'}")
        send_message("\n".join(summary_lines))
        check("Telegram test message sent", lambda: True)
    except Exception as e:
        check(f"Telegram: {e}", lambda: False)
    print()

    # ------------------------------------------------------------------
    # 7. Next Rebalance
    # ------------------------------------------------------------------
    print("7. Scheduling")
    from bot import get_next_rebalance_date

    next_date = get_next_rebalance_date()
    print(f"  Next rebalance: {next_date.isoformat()} at {cfg.REBALANCE_TIME} UTC")
    check("Next rebalance date computed", lambda: next_date is not None)
    print()

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    if connected:
        client.disconnect()

    print("=" * 50)
    total = passed + failed
    print(f"Results: {passed}/{total} passed, {failed}/{total} failed")
    if failed == 0:
        print("All checks passed!")
    else:
        print(f"{failed} check(s) failed. Review above.")
    print("=" * 50)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
