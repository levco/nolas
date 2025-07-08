from dataclasses import dataclass
from email.message import Message as PythonMessage

from app.api.models.messages import Message


@dataclass
class MessageResult:
    message: Message
    raw_message: PythonMessage
    uid: str | None = None
