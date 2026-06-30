from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.api.payloads.messages import (
    AttachmentData,
    EmailAddress,
    Message,
    MessageAttachment,
)
from app.api.payloads.threads import Thread
from app.models.account import Account


@dataclass
class ListMessagesParams:
    """Normalized Nylas v3 messages.list query parameters."""

    limit: int = 50
    page_token: str | None = None
    thread_id: str | None = None
    in_: str | None = None
    from_: str | None = None
    any_email: list[str] = field(default_factory=list)
    subject: str | None = None
    received_after: int | None = None
    received_before: int | None = None
    include_headers: bool = False
    # Provider-native query (Microsoft Graph $filter / Gmail q) passed through verbatim.
    search_query_native: str | None = None


@dataclass
class ListMessagesResult:
    messages: list[Message]
    next_cursor: str | None = None


@dataclass
class ListThreadsParams:
    """Normalized Nylas v3 threads.list query parameters."""

    limit: int = 20
    page_token: str | None = None
    in_: str | None = None
    from_: str | None = None
    to: str | None = None
    cc: str | None = None
    bcc: str | None = None
    any_email: list[str] = field(default_factory=list)
    subject: str | None = None
    latest_message_after: int | None = None
    latest_message_before: int | None = None
    unread: bool | None = None
    starred: bool | None = None
    has_attachment: bool | None = None
    # Provider-native query (Microsoft Graph $filter / Gmail q) passed through verbatim.
    search_query_native: str | None = None


@dataclass
class ListThreadsResult:
    threads: list[Thread]
    next_cursor: str | None = None


@dataclass
class ProviderSendResult:
    message_id: str
    thread_id: str


@dataclass
class FolderData:
    id: str
    name: str
    total_count: int | None = None
    unread_count: int | None = None
    attributes: list[str] = field(default_factory=list)


@dataclass
class AttachmentContent:
    data: bytes
    content_type: str
    filename: str


class ProviderClient(ABC):
    """Provider-specific email operations, normalized to Nylas v3 shapes."""

    @abstractmethod
    async def get_message(self, account: Account, message_id: str, include_headers: bool = False) -> Message | None:
        raise NotImplementedError

    @abstractmethod
    async def list_messages(self, account: Account, params: ListMessagesParams) -> ListMessagesResult:
        raise NotImplementedError

    @abstractmethod
    async def list_threads(self, account: Account, params: ListThreadsParams) -> ListThreadsResult:
        raise NotImplementedError

    @abstractmethod
    async def send_message(
        self,
        account: Account,
        to: list[EmailAddress],
        subject: str,
        body: str,
        from_: list[EmailAddress] | None = None,
        cc: list[EmailAddress] | None = None,
        bcc: list[EmailAddress] | None = None,
        reply_to: list[EmailAddress] | None = None,
        reply_to_message_id: str | None = None,
        attachments: list[AttachmentData] | None = None,
    ) -> ProviderSendResult:
        raise NotImplementedError

    @abstractmethod
    async def get_attachment_metadata(
        self, account: Account, message_id: str, attachment_id: str
    ) -> MessageAttachment | None:
        raise NotImplementedError

    @abstractmethod
    async def download_attachment(
        self, account: Account, message_id: str, attachment_id: str
    ) -> AttachmentContent | None:
        raise NotImplementedError

    @abstractmethod
    async def get_folder(self, account: Account, folder_id: str) -> FolderData | None:
        raise NotImplementedError
