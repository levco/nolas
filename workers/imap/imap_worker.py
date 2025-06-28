import asyncio
import logging

from app.database import db_manager
from lib.email_processor import EmailProcessor
from lib.imap.listener import IMAPListener
from models import WorkerConfig

logger = logging.getLogger(__name__)


class IMAPWorker:
    """Worker process that handles IMAP listening for a subset of accounts."""

    _email_processor: EmailProcessor
    _imap_listener: IMAPListener

    def __init__(self, config: WorkerConfig):
        self.config = config
        self.worker_id = config.worker_id
        self.accounts = config.accounts

        # State management
        self._active_tasks: list[asyncio.Task[None]] = []
        self._shutdown_event = asyncio.Event()

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
            logger.info(f"Starting IMAP worker {self.worker_id} with {len(self.accounts)} accounts")

            # Initialize components
            await self._initialize_components()

            # Start account listeners
            await self._start_account_listeners()

            # Mark startup complete
            self._stats["startup_time"] = asyncio.get_event_loop().time()
            logger.info(f"Worker {self.worker_id} startup complete")

            # Wait for shutdown signal
            await self._shutdown_event.wait()

        except Exception as e:
            logger.error(f"Fatal error in worker {self.worker_id}: {e}")
            raise
        finally:
            await self._cleanup()

    async def _initialize_components(self) -> None:
        """Initialize all worker components."""
        logger.info(f"Worker {self.worker_id}: Initializing components")

        # Initialize database manager
        db_manager.init_db()

        # Initialize email processor
        self._email_processor = EmailProcessor()
        await self._email_processor.init_session()

        # Initialize IMAP listener
        self._imap_listener = IMAPListener(self._email_processor)

        logger.info(f"Worker {self.worker_id}: Components initialized")

    async def _start_account_listeners(self) -> None:
        """Start IMAP listeners for all assigned accounts."""
        logger.info(f"Worker {self.worker_id}: Starting listeners for {len(self.accounts)} accounts")

        for account in self.accounts:
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

        logger.info(f"Worker {self.worker_id}: Started {self._stats['listeners_started']} total listeners")

    async def get_worker_stats(self) -> dict[str, int | str]:
        """Get comprehensive worker statistics."""
        listener_stats = await self._imap_listener.get_listener_stats()

        return {
            "worker_id": self.worker_id,
            "accounts_assigned": len(self.accounts),
            "accounts_loaded": int(self._stats["accounts_loaded"]),
            "listeners_started": int(self._stats["listeners_started"]),
            "active_listeners": listener_stats["active_listeners"],
            "failed_listeners": listener_stats["failed_listeners"],
            "active_tasks": len([t for t in self._active_tasks if not t.done()]),
            "emails_processed": int(self._stats["emails_processed"]),
            "connection_errors": int(self._stats["connection_errors"]),
            "startup_time": int(self._stats["startup_time"]),
        }

    async def _cleanup(self) -> None:
        """Clean up all resources."""
        logger.info(f"Worker {self.worker_id}: Starting cleanup")

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

            # Close email processor session
            if self._email_processor:
                await self._email_processor.close_session()

            # Close database connections
            await db_manager.close()

            logger.info(f"Worker {self.worker_id}: Cleanup complete")

        except Exception as e:
            logger.error(f"Error during worker {self.worker_id} cleanup: {e}")


async def start_worker(config: WorkerConfig) -> None:
    """Start a worker process with the given configuration."""
    worker = IMAPWorker(config)
    await worker.run()
