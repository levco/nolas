import os
from dataclasses import dataclass
from typing import Sequence

from app.models import Account


@dataclass
class WorkerConfig:
    worker_id: int
    accounts: Sequence[Account]
    max_connections_per_provider: int = 50


@dataclass
class EmailMessage:
    uid: int
    account: str
    folder: str
    from_address: str | None
    subject: str | None
    raw_message: bytes


class Config:
    # Webhook settings
    WEBHOOK_TIMEOUT = int(os.getenv("WEBHOOK_TIMEOUT", "10"))
    WEBHOOK_MAX_RETRIES = int(os.getenv("WEBHOOK_MAX_RETRIES", "3"))

    # Monitoring
    HEALTH_CHECK_INTERVAL = int(os.getenv("HEALTH_CHECK_INTERVAL", "60"))

    # Rate limiting
    RATE_LIMIT_PER_PROVIDER = int(os.getenv("RATE_LIMIT_PER_PROVIDER", "10"))  # requests per second
