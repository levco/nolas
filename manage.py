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
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from dotenv import load_dotenv

load_dotenv(override=True)
from app.container import ApplicationContainer  # noqa: E402
from logging_config import setup_logging  # noqa: E402
from models import WorkerConfig  # noqa: E402
from settings import settings  # noqa: E402
from workers.cluster_manager import IMAPClusterManager  # noqa: E402
from workers.imap.imap_worker import start_worker  # noqa: E402

setup_logging()

logger = logging.getLogger(__name__)

# Global container instance
container = ApplicationContainer()


@asynccontextmanager
async def fastapi_sqlalchemy_context() -> AsyncGenerator[None, None]:
    """Initialize fastapi_async_sqlalchemy for standalone scripts."""
    from fastapi_async_sqlalchemy import SQLAlchemyMiddleware, db
    from starlette.applications import Starlette

    base_url = settings.database.host.replace("postgresql://", "postgresql+asyncpg://")
    database_url = f"{base_url}/{settings.database.name}"

    # Create a minimal Starlette app to initialize the middleware
    app = Starlette()
    SQLAlchemyMiddleware(
        app,
        db_url=database_url,
        engine_args={
            "echo": False,
            "future": True,
            "pool_size": settings.database.min_pool_size,
            "max_overflow": settings.database.max_pool_size - settings.database.min_pool_size,
        },
    )

    async with db():
        yield


async def run_cluster_mode(num_workers: int | None = None) -> None:
    """Run in cluster mode with multiple worker processes."""
    async with fastapi_sqlalchemy_context():
        cluster_manager = IMAPClusterManager(
            account_repo=container.repos.account(),
            email_processor=container.controllers.imap_email_processor(),
            imap_listener=container.controllers.imap_listener(),
            num_workers=num_workers,
        )

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

    async with fastapi_sqlalchemy_context():
        try:
            imap_listener = container.controllers.imap_listener()

            # Get account repo from container
            account_repo = container.repos.account()

            # Get all active accounts
            active_accounts = (await account_repo.get_all_active()).all()

            if len(active_accounts) == 0:
                logger.warning("No active accounts found. Run with --migrate first.")
                return

            # Get all accounts
            logger.info(f"Found {len(active_accounts)} active accounts")

            # Create single worker config
            config = WorkerConfig(worker_id=0, accounts=active_accounts)

            # Run worker
            await start_worker(config, imap_listener)

        except Exception:
            logger.exception("Error in single worker mode")


async def list_accounts() -> None:
    """List all accounts in the database."""
    async with fastapi_sqlalchemy_context():
        # Get account repo from container
        account_repo = container.repos.account()
        accounts = (await account_repo.get_all_active()).all()
        account_count = len(accounts)

        if account_count == 0:
            print("No accounts found in database.")
            return

        logger.info(f"\nFound {account_count} accounts:")
        logger.info("-" * 80)
        for i, account in enumerate(accounts, 1):
            logger.info(f"{i:2d}. {account.email:30} {account.provider:15} {account.app_id}")
        logger.info("-" * 80)


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Scalable IMAP Email Tracker")
    parser.add_argument(
        "--mode", choices=["cluster", "single", "migrate", "add-test", "list"], default="cluster", help="Operating mode"
    )
    parser.add_argument("--workers", type=int, help="Number of worker processes")
    parser.add_argument("--migrate", action="store_true", help="Migrate accounts to database")

    args = parser.parse_args()

    try:
        if args.mode == "cluster":
            asyncio.run(run_cluster_mode(args.workers))
        elif args.mode == "single":
            asyncio.run(run_single_worker_mode())
        elif args.mode == "list":
            asyncio.run(list_accounts())

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception:
        logger.exception("Application failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
