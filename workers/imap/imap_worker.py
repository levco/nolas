import asyncio
import logging

from app.controllers.imap.listener import IMAPListener
from workers.worker_config import WorkerConfig

logger = logging.getLogger(__name__)


class IMAPWorker:
    """Worker process that handles IMAP listening for a subset of accounts."""

    def __init__(self, config: WorkerConfig, imap_listener: IMAPListener):
        self._config = config
        self._worker_id = config.worker_id
        self._accounts = config.accounts
        self._imap_listener = imap_listener

        # State management
        self._active_tasks: list[asyncio.Task[None]] = []
        self._shutdown_event = asyncio.Event()
        self._worker_task: asyncio.Task[None] | None = None

        # Performance tracking
        self._stats = {
            "accounts_loaded": 0,
            "listeners_started": 0,
            "emails_processed": 0,
            "connection_errors": 0,
            "startup_time": 0.0,
        }

    async def run(self) -> None:
        """Main entry point for the worker process."""
        try:
            logger.info(f"Starting IMAP worker {self._worker_id} with {len(self._accounts)} accounts")

            # Start account listeners
            await self._start_account_listeners()

            # Mark startup complete
            self._stats["startup_time"] = asyncio.get_event_loop().time()
            logger.info(f"Worker {self._worker_id} startup complete")

            # Wait for shutdown signal
            await self._shutdown_event.wait()

        except Exception as e:
            logger.error(f"Fatal error in worker {self._worker_id}: {e}")
            raise
        finally:
            await self._cleanup()

    async def _start_account_listeners(self) -> None:
        """Start IMAP listeners for all assigned accounts."""
        logger.info(f"Worker {self._worker_id}: Starting listeners for {len(self._accounts)} accounts")

        for account in self._accounts:
            try:
                # Start account listeners
                tasks = await self._imap_listener.start_account_listener(account)
                self._active_tasks.extend(tasks)

                self._stats["accounts_loaded"] += 1
                self._stats["listeners_started"] += len(tasks)

                logger.info(f"Started {len(tasks)} listeners for {account.email}")

                # Small delay to prevent overwhelming the IMAP servers
                await asyncio.sleep(0.1)

            except Exception as e:
                logger.error(f"Failed to start listeners for {account.email}: {e}")
                self._stats["connection_errors"] += 1
                continue

        logger.info(f"Worker {self._worker_id}: Started {self._stats['listeners_started']} total listeners")

    async def _cleanup(self) -> None:
        """Clean up all resources."""
        logger.info(f"Worker {self._worker_id}: Starting cleanup")

        try:
            # Stop all IMAP listeners
            if self._imap_listener:
                await self._imap_listener.stop_all_listeners()

            # Cancel remaining tasks
            for task in self._active_tasks:
                if not task.done():
                    task.cancel()

            if self._active_tasks:
                await asyncio.gather(*self._active_tasks, return_exceptions=True)

            logger.info(f"Worker {self._worker_id}: Cleanup complete")

        except Exception as e:
            logger.error(f"Error during worker {self._worker_id} cleanup: {e}")

    async def shutdown(self) -> None:
        """Trigger shutdown of the worker."""
        logger.info(f"Worker {self._worker_id}: Shutdown requested")
        self._shutdown_event.set()

        # Wait for the worker task to complete
        if self._worker_task and not self._worker_task.done():
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass


async def start_worker(config: WorkerConfig, imap_listener: IMAPListener) -> IMAPWorker:
    """Start a worker process with the given configuration."""
    worker = IMAPWorker(config, imap_listener)

    # Start the worker in background
    worker_task = asyncio.create_task(worker.run())

    # Store the task for cleanup
    worker._worker_task = worker_task

    return worker


async def start_worker_blocking(config: WorkerConfig, imap_listener: IMAPListener) -> None:
    """Start a worker process with the given configuration (blocking version for cluster mode)."""
    worker = IMAPWorker(config, imap_listener)
    await worker.run()
