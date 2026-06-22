from typing import Any

from pydantic import BaseModel, Field


class GoogleNotificationJobPayload(BaseModel):
    model_config = {"extra": "forbid"}

    email_address: str = Field(min_length=1)
    history_id: str = Field(min_length=1)


class MicrosoftNotificationJobPayload(BaseModel):
    model_config = {"extra": "forbid"}

    notification: dict[str, Any]


class SubscriptionRenewalJobPayload(BaseModel):
    model_config = {"extra": "forbid"}

    account_id: int


class WebhookDeliveryJobPayload(BaseModel):
    model_config = {"extra": "forbid"}

    account_id: int
    event_type: str = Field(min_length=1)
    source: str = Field(default="nolas", min_length=1)
    object_data: dict[str, Any]
    email_id: str | None = None
    thread_id: str | None = None
