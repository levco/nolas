import logging

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings

from app.environment import EnvironmentName
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
    poll_interval: int = Field(alias="IMAP_POLL_INTERVAL", default=60)
    poll_jitter_max: int = Field(alias="IMAP_POLL_JITTER", default=30)
    listener_mode: str = Field(alias="IMAP_LISTENER_MODE", default="single")


class WebhookSettings(BaseSettings):
    max_retries: int = Field(alias="WEBHOOK_MAX_RETRIES", default=3)
    timeout: int = Field(alias="WEBHOOK_TIMEOUT", default=10)


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "extra": "allow"}

    environment: EnvironmentName = Field(alias="ENVIRONMENT")
    password_encryption_key: str = Field(alias="PASSWORD_ENCRYPTION_KEY")

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
