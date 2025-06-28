import os
from dataclasses import dataclass


@dataclass
class AccountConfig:
    email: str
    username: str
    password: str
    provider: str
    webhook_url: str

    def __post_init__(self) -> None:
        if not all([self.email, self.username, self.password, self.provider, self.webhook_url]):
            raise ValueError("All account fields are required")


@dataclass
class WorkerConfig:
    worker_id: int
    accounts: list[AccountConfig]
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
    # IMAP server configurations
    IMAP_SERVERS = {
        "gmail.com": "imap.gmail.com",
        "outlook.com": "imap-mail.outlook.com",
        "purelymail": "imap.purelymail.com",
        "yahoo.com": "imap.mail.yahoo.com",
    }

    # Connection limits per provider (conservative to avoid rate limiting)
    CONNECTION_LIMITS = {
        "gmail.com": 8,
        "outlook.com": 15,
        "purelymail": 20,
        "yahoo.com": 10,
    }

    # Webhook settings
    WEBHOOK_TIMEOUT = int(os.getenv("WEBHOOK_TIMEOUT", "10"))
    WEBHOOK_MAX_RETRIES = int(os.getenv("WEBHOOK_MAX_RETRIES", "3"))

    # Monitoring
    HEALTH_CHECK_INTERVAL = int(os.getenv("HEALTH_CHECK_INTERVAL", "60"))

    # Rate limiting
    RATE_LIMIT_PER_PROVIDER = int(os.getenv("RATE_LIMIT_PER_PROVIDER", "10"))  # requests per second
