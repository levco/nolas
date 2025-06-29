from typing import Any

from pydantic import BaseModel


class AccountConfig(BaseModel):
    id: int
    email: str
    credentials: str
    provider: str
    provider_context: dict[str, Any]
    webhook_url: str
