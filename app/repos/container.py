from dependency_injector import containers, providers

from app.repos.account import AccountRepo
from app.repos.app import AppRepo
from app.repos.connection_health import ConnectionHealthRepo
from app.repos.email import EmailRepo
from app.repos.oauth2 import OAuth2AuthorizationRequestRepo
from app.repos.uid_tracking import UidTrackingRepo
from app.repos.webhook_log import WebhookLogRepo


class RepoContainer(containers.DeclarativeContainer):
    app = providers.Singleton(AppRepo)
    account = providers.Singleton(AccountRepo)
    connection_health = providers.Singleton(ConnectionHealthRepo)
    email = providers.Singleton(EmailRepo)
    oauth2_authorization_request = providers.Singleton(OAuth2AuthorizationRequestRepo)
    uid_tracking = providers.Singleton(UidTrackingRepo)
    webhook_log = providers.Singleton(WebhookLogRepo)
