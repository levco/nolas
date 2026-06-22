from dependency_injector import containers, providers

from app.services.google_oidc import GooglePubSubOidcVerifier
from settings import settings


class ServiceContainer(containers.DeclarativeContainer):
    google_oidc_verifier = providers.Singleton(
        GooglePubSubOidcVerifier,
        audience=settings.google.pubsub_oidc_audience,
        service_account_email=settings.google.pubsub_oidc_service_account_email,
    )
