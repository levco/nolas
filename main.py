#!/usr/bin/env python3
"""
Scalable IMAP Email Tracker

A high-performance, async-based IMAP email tracker that can scale to 1000+ email accounts
using connection pooling, rate limiting, and distributed worker processes.

Usage:
    python main.py [--mode MODE] [--migrate] [--workers N]

Modes:
    - cluster: Start cluster manager (default for production)
    - single: Start single worker process (for development/testing)
    - migrate: Migrate accounts from old config to database

Environment Variables:
    DATABASE_URL: PostgreSQL connection string
    NUM_WORKERS: Number of worker processes
    WEBHOOK_TIMEOUT: Webhook timeout in seconds
"""

import argparse
import asyncio
import logging
import signal
import sys

from dotenv import load_dotenv

load_dotenv(override=True)
from database import DatabaseManager  # noqa: E402
from logging_config import setup_logging  # noqa: E402
from models import WorkerConfig  # noqa: E402
from workers.cluster_manager import IMAPClusterManager  # noqa: E402
from workers.imap.imap_worker import start_worker  # noqa: E402

setup_logging()

logger = logging.getLogger(__name__)


async def run_cluster_mode(num_workers: int | None = None) -> None:
    """Run in cluster mode with multiple worker processes."""
    cluster_manager = IMAPClusterManager(num_workers)

    # Setup signal handlers for graceful shutdown
    shutdown_event = asyncio.Event()

    def signal_handler() -> None:
        logger.info("Received shutdown signal")
        shutdown_event.set()

    # Register signal handlers
    for sig in [signal.SIGINT, signal.SIGTERM]:
        signal.signal(sig, lambda s, f: signal_handler())

    try:
        # Start cluster in background
        cluster_task = asyncio.create_task(cluster_manager.start_cluster())

        # Wait for shutdown signal
        await shutdown_event.wait()

        # Graceful shutdown
        logger.info("Initiating graceful shutdown...")
        await cluster_manager.shutdown()

        # Cancel cluster task
        cluster_task.cancel()
        try:
            await cluster_task
        except asyncio.CancelledError:
            pass

    except Exception as e:
        logger.error(f"Cluster mode failed: {e}")
        raise


async def run_single_worker_mode() -> None:
    """Run in single worker mode (for development/testing)."""
    logger.info("Starting in single worker mode")

    # Load accounts from database
    db_manager = DatabaseManager()
    await db_manager.init_pool()

    try:
        accounts = await db_manager.get_active_accounts()
        if not accounts:
            logger.warning("No active accounts found. Run with --migrate first.")
            return

        # Create single worker config
        config = WorkerConfig(worker_id=0, accounts=accounts)

        # Run worker
        await start_worker(config)

    finally:
        await db_manager.close_pool()


async def add_test_account() -> None:
    """Add a test account to the database."""
    db_manager = DatabaseManager()
    await db_manager.init_pool()

    try:
        try:
            from test_accounts import test_accounts
        except ImportError:
            logger.error("test_accounts.py not found")
            return

        for account in test_accounts:
            await db_manager.add_account(account)
            logger.info(f"Added test account: {account.email}")

    finally:
        await db_manager.close_pool()


async def list_accounts() -> None:
    """List all accounts in the database."""
    db_manager = DatabaseManager()
    await db_manager.init_pool()

    try:
        accounts = await db_manager.get_active_accounts()

        if not accounts:
            print("No accounts found in database.")
            return

        print(f"\nFound {len(accounts)} accounts:")
        print("-" * 80)
        for i, account in enumerate(accounts, 1):
            print(f"{i:2d}. {account.email:30} {account.provider:15} {account.webhook_url}")
        print("-" * 80)

    finally:
        await db_manager.close_pool()


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Scalable IMAP Email Tracker")
    parser.add_argument(
        "--mode", choices=["cluster", "single", "migrate", "add-test", "list"], default="cluster", help="Operating mode"
    )
    parser.add_argument("--workers", type=int, help="Number of worker processes")
    parser.add_argument("--migrate", action="store_true", help="Migrate accounts to database")

    args = parser.parse_args()

    # Handle migration flag for backward compatibility
    if args.migrate:
        args.mode = "migrate"

    try:
        if args.mode == "cluster":
            asyncio.run(run_cluster_mode(args.workers))
        elif args.mode == "single":
            asyncio.run(run_single_worker_mode())
        elif args.mode == "add-test":
            asyncio.run(add_test_account())
        elif args.mode == "list":
            asyncio.run(list_accounts())

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Application failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
