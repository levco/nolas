import asyncio
import logging
import multiprocessing as mp
from typing import Sequence

from app.controllers.imap.email_processor import EmailProcessor
from app.controllers.imap.listener import IMAPListener
from app.models.account import Account
from app.repos.account import AccountRepo
from models import WorkerConfig
from settings import settings
from workers.imap.imap_worker import start_worker

logger = logging.getLogger(__name__)


class IMAPClusterManager:
    """Manages multiple IMAP worker processes for horizontal scaling."""

    def __init__(
        self,
        account_repo: AccountRepo,
        email_processor: EmailProcessor,
        imap_listener: IMAPListener,
        num_workers: int | None = None,
    ):
        self._worker_processes: list[mp.Process] = []
        self._shutdown_event = mp.Event()

        self._num_workers = num_workers or settings.worker.num_workers

        self._account_repo = account_repo
        self._email_processor = email_processor
        self._imap_listener = imap_listener

    async def start_cluster(self) -> None:
        """Start the IMAP cluster with distributed accounts."""
        try:
            logger.info(f"Starting IMAP cluster with {self._num_workers} workers")

            # Load accounts from database
            accounts = await self._load_accounts()
            logger.info(f"Loaded {len(accounts)} active accounts from database")

            if not accounts:
                logger.warning("No active accounts found in database")
                return

            # Distribute accounts across workers
            worker_configs = self._distribute_accounts(accounts)

            # Start worker processes
            self._start_workers(worker_configs)

            logger.info(f"IMAP cluster started with {len(self._worker_processes)} workers")

            # Monitor workers
            await self._monitor_workers()

        except Exception as e:
            logger.error(f"Failed to start IMAP cluster: {e}")
            raise
        finally:
            await self._cleanup()

    async def _load_accounts(self) -> Sequence[Account]:
        """Load accounts from database using repository."""
        try:
            accounts_result = await self._account_repo.get_all_active()
            return accounts_result.all()
        except Exception:
            logger.exception("Failed to load accounts")
        return []

    def _distribute_accounts(self, accounts: Sequence[Account]) -> list[WorkerConfig]:
        """Distribute accounts evenly across workers."""
        if not accounts:
            return []

        chunk_size = max(1, len(accounts) // self._num_workers)
        worker_configs = []

        for i in range(self._num_workers):
            start_idx = i * chunk_size

            # Last worker gets remaining accounts
            if i == self._num_workers - 1:
                end_idx = len(accounts)
            else:
                end_idx = start_idx + chunk_size

            worker_accounts = accounts[start_idx:end_idx]

            if worker_accounts:  # Only create workers with accounts
                config = WorkerConfig(
                    worker_id=i,
                    accounts=worker_accounts,
                    max_connections_per_provider=settings.worker.max_connections_per_provider,
                )
                worker_configs.append(config)

                logger.info(f"Worker {i}: assigned {len(worker_accounts)} accounts")

        return worker_configs

    def _start_workers(self, worker_configs: list[WorkerConfig]) -> None:
        """Start worker processes."""
        for config in worker_configs:
            process = mp.Process(
                target=self._run_worker_process, args=(config,), name=f"imap-worker-{config.worker_id}"
            )
            process.start()
            self._worker_processes.append(process)

            logger.info(f"Started worker process {config.worker_id} (PID: {process.pid})")

    def _run_worker_process(self, config: WorkerConfig) -> None:
        """Entry point for worker process."""
        try:
            # Setup logging for worker
            logging.basicConfig(
                level=logging.INFO, format=f"%(asctime)s - Worker-{config.worker_id} - %(levelname)s - %(message)s"
            )

            # Run the async worker
            asyncio.run(start_worker(config, self._imap_listener))

        except KeyboardInterrupt:
            logger.info(f"Worker {config.worker_id} interrupted")
        except Exception as e:
            logger.error(f"Worker {config.worker_id} failed: {e}")
            raise

    async def _monitor_workers(self) -> None:
        """Monitor worker processes and handle failures."""
        logger.info("Starting worker monitoring")

        while not self._shutdown_event.is_set():
            try:
                # Check worker health
                alive_workers = [p for p in self._worker_processes if p.is_alive()]
                dead_workers = [p for p in self._worker_processes if not p.is_alive()]

                if dead_workers:
                    logger.warning(f"Found {len(dead_workers)} dead workers")
                    for worker in dead_workers:
                        logger.error(f"Worker {worker.name} died (exit code: {worker.exitcode})")
                        self._worker_processes.remove(worker)

                logger.info(f"Cluster health: {len(alive_workers)} workers alive")

                # Sleep before next check
                await asyncio.sleep(30)

            except Exception as e:
                logger.error(f"Error in worker monitoring: {e}")
                await asyncio.sleep(10)

    async def get_cluster_stats(self) -> dict[str, int | list[int]]:
        """Get cluster-wide statistics."""
        alive_workers = [p for p in self._worker_processes if p.is_alive()]

        stats: dict[str, int | list[int]] = {
            "total_workers": len(self._worker_processes),
            "alive_workers": len(alive_workers),
            "dead_workers": len(self._worker_processes) - len(alive_workers),
            "worker_pids": [p.pid for p in alive_workers if p.pid is not None],
        }

        return stats

    async def shutdown(self) -> None:
        """Gracefully shutdown the cluster."""
        logger.info("Shutting down IMAP cluster")

        self._shutdown_event.set()

        # Terminate worker processes
        for process in self._worker_processes:
            if process.is_alive():
                logger.info(f"Terminating worker {process.name}")
                process.terminate()

        # Wait for processes to terminate
        for process in self._worker_processes:
            process.join(timeout=30)
            if process.is_alive():
                logger.warning(f"Force killing worker {process.name}")
                process.kill()

        self._worker_processes.clear()
        logger.info("IMAP cluster shutdown complete")

    async def _cleanup(self) -> None:
        """Clean up cluster resources."""
        try:
            logger.info("Cluster cleanup complete")

        except Exception as e:
            logger.error(f"Error during cluster cleanup: {e}")
