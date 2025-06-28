import os
from typing import TYPE_CHECKING, cast

from pydantic_settings import BaseSettings


def get_settings() -> BaseSettings:
    if os.getenv("LEV_ENV") == "test":
        from .test_settings import TestSettings

        return TestSettings()

    from .settings import Settings

    return Settings()


if TYPE_CHECKING:
    from .settings import Settings

    settings = cast(Settings, get_settings())
else:
    settings = get_settings()
