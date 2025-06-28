import logging

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings


class LoggingSettings(BaseSettings):
    use_config: bool = Field(alias="LOGGING_USE_CONFIG", default=True)
    use_pretty_json: bool = Field(alias="LOGGING_USE_PRETTY_JSON", default=True)
    level: int = Field(alias="LOGGING_LEVEL", default=logging.INFO)

    @field_validator("level", mode="before")
    def set_logging_level(cls, level: str, info: ValidationInfo) -> int:
        return cls._get_logging_level(level)

    @classmethod
    def _get_logging_level(cls, level: str | None) -> int:
        if level == "fatal":
            return logging.FATAL
        elif level == "error":
            return logging.ERROR
        elif level == "warning":
            return logging.WARNING
        elif level == "info":
            return logging.INFO
        elif level == "debug":
            return logging.DEBUG
        elif level == "notset":
            return logging.NOTSET

        print("Invalid logging level, using INFO by default.")
        return logging.INFO
