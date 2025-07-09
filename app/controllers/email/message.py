from dataclasses import dataclass
from email.message import Message as PythonMessage

from app.api.models.messages import Message, SendMessageData


@dataclass
class MessageResult:
    message: Message
    raw_message: PythonMessage
    uid: int | None = None


@dataclass
class SendMessageResult:
    message: SendMessageData
    message_id: str
    thread_id: str
    folder: str | None = None
