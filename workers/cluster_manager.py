import asyncio
import logging
import multiprocessing as mp

from app.database import db_manager, get_db_session
from app.models import Account
from app.repos.account import AccountRepo
from models import AccountConfig, WorkerConfig
from settings import settings
from workers.imap.imap_worker import start_worker

logger = logging.getLogger(__name__)


def convert_account_model_to_config(account: Account) -> AccountConfig:
    """Convert SQLAlchemy Account model to AccountConfig dataclass."""
    return AccountConfig(
        email=account.email,
        username=account.username,
        password=account.password_encrypted,  # TODO: Implement decryption
        provider=account.provider,
        webhook_url=account.webhook_url,
    )


class IMAPClusterManager:
    """Manages multiple IMAP worker processes for horizontal scaling."""

    def __init__(self, num_workers: int | None = None):
        self.num_workers = num_workers or settings.worker.num_workers
        self._worker_processes: list[mp.Process] = []
        self._shutdown_event = mp.Event()

    async def start_cluster(self) -> None:
        """Start the IMAP cluster with distributed accounts."""
        try:
            logger.info(f"Starting IMAP cluster with {self.num_workers} workers")

            # Initialize database
            db_manager.init_db()

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

    async def _load_accounts(self) -> list[AccountConfig]:
        """Load accounts from database using repository."""
        try:
            async for session in get_db_session():
                account_repo = AccountRepo(session)
                accounts_models = await account_repo.get_all_active()

                # Convert to AccountConfig
                accounts = [convert_account_model_to_config(acc) for acc in accounts_models]
                return accounts
        except Exception as e:
            logger.error(f"Failed to load accounts: {e}")
        return []

    def _distribute_accounts(self, accounts: list[AccountConfig]) -> list[WorkerConfig]:
        """Distribute accounts evenly across workers."""
        if not accounts:
            return []

        chunk_size = max(1, len(accounts) // self.num_workers)
        worker_configs = []

        for i in range(self.num_workers):
            start_idx = i * chunk_size

            # Last worker gets remaining accounts
            if i == self.num_workers - 1:
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
            asyncio.run(start_worker(config))

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

    async def add_account(self, account: AccountConfig) -> bool:
        """Add a new account to the cluster (dynamic scaling)."""
        try:
            # Add to database
            async for session in get_db_session():
                account_repo = AccountRepo(session)
                account_data = {
                    "email": account.email,
                    "username": account.username,
                    "password_encrypted": account.password,
                    "provider": account.provider,
                    "webhook_url": account.webhook_url,
                    "is_active": True,
                }
                await account_repo.create(account_data)
                await session.commit()
                break

            # For now, just restart the cluster
            # In production, you'd want more sophisticated load balancing
            logger.info(f"Added account {account.email} to database")
            return True

        except Exception as e:
            logger.error(f"Failed to add account {account.email}: {e}")
            return False

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
            # Close database connections
            await db_manager.close()

            logger.info("Cluster cleanup complete")

        except Exception as e:
            logger.error(f"Error during cluster cleanup: {e}")
