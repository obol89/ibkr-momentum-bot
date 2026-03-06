"""Entrypoint for the IBKR Dual Momentum Bot.

Handles PID locking, signal handling, logging setup, and bot lifecycle.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import config
from bot import MomentumBot

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PID lock
# ---------------------------------------------------------------------------


def _read_pid() -> int | None:
    """Read PID from lock file, or None if not present."""
    if not config.PID_FILE.exists():
        return None
    try:
        return int(config.PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None


def _is_process_running(pid: int) -> bool:
    """Check if a process with the given PID is running."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _is_bot_process(pid: int) -> bool:
    """Check if the given PID is an instance of this bot (not an unrelated process).

    Verifies via /proc that the process cmdline or working directory
    references ibkr-momentum-bot specifically.
    """
    try:
        proc = Path(f"/proc/{pid}")
        # Check cmdline for our project path
        cmdline_path = proc / "cmdline"
        if cmdline_path.exists():
            cmdline = cmdline_path.read_text()
            if "ibkr-momentum-bot" in cmdline:
                return True
        # Check working directory
        cwd_path = proc / "cwd"
        if cwd_path.is_symlink():
            cwd = str(cwd_path.resolve())
            if "ibkr-momentum-bot" in cwd:
                return True
    except (OSError, PermissionError):
        pass
    return False


def acquire_pid_lock() -> None:
    """Acquire PID lock, killing any stale bot instance first."""
    existing_pid = _read_pid()
    if existing_pid is not None:
        if _is_process_running(existing_pid):
            if _is_bot_process(existing_pid):
                logger.warning(
                    "Killing existing bot instance (PID %d)", existing_pid
                )
                try:
                    os.kill(existing_pid, signal.SIGTERM)
                    # Wait up to 5 seconds for graceful shutdown
                    for _ in range(50):
                        if not _is_process_running(existing_pid):
                            break
                        time.sleep(0.1)
                    # Force kill if still running
                    if _is_process_running(existing_pid):
                        logger.warning("Force-killing PID %d", existing_pid)
                        os.kill(existing_pid, signal.SIGKILL)
                        time.sleep(0.5)
                except OSError:
                    pass
            else:
                logger.warning(
                    "PID %d is running but is not a bot process, overwriting PID file",
                    existing_pid,
                )
        else:
            logger.warning("Stale PID file found (PID %d), removing", existing_pid)

        config.PID_FILE.unlink(missing_ok=True)

    config.PID_FILE.write_text(str(os.getpid()))
    logger.info("PID lock acquired: %d", os.getpid())


def release_pid_lock() -> None:
    """Release PID lock file."""
    config.PID_FILE.unlink(missing_ok=True)
    logger.info("PID lock released")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging() -> None:
    """Configure logging to stdout (for journalctl) and rotating file."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Stdout handler
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(fmt)
    root.addHandler(stdout_handler)

    # Rotating file handler
    log_file = config.LOG_DIR / "bot.log"
    file_handler = RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=3
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # Quiet noisy libraries
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("ib_insync").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="IBKR Dual Momentum Bot")
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="Execute an immediate rebalance on startup",
    )
    args = parser.parse_args()

    setup_logging()
    logger.info(
        "IBKR Momentum Bot starting (paper=%s, pid=%d)",
        config.PAPER_TRADING,
        os.getpid(),
    )

    acquire_pid_lock()
    bot = MomentumBot()

    def shutdown(signum: int, frame: object) -> None:
        logger.info("Received signal %d, shutting down...", signum)
        bot.stop()
        release_pid_lock()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        bot.start(run_now=args.run_now)

        # Keep main thread alive
        logger.info("Bot is running. Press Ctrl+C to stop.")
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received")
    except Exception:
        logger.critical("Unhandled exception", exc_info=True)
        try:
            from notifier import send_error
            send_error("Bot crashed with unhandled exception! Check logs.")
        except Exception:
            pass
    finally:
        bot.stop()
        release_pid_lock()


if __name__ == "__main__":
    main()
