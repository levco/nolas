from dependency_injector import containers, providers

from app.repos.account import AccountRepo
from app.repos.connection_health import ConnectionHealthRepo
from app.repos.uid_tracking import UidTrackingRepo
from app.repos.webhook_log import WebhookLogRepo


class RepoContainer(containers.DeclarativeContainer):
    # Simplified repos using fastapi_async_sqlalchemy directly
    account = providers.Singleton(AccountRepo)
    connection_health = providers.Singleton(ConnectionHealthRepo)
    uid_tracking = providers.Singleton(UidTrackingRepo)
    webhook_log = providers.Singleton(WebhookLogRepo)
