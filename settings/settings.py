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


class SentrySettings(BaseSettings):
    is_enabled: bool = Field(alias="SENTRY_ENABLED", default=False)
    dsn: str = Field(alias="SENTRY_DSN", default="")


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


class ApiSettings(BaseSettings):
    # Public HTTPS base URL of this service (no trailing slash). Required for Microsoft Graph
    # change-notification subscriptions and referenced by Google Pub/Sub push configuration.
    public_base_url: str = Field(alias="API_PUBLIC_BASE_URL", default="")


class GoogleProviderSettings(BaseSettings):
    client_id: str = Field(alias="GOOGLE_CLIENT_ID", default="")
    client_secret: str = Field(alias="GOOGLE_CLIENT_SECRET", default="")
    # Fully-qualified Pub/Sub topic for Gmail watch, e.g. projects/<project>/topics/<topic>.
    pubsub_topic: str = Field(alias="GOOGLE_PUBSUB_TOPIC", default="")
    # Shared secret expected as ?token= on the Pub/Sub push endpoint.
    pubsub_verification_token: str = Field(alias="GOOGLE_PUBSUB_VERIFICATION_TOKEN", default="")
    request_timeout: int = Field(alias="GOOGLE_REQUEST_TIMEOUT", default=30)


class MicrosoftProviderSettings(BaseSettings):
    client_id: str = Field(alias="MICROSOFT_CLIENT_ID", default="")
    client_secret: str = Field(alias="MICROSOFT_CLIENT_SECRET", default="")
    authority: str = Field(alias="MICROSOFT_AUTHORITY", default="https://login.microsoftonline.com/common")
    scopes: str = Field(
        alias="MICROSOFT_SCOPES",
        default="offline_access https://graph.microsoft.com/User.Read "
        "https://graph.microsoft.com/Mail.ReadWrite https://graph.microsoft.com/Mail.Send",
    )
    request_timeout: int = Field(alias="MICROSOFT_REQUEST_TIMEOUT", default=30)


class SubscriptionRenewalSettings(BaseSettings):
    # How often the renewal worker scans accounts, in seconds.
    poll_interval: int = Field(alias="SUBSCRIPTION_RENEWAL_POLL_INTERVAL", default=3600)
    # Renew watches/subscriptions expiring within this many hours.
    renew_within_hours: int = Field(alias="SUBSCRIPTION_RENEWAL_WITHIN_HOURS", default=24)


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "extra": "allow"}

    environment: EnvironmentName = Field(alias="ENVIRONMENT")
    password_encryption_key: str = Field(alias="PASSWORD_ENCRYPTION_KEY")

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    imap: IMAPSettings = Field(default_factory=IMAPSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    sentry: SentrySettings = Field(default_factory=SentrySettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    webhook: WebhookSettings = Field(default_factory=WebhookSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    google: GoogleProviderSettings = Field(default_factory=GoogleProviderSettings)
    microsoft: MicrosoftProviderSettings = Field(default_factory=MicrosoftProviderSettings)
    subscription_renewal: SubscriptionRenewalSettings = Field(default_factory=SubscriptionRenewalSettings)

    @field_validator("environment", mode="before")
    def set_logging_level(cls, level: str, info: ValidationInfo) -> EnvironmentName:
        try:
            return EnvironmentName(level)
        except ValueError:
            logging.getLogger(__name__).warning(f"Invalid environment: {level}")
            return EnvironmentName.DEVELOPMENT
