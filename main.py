"""
Main entry point for the Perpetual Funding Rate Arbitrage Bot.

Usage:
    python main.py                 # Run one cycle (for cron)
    python main.py --daemon        # Run continuously
    python main.py --dashboard     # Run with web dashboard
    python main.py --status        # Print current status
"""

import argparse
import asyncio
import signal
import sys
import threading
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent))

from config import settings
from utils.logger import get_logger
from utils.models import Platform
from connectors import (
    HyperliquidConnector,
    LighterConnector,
    OstiumConnector,
    AsterConnector,
    EdgeXConnector,
    DriftConnector,
)
from engine.arb_engine import ArbEngine
from monitoring.alerts import AlertManager

log = get_logger("main")


def create_connectors():
    """Instantiate all platform connectors."""
    connectors = [
        HyperliquidConnector(),
        LighterConnector(),
        OstiumConnector(),
        AsterConnector(),
        EdgeXConnector(),
    ]
    if DriftConnector is not None:
        connectors.append(DriftConnector())
    else:
        log.warning("Drift connector unavailable (Solana deps not installed)")
    return connectors


async def initialise_connectors(connectors):
    """Initialise all connectors (async setup). Returns only successfully initialised ones."""
    active = []
    for conn in connectors:
        try:
            await conn.initialise()
            log.info(f"Initialised: {conn.platform.value}")
            active.append(conn)
        except Exception as e:
            log.error(f"Failed to initialise {conn.platform.value}: {e}")
    if not active:
        log.error("No connectors initialised successfully — bot cannot operate")

    # Health check: verify each active connector can fetch balance
    for conn in active:
        try:
            bal = await conn.get_balance()
            log.info(
                f"[HEALTH] {conn.platform.value}: equity=${bal.equity_usd:.2f} "
                f"free_margin=${bal.free_margin_usd:.2f}"
            )
            if bal.equity_usd == 0 and bal.free_margin_usd == 0:
                log.warning(
                    f"[HEALTH] {conn.platform.value}: balance returned all zeros "
                    f"— check API keys / auth or account funding"
                )
        except Exception as e:
            log.error(f"[HEALTH] {conn.platform.value}: balance check FAILED: {e}")

    return active


async def shutdown_connectors(connectors):
    """Gracefully shut down all connectors."""
    for conn in connectors:
        try:
            await conn.shutdown()
        except Exception:
            pass


async def run_single_cycle(engine: ArbEngine, alerts: AlertManager):
    """Run a single scan-and-trade cycle."""
    summary = await engine.run_cycle()

    # Send alerts
    if summary.get("positions_opened", 0) > 0 or summary.get("positions_closed", 0) > 0:
        await alerts.alert_cycle_summary(summary)

    if summary.get("errors"):
        for err in summary["errors"]:
            await alerts.alert_error(err)

    return summary


async def run_daemon(engine: ArbEngine, alerts: AlertManager):
    """Run the bot continuously with the configured polling interval."""
    log.info(
        f"Starting daemon mode (poll every {settings.scheduler.funding_rate_poll_interval}s)"
    )

    stop_event = asyncio.Event()

    def _handle_signal():
        log.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    while not stop_event.is_set():
        try:
            await run_single_cycle(engine, alerts)
        except Exception as e:
            log.error(f"Cycle error: {e}", exc_info=True)
            await alerts.alert_error(str(e))

        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=settings.scheduler.funding_rate_poll_interval,
            )
        except asyncio.TimeoutError:
            pass  # normal — timeout means it's time for the next cycle

    log.info("Daemon stopped")


async def async_main(args):
    # Create and initialise (only keep connectors that init successfully)
    connectors = create_connectors()
    connectors = await initialise_connectors(connectors)

    engine = ArbEngine(connectors)
    alerts = AlertManager()
    await alerts.start()

    # Optionally start dashboard
    if args.dashboard:
        from dashboard.app import set_engine, run_dashboard
        set_engine(engine)
        dash_thread = threading.Thread(target=run_dashboard, daemon=True)
        dash_thread.start()
        log.info(f"Dashboard started at http://{settings.dashboard.host}:{settings.dashboard.port}")

    try:
        if args.status:
            # Just print status
            await engine.run_cycle()
            import json
            print(json.dumps(engine.get_status(), indent=2))

        elif args.daemon:
            await run_daemon(engine, alerts)

        else:
            # Single cycle (for cron)
            summary = await run_single_cycle(engine, alerts)
            log.info(f"Cycle complete: {summary}")

    finally:
        await alerts.stop()
        await shutdown_connectors(connectors)


def main():
    parser = argparse.ArgumentParser(description="Perp Funding Rate Arbitrage Bot")
    parser.add_argument("--daemon", action="store_true", help="Run continuously")
    parser.add_argument("--dashboard", action="store_true", help="Enable web dashboard")
    parser.add_argument("--status", action="store_true", help="Print status and exit")
    args = parser.parse_args()

    if settings.dry_run:
        log.warning("*** DRY RUN MODE — no real trades will be executed ***")

    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
