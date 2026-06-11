import asyncio
import logging
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi_async_sqlalchemy import db

from app.controllers.notifications.incoming_controller import IncomingNotificationController

logger = logging.getLogger(__name__)

GOOGLE = "google"
MICROSOFT = "microsoft"


# Re-enqueue a failed job at most this many times before dropping it.
MAX_JOB_ATTEMPTS = 3


@dataclass
class NotificationJob:
    kind: str  # GOOGLE | MICROSOFT
    payload: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0


class NotificationQueue:
    """Bounded in-process queue decoupling provider pushes from processing.

    Push endpoints ack immediately after enqueueing; worker tasks do the actual
    history sync / message fetch / webhook delivery. When the queue is full the
    endpoints return 503 so the provider retries (backpressure instead of OOM).

    Crash semantics: Google is self-healing — the per-account history cursor means
    any notification lost between ack and processing is recovered on the next
    notification's history.list. Microsoft notifications identify a single message;
    a crash between ack and processing loses that delivery (bounded risk, the
    message is still fetchable and any later notification for the mailbox is
    unaffected).
    """

    def __init__(
        self,
        incoming_controller: IncomingNotificationController,
        workers: int = 8,
        maxsize: int = 1000,
        session_context: Callable[[], AbstractAsyncContextManager[Any]] | None = None,
    ) -> None:
        self._incoming_controller = incoming_controller
        self._workers = workers
        self._queue: asyncio.Queue[NotificationJob] = asyncio.Queue(maxsize=maxsize)
        self._tasks: list[asyncio.Task[None]] = []
        # Each job needs its own DB session; db(commit_on_exit=True) mirrors request scope.
        self._session_context = session_context or (lambda: db(commit_on_exit=True))

    def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [asyncio.create_task(self._worker(i)) for i in range(self._workers)]
        logger.info(f"Notification queue started with {self._workers} workers (maxsize={self._queue.maxsize})")

    async def stop(self) -> None:
        # Drain what we can, then cancel workers.
        try:
            await asyncio.wait_for(self._queue.join(), timeout=10)
        except asyncio.TimeoutError:
            logger.warning(f"Notification queue stopped with ~{self._queue.qsize()} jobs unprocessed")
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    def try_enqueue(self, job: NotificationJob) -> bool:
        """Returns False when the queue is full (caller should signal retry to the provider)."""
        try:
            self._queue.put_nowait(job)
            return True
        except asyncio.QueueFull:
            logger.warning(f"Notification queue full; rejecting {job.kind} notification")
            return False

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    async def _worker(self, worker_id: int) -> None:
        while True:
            job = await self._queue.get()
            try:
                async with self._session_context():
                    await self._process(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                job.attempts += 1
                if job.attempts < MAX_JOB_ATTEMPTS:
                    logger.warning(
                        f"[worker-{worker_id}] {job.kind} notification failed "
                        f"(attempt {job.attempts}/{MAX_JOB_ATTEMPTS}); re-enqueueing"
                    )
                    if not self.try_enqueue(job):
                        logger.error(
                            f"[worker-{worker_id}] queue full; dropping {job.kind} notification "
                            f"after {job.attempts} attempt(s)"
                        )
                else:
                    logger.exception(
                        f"[worker-{worker_id}] dropping {job.kind} notification after {job.attempts} attempts"
                    )
            finally:
                self._queue.task_done()

    async def _process(self, job: NotificationJob) -> None:
        if job.kind == GOOGLE:
            await self._incoming_controller.process_google_notification(
                job.payload["email_address"], job.payload["history_id"]
            )
        elif job.kind == MICROSOFT:
            await self._incoming_controller.process_microsoft_notification(job.payload["notification"])
        else:
            logger.error(f"Unknown notification job kind: {job.kind}")
