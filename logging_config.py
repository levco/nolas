import logging
import logging.config
from typing import Any

from pythonjsonlogger.json import JsonFormatter

from environment import EnvironmentName
from settings import settings

JSON_FORMAT = (
    "%(module)s %(asctime)s %(levelname)s %(thread)d %(processName)s %(task_name)s %(task_id)s %(name)s "
    "%(funcName)s %(filename)s %(lineno)d %(message)s"
)
LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "jsonFormat": {
            "format": JSON_FORMAT,
            "class": "logging_config.CustomJsonFormatter",
        },
        "standardFormat": {
            "format": (
                "%(asctime)s %(levelname)s %(thread)d %(processName)s [%(name)s] [%(funcName)s] "
                "[%(filename)s:%(lineno)d] - %(message)s"
            )
        },
        "standardTaskFormat": {
            "format": (
                "%(asctime)s %(levelname)s %(thread)d %(processName)s %(task_name)s [%(task_id)s] [%(name)s] "
                "[%(funcName)s] [%(filename)s:%(lineno)d] - %(message)s"
            )
        },
    },
    "handlers": {
        "jsonStreamHandler": {
            "formatter": "jsonFormat",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",  # Default is stderr
        },
    },
    "loggers": {
        "": {"handlers": ["jsonStreamHandler"], "propagate": False},
        # werkzeug logging request twice.  This ensures it only logged once (propogate set to false)
        "werkzeug": {
            "handlers": ["jsonStreamHandler"],
            "propagate": False,
        },
    },
}
LOCAL_LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {"format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s"},
    },
    "handlers": {
        "default": {
            "formatter": "standard",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",  # Default is stderr
        },
    },
    "loggers": {
        "": {
            "handlers": ["default"],
            "level": settings.logging.level,
            "propagate": False,
        },
        # werkzeug logging request twice.  This ensures it only logged once (propogate set to false)
        "werkzeug": {"handlers": ["default"], "propagate": False},
        "gql": {"handlers": ["default"], "level": logging.WARNING, "propagate": False},
        "botocore": {"handlers": ["default"], "level": logging.WARNING, "propagate": False},
        "urllib3": {"handlers": ["default"], "level": logging.WARNING, "propagate": False},
        "pymongo": {"handlers": ["default"], "level": logging.WARNING, "propagate": False},
        "aioimaplib": {"handlers": ["default"], "level": logging.WARNING, "propagate": False},
        "asyncio": {"handlers": ["default"], "level": logging.WARNING, "propagate": False},
        "python_multipart": {"handlers": ["default"], "level": logging.WARNING, "propagate": False},
    },
}


# Used because we add custom local formatting.
class CustomJsonFormatter(JsonFormatter):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._app_env = settings.environment
        self._pretty_format = settings.logging.use_pretty_json
        # For local development we want to make logs more clear
        if self._app_env == EnvironmentName.DEVELOPMENT and self._pretty_format:
            self.json_indent = 2

    def format(self, record: logging.LogRecord) -> str:
        result = super(CustomJsonFormatter, self).format(record)
        # For local development we want to make logs more clear.
        if self._app_env == EnvironmentName.DEVELOPMENT and self._pretty_format:
            result = result.replace("\\n", "\n\t\t")
        return result


def setup_logging() -> None:
    if settings.logging.use_config is True:
        """Setup root logger using our logging config"""
        logging.config.dictConfig(LOGGING_CONFIG)
        logging.captureWarnings(True)
        logging.disable(logging.NOTSET)
    else:
        logging.config.dictConfig(LOCAL_LOGGING_CONFIG)
        logging.captureWarnings(True)
        logging.disable(logging.NOTSET)
