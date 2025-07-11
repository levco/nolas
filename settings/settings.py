import logging

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings

from environment import EnvironmentName
from settings.log import LoggingSettings


class DatabaseSettings(BaseSettings):
    host: str = Field(alias="DATABASE_HOST", default="postgresql://localhost:5432")
    name: str = Field(alias="DATABASE_NAME", default="nolas")
    min_pool_size: int = Field(alias="DATABASE_MIN_POOL_SIZE", default=5)
    max_pool_size: int = Field(alias="DATABASE_MAX_POOL_SIZE", default=20)

    @property
    def async_host(self) -> str:
        """Return the host URL with async driver for SQLAlchemy async engine."""
        return self.host.replace("postgresql://", "postgresql+asyncpg://", 1)


class WorkerSettings(BaseSettings):
    num_workers: int = Field(alias="WORKERS_NUM", default=2)
    max_connections_per_provider: int = Field(alias="WORKER_MAX_CONNECTIONS_PER_PROVIDER", default=50)


class IMAPSettings(BaseSettings):
    timeout: int = Field(alias="IMAP_TIMEOUT", default=300)
    idle_timeout: int = Field(alias="IMAP_IDLE_TIMEOUT", default=1740)  # 29 minutes (RFC requirement)

    # Polling settings - much simpler than IDLE
    poll_interval: int = Field(alias="IMAP_POLL_INTERVAL", default=60)  # Poll every 60 seconds
    poll_jitter_max: int = Field(alias="IMAP_POLL_JITTER", default=30)  # Max jitter to spread load


class WebhookSettings(BaseSettings):
    max_retries: int = Field(alias="WEBHOOK_MAX_RETRIES", default=3)
    timeout: int = Field(alias="WEBHOOK_TIMEOUT", default=10)


class Settings(BaseSettings):
    environment: EnvironmentName = Field(alias="ENVIRONMENT")

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    imap: IMAPSettings = Field(default_factory=IMAPSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    webhook: WebhookSettings = Field(default_factory=WebhookSettings)

    @field_validator("environment", mode="before")
    def set_logging_level(cls, level: str, info: ValidationInfo) -> EnvironmentName:
        try:
            return EnvironmentName(level)
        except ValueError:
            logging.getLogger(__name__).warning(f"Invalid environment: {level}")
            return EnvironmentName.DEVELOPMENT
