import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from unittest.mock import AsyncMock

import pytest

from app.controllers.notifications.queue import GOOGLE, MICROSOFT, NotificationJob, NotificationQueue


@asynccontextmanager
async def _noop_session() -> AsyncGenerator[None, None]:
    yield


def _make_queue(workers: int = 2, maxsize: int = 4) -> tuple[NotificationQueue, AsyncMock]:
    controller = AsyncMock()
    queue = NotificationQueue(
        incoming_controller=controller,
        workers=workers,
        maxsize=maxsize,
        session_context=_noop_session,
    )
    return queue, controller


class TestNotificationQueue:
    @pytest.mark.asyncio
    async def test_google_job_dispatches_to_controller(self) -> None:
        queue, controller = _make_queue()
        queue.start()
        try:
            assert queue.try_enqueue(
                NotificationJob(kind=GOOGLE, payload={"email_address": "a@b.co", "history_id": "42"})
            )
            await asyncio.wait_for(queue._queue.join(), timeout=2)
            controller.process_google_notification.assert_awaited_once_with("a@b.co", "42")
        finally:
            await queue.stop()

    @pytest.mark.asyncio
    async def test_microsoft_job_dispatches_to_controller(self) -> None:
        queue, controller = _make_queue()
        queue.start()
        try:
            notification = {"subscriptionId": "sub-1", "resourceData": {"id": "msg-1"}}
            assert queue.try_enqueue(NotificationJob(kind=MICROSOFT, payload={"notification": notification}))
            await asyncio.wait_for(queue._queue.join(), timeout=2)
            controller.process_microsoft_notification.assert_awaited_once_with(notification)
        finally:
            await queue.stop()

    @pytest.mark.asyncio
    async def test_full_queue_rejects_with_backpressure(self) -> None:
        queue, _ = _make_queue(workers=1, maxsize=2)
        # Not started: jobs accumulate.
        assert queue.try_enqueue(NotificationJob(kind=GOOGLE, payload={}))
        assert queue.try_enqueue(NotificationJob(kind=GOOGLE, payload={}))
        assert not queue.try_enqueue(NotificationJob(kind=GOOGLE, payload={}))
        assert queue.depth == 2

    @pytest.mark.asyncio
    async def test_failed_job_is_retried_then_succeeds(self) -> None:
        queue, controller = _make_queue(workers=1)
        controller.process_google_notification.side_effect = [RuntimeError("boom"), None]
        queue.start()
        try:
            queue.try_enqueue(NotificationJob(kind=GOOGLE, payload={"email_address": "a@b.co", "history_id": "1"}))
            await asyncio.wait_for(queue._queue.join(), timeout=2)
            assert controller.process_google_notification.await_count == 2
        finally:
            await queue.stop()

    @pytest.mark.asyncio
    async def test_persistently_failing_job_is_dropped_after_max_attempts(self) -> None:
        queue, controller = _make_queue(workers=1)
        controller.process_google_notification.side_effect = RuntimeError("boom")
        queue.start()
        try:
            queue.try_enqueue(NotificationJob(kind=GOOGLE, payload={"email_address": "a@b.co", "history_id": "1"}))
            await asyncio.wait_for(queue._queue.join(), timeout=2)
            assert controller.process_google_notification.await_count == 3  # MAX_JOB_ATTEMPTS
            assert queue.depth == 0
        finally:
            await queue.stop()

    @pytest.mark.asyncio
    async def test_unknown_kind_is_swallowed(self) -> None:
        queue, controller = _make_queue(workers=1)
        queue.start()
        try:
            queue.try_enqueue(NotificationJob(kind="carrier-pigeon", payload={}))
            await asyncio.wait_for(queue._queue.join(), timeout=2)
            controller.process_google_notification.assert_not_awaited()
            controller.process_microsoft_notification.assert_not_awaited()
        finally:
            await queue.stop()
