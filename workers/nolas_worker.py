"""
Durable job processor worker.

Consumes queued rows from the jobs table and dispatches by job type
(Google notification, Microsoft notification, subscription renewal, ...).

Run with: python workers/job_processor.py
"""

import asyncio
import logging
import os
import signal
import sys
from uuid import uuid4

from dotenv import load_dotenv
from fastapi_async_sqlalchemy import db

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../")))
load_dotenv("./.env", override=True)

import sentry_sdk

from app.container import get_wire_container
from app.db import fastapi_sqlalchemy_context
from logging_config import setup_logging
from settings import settings

if settings.sentry.is_enabled:
    sentry_sdk.init(dsn=settings.sentry.dsn, environment=settings.environment.value)

logger = logging.getLogger(__name__)
setup_logging()
container = get_wire_container()


async def _run_worker_loop(worker_id: str, shutdown_event: asyncio.Event) -> None:
    try:
        job_processor = container.controllers.job_processor()
    except Exception:
        logger.exception(f"{worker_id}: failed to initialize job processor loop")
        shutdown_event.set()
        return

    while not shutdown_event.is_set():
        try:
            async with db(commit_on_exit=True):
                processed = await job_processor.process_available_jobs(
                    worker_id=worker_id,
                    batch_size=settings.job_processor.batch_size,
                    lock_timeout_seconds=settings.job_processor.lock_timeout_seconds,
                )
        except Exception:
            logger.exception(f"{worker_id}: job processor iteration failed")
            processed = 0
            await asyncio.sleep(2)

        if processed == 0:
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=settings.job_processor.idle_sleep_seconds)
            except asyncio.TimeoutError:
                continue


def _handle_worker_task_done(task: asyncio.Task[None], worker_id: str, shutdown_event: asyncio.Event) -> None:
    """Fail fast if any worker loop exits unexpectedly."""
    if shutdown_event.is_set():
        return

    if task.cancelled():
        logger.error(f"{worker_id}: worker loop cancelled unexpectedly")
        shutdown_event.set()
        return

    error = task.exception()
    if error is not None:
        logger.error(
            f"{worker_id}: worker loop crashed",
            exc_info=(type(error), error, error.__traceback__),
        )
        shutdown_event.set()
        return

    logger.error(f"{worker_id}: worker loop exited unexpectedly")
    shutdown_event.set()


async def main() -> None:
    shutdown_event = asyncio.Event()
    process_id = uuid4().hex[:8]

    def signal_handler() -> None:
        logger.info("Received shutdown signal")
        shutdown_event.set()

    for sig in [signal.SIGINT, signal.SIGTERM]:
        signal.signal(sig, lambda s, f: signal_handler())

    token_service = container.controllers.token_service()
    concurrency = settings.job_processor.concurrency

    async with fastapi_sqlalchemy_context():
        logger.info(f"Starting {concurrency} async job loop(s)")
        worker_tasks: list[asyncio.Task[None]] = []
        for idx in range(concurrency):
            worker_id = f"job-processor-{process_id}-{idx}"
            task = asyncio.create_task(
                _run_worker_loop(worker_id=worker_id, shutdown_event=shutdown_event),
                name=f"job-processor-{idx}",
            )
            task.add_done_callback(
                lambda completed_task, wid=worker_id: _handle_worker_task_done(
                    completed_task,
                    wid,
                    shutdown_event,
                )
            )
            worker_tasks.append(task)

        await shutdown_event.wait()
        for task in worker_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*worker_tasks, return_exceptions=True)

    await token_service.close()


if __name__ == "__main__":
    logger.info("Starting durable job processor worker")
    asyncio.run(main())
