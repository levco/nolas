from app.models import WebhookLog
from app.repos.base import BaseRepo


class WebhookLogRepo(BaseRepo[WebhookLog]):
    """Repository for WebhookLog model operations."""

    def __init__(self) -> None:
        super().__init__(WebhookLog)
