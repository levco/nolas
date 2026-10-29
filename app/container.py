import logging
from typing import cast

from dependency_injector import containers, providers

from app.controllers.container import ControllerContainer
from app.init_redis_utils import close_redis_instance
from app.repos.container import RepoContainer
from app.services.container import ServiceContainer

logger = logging.getLogger(__name__)


class ApplicationContainer(containers.DeclarativeContainer):
    repos: RepoContainer = cast(RepoContainer, providers.Container(RepoContainer))
    controllers: ControllerContainer = cast(ControllerContainer, providers.Container(ControllerContainer, repos=repos))
    services: ServiceContainer = cast(ServiceContainer, providers.Container(ServiceContainer))


def get_wire_container() -> ApplicationContainer:
    application_container = ApplicationContainer()

    application_container.wire(packages=["app.api.v3"])

    return application_container


async def close_controller_resources(container: ApplicationContainer) -> None:
    for name, close_method in (
        ("google_http_client", "close"),
        ("microsoft_http_client", "close"),
        ("token_service", "close"),
        ("webhook_sender", "close_session"),
        ("redis_client", "aclose"),
    ):
        try:
            resource = getattr(container.controllers, name)()
            if name == "redis_client":
                await close_redis_instance(resource)
            else:
                await getattr(resource, close_method)()
        except Exception:
            logger.exception("Failed to close %s during shutdown", name)
