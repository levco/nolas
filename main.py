import sentry_sdk

from app.container import get_wire_container
from app.create_app import create_app
from logging_config import setup_logging
from settings import settings

if settings.sentry.is_enabled:
    sentry_sdk.init(dsn=settings.sentry.dsn, environment=settings.environment.value, send_default_pii=True)

setup_logging()
container = get_wire_container()
app = create_app()
