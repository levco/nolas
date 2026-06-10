import asyncio
import base64
import json
import logging
import uuid
from typing import Any

from app.api.payloads.messages import AttachmentData, EmailAddress, Message, MessageAttachment
from app.controllers.providers.base import (
    AttachmentContent,
    FolderData,
    ListMessagesParams,
    ListMessagesResult,
    ProviderClient,
    ProviderSendResult,
)
from app.controllers.providers.exceptions import ProviderError, ProviderNotFoundError
from app.controllers.providers.google.mapper import gmail_label_name, map_gmail_message
from app.controllers.providers.google.query import build_gmail_query
from app.controllers.providers.http import AuthorizedHttpClient
from app.controllers.providers.mime import build_mime_message
from app.models.account import Account

logger = logging.getLogger(__name__)

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
GMAIL_UPLOAD_BASE = "https://gmail.googleapis.com/upload/gmail/v1/users/me"

# Concurrent message hydrations per list call.
FETCH_CONCURRENCY = 10


class GmailClient(ProviderClient):
    """Gmail API implementation of the provider interface."""

    def __init__(self, http_client: AuthorizedHttpClient) -> None:
        self._http = http_client

    async def get_message(self, account: Account, message_id: str, include_headers: bool = False) -> Message | None:
        try:
            raw = await self._http.request(
                account, "GET", f"{GMAIL_API_BASE}/messages/{message_id}", params={"format": "full"}
            )
        except ProviderNotFoundError:
            return None
        return map_gmail_message(raw, account.uuid, include_headers=include_headers)

    async def list_messages(self, account: Account, params: ListMessagesParams) -> ListMessagesResult:
        if params.thread_id:
            return await self._list_thread_messages(account, params)

        query_params: dict[str, Any] = {
            "q": build_gmail_query(params),
            "maxResults": params.limit,
        }
        if params.page_token:
            query_params["pageToken"] = params.page_token

        listing = await self._http.request(account, "GET", f"{GMAIL_API_BASE}/messages", params=query_params)
        ids = [item["id"] for item in listing.get("messages", [])]
        messages = await self._hydrate_messages(account, ids, params.include_headers)
        return ListMessagesResult(messages=messages, next_cursor=listing.get("nextPageToken"))

    async def _list_thread_messages(self, account: Account, params: ListMessagesParams) -> ListMessagesResult:
        try:
            thread = await self._http.request(
                account, "GET", f"{GMAIL_API_BASE}/threads/{params.thread_id}", params={"format": "full"}
            )
        except ProviderNotFoundError:
            return ListMessagesResult(messages=[])

        messages = [
            map_gmail_message(raw, account.uuid, include_headers=params.include_headers)
            for raw in thread.get("messages", [])
            if "DRAFT" not in raw.get("labelIds", [])
        ]
        messages = _filter_in_memory(messages, params)
        return ListMessagesResult(messages=messages[: params.limit])

    async def _hydrate_messages(self, account: Account, ids: list[str], include_headers: bool) -> list[Message]:
        semaphore = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def fetch(message_id: str) -> Message | None:
            async with semaphore:
                try:
                    return await self.get_message(account, message_id, include_headers=include_headers)
                except ProviderNotFoundError:
                    return None

        results = await asyncio.gather(*[fetch(message_id) for message_id in ids])
        return [message for message in results if message is not None]

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
        thread_id: str | None = None
        in_reply_to: str | None = None
        references: str | None = None

        if reply_to_message_id:
            original = await self._http.request(
                account,
                "GET",
                f"{GMAIL_API_BASE}/messages/{reply_to_message_id}",
                params={"format": "metadata", "metadataHeaders": ["Message-ID", "References"]},
            )
            thread_id = original.get("threadId")
            original_headers = original.get("payload", {}).get("headers", [])
            for header in original_headers:
                if header.get("name", "").lower() == "message-id":
                    in_reply_to = header.get("value")
                elif header.get("name", "").lower() == "references":
                    references = header.get("value")
            if in_reply_to:
                references = f"{references} {in_reply_to}".strip() if references else in_reply_to

        sender_email = (from_ or [EmailAddress(name=account.email, email=account.email)])[0].email
        raw_mime, _ = build_mime_message(
            to=to,
            subject=subject,
            body=body,
            from_=from_ or [EmailAddress(name=account.email, email=account.email)],
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            attachments=attachments,
            in_reply_to=in_reply_to,
            references=references,
            sender_domain=sender_email.split("@")[-1] if "@" in sender_email else None,
        )

        # Multipart upload: JSON metadata part (threadId) + raw RFC822 part.
        boundary = f"nolas-{uuid.uuid4().hex}"
        metadata: dict[str, Any] = {}
        if thread_id:
            metadata["threadId"] = thread_id
        multipart_body = (
            (
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(metadata)}\r\n"
                f"--{boundary}\r\nContent-Type: message/rfc822\r\n\r\n"
            ).encode()
            + raw_mime
            + f"\r\n--{boundary}--".encode()
        )

        response = await self._http.request(
            account,
            "POST",
            f"{GMAIL_UPLOAD_BASE}/messages/send",
            params={"uploadType": "multipart"},
            data=multipart_body,
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
        )

        message_id = response.get("id")
        if not message_id:
            raise ProviderError(f"Gmail send did not return a message id: {response}")
        return ProviderSendResult(message_id=message_id, thread_id=response.get("threadId", message_id))

    async def get_attachment_metadata(
        self, account: Account, message_id: str, attachment_id: str
    ) -> MessageAttachment | None:
        message = await self.get_message(account, message_id)
        if message is None:
            return None
        for attachment in message.attachments:
            if attachment.id == attachment_id:
                return attachment
        return None

    async def download_attachment(
        self, account: Account, message_id: str, attachment_id: str
    ) -> AttachmentContent | None:
        metadata = await self.get_attachment_metadata(account, message_id, attachment_id)
        if metadata is None:
            return None
        try:
            response = await self._http.request(
                account, "GET", f"{GMAIL_API_BASE}/messages/{message_id}/attachments/{attachment_id}"
            )
        except ProviderNotFoundError:
            return None
        data = base64.urlsafe_b64decode(response.get("data", "").encode())
        return AttachmentContent(data=data, content_type=metadata.content_type, filename=metadata.filename)

    async def get_folder(self, account: Account, folder_id: str) -> FolderData | None:
        try:
            label = await self._http.request(account, "GET", f"{GMAIL_API_BASE}/labels/{folder_id}")
        except ProviderNotFoundError:
            return None
        return FolderData(
            id=label["id"],
            name=gmail_label_name(label["id"], label.get("name")),
            total_count=label.get("messagesTotal"),
            unread_count=label.get("messagesUnread"),
            attributes=[label.get("type", "user")],
        )

    # --- Notification / watch helpers (used by the notifications controller and renewal worker) ---

    async def get_profile(self, account: Account) -> dict[str, Any]:
        return dict(await self._http.request(account, "GET", f"{GMAIL_API_BASE}/profile"))

    async def watch(self, account: Account, topic_name: str) -> dict[str, Any]:
        return dict(
            await self._http.request(account, "POST", f"{GMAIL_API_BASE}/watch", json_body={"topicName": topic_name})
        )

    async def stop_watch(self, account: Account) -> None:
        await self._http.request(account, "POST", f"{GMAIL_API_BASE}/stop", expect_json=False)

    async def list_history(
        self, account: Account, start_history_id: str, page_token: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"startHistoryId": start_history_id, "historyTypes": "messageAdded"}
        if page_token:
            params["pageToken"] = page_token
        return dict(await self._http.request(account, "GET", f"{GMAIL_API_BASE}/history", params=params))


def _filter_in_memory(messages: list[Message], params: ListMessagesParams) -> list[Message]:
    """Apply residual filters when listing within a thread."""
    result = messages
    if params.from_:
        result = [m for m in result if any(a.email.lower() == params.from_.lower() for a in m.from_)]
    if params.any_email:
        wanted = {email.lower() for email in params.any_email}
        result = [m for m in result if wanted & {a.email.lower() for a in [*m.from_, *m.to, *m.cc, *m.bcc]}]
    if params.received_after is not None:
        result = [m for m in result if m.date >= params.received_after]
    if params.received_before is not None:
        result = [m for m in result if m.date <= params.received_before]
    if params.subject:
        result = [m for m in result if m.subject == params.subject]
    return result
