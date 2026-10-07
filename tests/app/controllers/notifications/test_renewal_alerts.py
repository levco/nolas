import asyncio
import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from app.controllers.notifications import renewal_alerts
from app.controllers.notifications.renewal_alerts import RenewalAlerts
from app.models.job import Job
from settings.settings import SlackSettings


@pytest.fixture
def config(monkeypatch):
    config = SlackSettings(
        SLACK_BOT_TOKEN="test-token",
        SLACK_RENEWAL_ALERT_CHANNEL_ID="test-channel",
        SLACK_RENEWAL_FAILURE_THRESHOLD=3,
        SLACK_RENEWAL_FAILURE_WINDOW_SECONDS=60,
        SLACK_RENEWAL_ALERT_COOLDOWN_SECONDS=60,
    )
    monkeypatch.setattr(
        renewal_alerts, "settings", SimpleNamespace(slack=config, environment=SimpleNamespace(value=f"test-{uuid4()}"))
    )
    return config


@pytest_asyncio.fixture
async def redis(config):
    url = os.getenv("REDIS_URL")
    if not url:
        pytest.skip("Set REDIS_URL to run renewal alert integration checks")
    client = Redis.from_url(url, socket_timeout=1, socket_connect_timeout=1)
    key = f"nolas:{renewal_alerts.settings.environment.value}:renewal-failures"
    try:
        await client.ping()
        yield client, key
    finally:
        await client.delete(key, f"{key}:cooldown")
        await client.aclose()


def _job(identifier: int) -> Job:
    return Job(id=identifier, payload={"account_id": 123}, attempts=5, max_attempts=5)


@pytest.mark.asyncio
async def test_window_deduplication_and_cooldown_are_shared_by_replicas(redis) -> None:
    client, key = redis
    slack = Mock()
    first, second = RenewalAlerts(client, slack), RenewalAlerts(client, slack)
    await client.zadd(key, {"old": time.time() - 61})
    await first.record_failure(_job(1))
    await second.record_failure(_job(1))
    await second.record_failure(_job(2))
    slack.post_message_to_channel.assert_not_called()
    assert await client.zcard(key) == 2

    await asyncio.gather(first.record_failure(_job(3)), second.record_failure(_job(4)))
    slack.post_message_to_channel.assert_called_once()
    assert "exhausted their retries" in slack.post_message_to_channel.call_args.args[1]
    assert "account 123" in slack.post_message_to_channel.call_args.args[1]
    assert await client.ttl(key) > 0
    assert await client.ttl(f"{key}:cooldown") > 0

    # Simulate the end of the cooldown; a later exhausted job may alert again.
    await client.delete(f"{key}:cooldown")
    await second.record_failure(_job(5))
    assert slack.post_message_to_channel.call_count == 2


@pytest.mark.asyncio
async def test_slack_failure_does_not_interrupt_processing(redis, config) -> None:
    client, key = redis
    config.renewal_failure_threshold = 1
    slack = Mock()
    slack.post_message_to_channel.side_effect = RuntimeError("Slack unavailable")
    alerts = RenewalAlerts(client, slack)
    await alerts.record_failure(_job(1))
    await alerts.record_failure(_job(2))
    slack.post_message_to_channel.assert_called_once()
    assert await client.exists(f"{key}:cooldown")


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["bot_token", "renewal_alert_channel_id"])
async def test_disabled_without_credentials(config, missing) -> None:
    setattr(config, missing, "")
    client, slack = Mock(), Mock()
    await RenewalAlerts(client, slack).record_failure(_job(1))
    client.pipeline.assert_not_called()
    slack.post_message_to_channel.assert_not_called()


@pytest.mark.asyncio
async def test_redis_failure_does_not_interrupt_processing(config) -> None:
    client, slack = MagicMock(), Mock()
    client.pipeline.return_value.__aenter__.side_effect = RuntimeError("Redis unavailable")
    await RenewalAlerts(client, slack).record_failure(_job(1))
    client.pipeline.return_value.__aenter__.assert_awaited_once()
    slack.post_message_to_channel.assert_not_called()
