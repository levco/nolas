import asyncio
import logging
import os
import signal
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../")))
load_dotenv("./.env", override=True)

from app.container import get_wire_container
from app.db import fastapi_sqlalchemy_context
from logging_config import setup_logging
from models import WorkerConfig
from settings import settings
from workers.cluster_manager import IMAPClusterManager
from workers.imap.imap_worker import start_worker

setup_logging()
logger = logging.getLogger(__name__)


container = get_wire_container()


def main() -> None:
    logger.info(f"Starting IMAP tracker in {settings.imap.listener_mode} mode")

    mode = settings.imap.listener_mode
    if mode == "single":
        asyncio.run(run_single_worker_mode())
    elif mode == "cluster":
        asyncio.run(run_cluster_mode())
    else:
        raise ValueError(f"Invalid listener mode: {mode}")


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

            # Setup signal handlers for graceful shutdown
            shutdown_event = asyncio.Event()

            def signal_handler() -> None:
                logger.info("Received shutdown signal")
                shutdown_event.set()

            # Register signal handlers
            for sig in [signal.SIGINT, signal.SIGTERM]:
                signal.signal(sig, lambda s, f: signal_handler())

            # Start worker
            worker = await start_worker(config, imap_listener)

            # Wait for shutdown signal
            await shutdown_event.wait()

            # Graceful shutdown
            logger.info("Initiating graceful shutdown...")

            # Shutdown the worker (this will trigger cleanup)
            await worker.shutdown()

        except Exception:
            logger.exception("Error in single worker mode")


async def run_cluster_mode(num_workers: int | None = None) -> None:
    """Run in cluster mode with multiple worker processes."""
    async with fastapi_sqlalchemy_context():
        cluster_manager = IMAPClusterManager(
            account_repo=container.repos.account(),
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


if __name__ == "__main__":
    main()
