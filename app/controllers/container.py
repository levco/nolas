from typing import cast

from dependency_injector import containers, providers

from app.controllers.email.email_controller import EmailController
from app.controllers.grant.authorization_controller import AuthorizationController
from app.controllers.grant.custom_auth_controller import CustomAuthController
from app.controllers.grant.grant_controller import GrantController
from app.controllers.imap.connection import ConnectionManager
from app.controllers.imap.email_processor import EmailProcessor
from app.controllers.imap.listener import IMAPListener
from app.controllers.imap.message_controller import MessageController
from app.controllers.notifications.incoming_controller import IncomingNotificationController
from app.controllers.notifications.queue import NotificationQueue
from app.controllers.notifications.subscription_manager import SubscriptionManager
from app.controllers.providers.google.gmail_client import GmailClient
from app.controllers.providers.http import AuthorizedHttpClient
from app.controllers.providers.imap_adapter import ImapProviderAdapter
from app.controllers.providers.microsoft.graph_client import GraphClient
from app.controllers.providers.registry import ProviderRegistry
from app.controllers.providers.token_service import TokenService
from app.controllers.smtp.smtp_controller import SMTPController
from app.controllers.webhooks.sender import WebhookSender
from app.repos.container import RepoContainer
from settings import settings


class ControllerContainer(containers.DeclarativeContainer):
    repos: RepoContainer = cast(RepoContainer, providers.DependenciesContainer())

    imap_email_processor = providers.Singleton(
        EmailProcessor, webhook_log_repo=repos.webhook_log, email_repo=repos.email
    )
    imap_connection_manager = providers.Singleton(ConnectionManager)
    imap_message_controller = providers.Singleton(MessageController, connection_manager=imap_connection_manager)
    imap_listener = providers.Singleton(
        IMAPListener,
        connection_health_repo=repos.connection_health,
        uid_tracking_repo=repos.uid_tracking,
        connection_manager=imap_connection_manager,
        email_processor=imap_email_processor,
        email_repo=repos.email,
    )

    smtp_controller = providers.Singleton(SMTPController, connection_manager=imap_connection_manager)

    email_controller = providers.Singleton(
        EmailController,
        email_repo=repos.email,
        message_controller=imap_message_controller,
        smtp_controller=smtp_controller,
    )

    grant_controller = providers.Singleton(
        GrantController,
        account_repo=repos.account,
        uid_tracking_repo=repos.uid_tracking,
    )

    authorization_controller = providers.Singleton(
        AuthorizationController,
        account_repo=repos.account,
        oauth2_authorization_request_repo=repos.oauth2_authorization_request,
        connection_manager=imap_connection_manager,
        smtp_controller=smtp_controller,
    )

    # --- Google / Microsoft provider stack ---

    webhook_sender = providers.Singleton(WebhookSender, webhook_log_repo=repos.webhook_log)

    token_service = providers.Singleton(TokenService, account_repo=repos.account, webhook_sender=webhook_sender)

    google_http_client = providers.Singleton(
        AuthorizedHttpClient, token_service=token_service, timeout=settings.google.request_timeout
    )
    microsoft_http_client = providers.Singleton(
        AuthorizedHttpClient, token_service=token_service, timeout=settings.microsoft.request_timeout
    )

    gmail_client = providers.Singleton(GmailClient, http_client=google_http_client)
    graph_client = providers.Singleton(GraphClient, http_client=microsoft_http_client)

    imap_provider_adapter = providers.Singleton(
        ImapProviderAdapter,
        email_controller=email_controller,
        connection_manager=imap_connection_manager,
    )

    provider_registry = providers.Singleton(
        ProviderRegistry,
        gmail_client=gmail_client,
        graph_client=graph_client,
        imap_adapter=imap_provider_adapter,
    )

    subscription_manager = providers.Singleton(
        SubscriptionManager,
        account_repo=repos.account,
        gmail_client=gmail_client,
        graph_client=graph_client,
    )

    custom_auth_controller = providers.Singleton(
        CustomAuthController,
        account_repo=repos.account,
        token_service=token_service,
        subscription_manager=subscription_manager,
    )

    incoming_notification_controller = providers.Singleton(
        IncomingNotificationController,
        account_repo=repos.account,
        email_repo=repos.email,
        gmail_client=gmail_client,
        graph_client=graph_client,
        webhook_sender=webhook_sender,
    )

    notification_queue = providers.Singleton(
        NotificationQueue,
        incoming_controller=incoming_notification_controller,
        workers=settings.notification_queue.workers,
        maxsize=settings.notification_queue.maxsize,
    )
