from typing import cast

from dependency_injector import containers, providers

from app.controllers.container import ControllerContainer
from app.repos.container import RepoContainer
from app.services.container import ServiceContainer


class ApplicationContainer(containers.DeclarativeContainer):
    repos: RepoContainer = cast(RepoContainer, providers.Container(RepoContainer))
    controllers: ControllerContainer = cast(ControllerContainer, providers.Container(ControllerContainer, repos=repos))
    services: ServiceContainer = cast(ServiceContainer, providers.Container(ServiceContainer))


def get_wire_container() -> ApplicationContainer:
    application_container = ApplicationContainer()

    application_container.wire(packages=["app.api.v3"])

    return application_container
