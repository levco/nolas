import asyncio
import json
import logging
import re
import uuid
from typing import TYPE_CHECKING, Any

from app.api.payloads.messages import (
    AttachmentData,
    EmailAddress,
    Message,
    MessageAttachment,
)
from app.controllers.providers.base import (
    AttachmentContent,
    FolderData,
    ListMessagesParams,
    ListMessagesResult,
    ListThreadsParams,
    ListThreadsResult,
    ProviderClient,
    ProviderSendResult,
)
from app.controllers.providers.exceptions import (
    ProviderError,
    ProviderNotFoundError,
    ProviderRateLimitError,
)
from app.controllers.providers.google.mapper import (
    decode_base64url,
    gmail_label_name,
    map_gmail_message,
)
from app.controllers.providers.google.query import (
    build_gmail_query,
    build_gmail_thread_query,
)
from app.controllers.providers.mime import build_mime_message
from app.controllers.providers.threads import (
    build_threads_from_messages,
    filter_threads,
)
from app.models.account import Account

logger = logging.getLogger(__name__)

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
GMAIL_UPLOAD_BASE = "https://gmail.googleapis.com/upload/gmail/v1/users/me"

# Parallel Gmail batch envelopes per list call.
BATCH_FETCH_CONCURRENCY = 2
# Per-envelope subrequests; lower reduces provider-side concurrency pressure.
BATCH_SIZE = 20
# Concurrency for per-message fallback fetches.
FALLBACK_FETCH_CONCURRENCY = 5
GMAIL_BATCH_BASE = "https://gmail.googleapis.com/batch/gmail/v1"

if TYPE_CHECKING:
    from app.controllers.providers.http import AuthorizedHttpClient


class GmailClient(ProviderClient):
    """Gmail API implementation of the provider interface."""

    def __init__(self, http_client: "AuthorizedHttpClient") -> None:
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

    async def list_threads(self, account: Account, params: ListThreadsParams) -> ListThreadsResult:
        query = build_gmail_thread_query(params)
        query_params: dict[str, Any] = {"maxResults": params.limit, "q": query}
        if params.page_token:
            query_params["pageToken"] = params.page_token

        listing = await self._http.request(account, "GET", f"{GMAIL_API_BASE}/threads", params=query_params)
        thread_ids = [item["id"] for item in listing.get("threads", [])]
        messages_by_thread = await self._hydrate_threads(account, thread_ids)
        all_messages = [message for thread_id in thread_ids for message in messages_by_thread.get(thread_id, [])]

        threads = build_threads_from_messages(all_messages)
        thread_by_id = {thread.id: thread for thread in threads}
        ordered_threads = [thread_by_id[thread_id] for thread_id in thread_ids if thread_id in thread_by_id]
        filtered = filter_threads(ordered_threads, all_messages, params)
        return ListThreadsResult(threads=filtered[: params.limit], next_cursor=listing.get("nextPageToken"))

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
        # Fetch the message fresh to get a current Gmail token. We cannot reuse
        # a previously stored attachmentId because Gmail regenerates it on every
        # message fetch, so any token obtained before this call is already stale.
        message = await self.get_message(account, message_id)
        if message is None:
            return None
        attachment = next((a for a in message.attachments if a.id == attachment_id), None)
        if attachment is None or attachment.provider_id is None:
            return None
        try:
            response = await self._http.request(
                account, "GET", f"{GMAIL_API_BASE}/messages/{message_id}/attachments/{attachment.provider_id}"
            )
        except ProviderNotFoundError:
            return None
        data = decode_base64url(response.get("data", ""))
        return AttachmentContent(data=data, content_type=attachment.content_type, filename=attachment.filename)

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

    async def _hydrate_threads(self, account: Account, thread_ids: list[str]) -> dict[str, list[Message]]:
        if not thread_ids:
            return {}

        semaphore = asyncio.Semaphore(FALLBACK_FETCH_CONCURRENCY)
        messages_by_thread: dict[str, list[Message]] = {}

        async def fetch(thread_id: str) -> None:
            async with semaphore:
                try:
                    thread = await self._http.request(
                        account, "GET", f"{GMAIL_API_BASE}/threads/{thread_id}", params={"format": "full"}
                    )
                except ProviderNotFoundError:
                    return
                messages_by_thread[thread_id] = [
                    map_gmail_message(raw, account.uuid, include_headers=False)
                    for raw in thread.get("messages", [])
                    if "DRAFT" not in raw.get("labelIds", [])
                ]

        await asyncio.gather(*[fetch(thread_id) for thread_id in thread_ids])
        return messages_by_thread

    async def _hydrate_messages(self, account: Account, ids: list[str], include_headers: bool) -> list[Message]:
        if not ids:
            return []

        semaphore = asyncio.Semaphore(BATCH_FETCH_CONCURRENCY)
        message_by_id: dict[str, Message] = {}

        async def fetch_batch(batch_ids: list[str]) -> None:
            async with semaphore:
                message_by_id.update(await self._fetch_message_batch(account, batch_ids, include_headers))

        batches = [ids[i : i + BATCH_SIZE] for i in range(0, len(ids), BATCH_SIZE)]
        await asyncio.gather(*[fetch_batch(batch_ids) for batch_ids in batches])
        return [message_by_id[message_id] for message_id in ids if message_id in message_by_id]

    async def _fetch_message_batch(
        self, account: Account, message_ids: list[str], include_headers: bool
    ) -> dict[str, Message]:
        boundary = f"batch_nolas_{uuid.uuid4().hex}"
        request_lines: list[str] = []

        for message_id in message_ids:
            request_lines.extend(
                [
                    f"--{boundary}",
                    "Content-Type: application/http",
                    "Content-Transfer-Encoding: binary",
                    f"Content-ID: <{message_id}>",
                    "",
                    f"GET /gmail/v1/users/me/messages/{message_id}?format=full HTTP/1.1",
                    "",
                ]
            )
        request_lines.extend([f"--{boundary}--", ""])

        try:
            response_body = await self._http.request(
                account,
                "POST",
                GMAIL_BATCH_BASE,
                data="\r\n".join(request_lines).encode(),
                headers={"Content-Type": f"multipart/mixed; boundary={boundary}"},
                expect_json=False,
            )
            messages, rate_limited_ids = self._parse_batch_response(
                response_body, account, include_headers, valid_message_ids=set(message_ids)
            )
            if rate_limited_ids:
                logger.warning(
                    "Gmail batch partially rate limited (%s/%s); retrying those messages individually",
                    len(rate_limited_ids),
                    len(message_ids),
                )
                messages.update(await self._fetch_messages_individually(account, rate_limited_ids, include_headers))
            return messages
        except ProviderRateLimitError:
            logger.warning("Gmail batch request rate limited; falling back to per-message fetch")
            return await self._fetch_messages_individually(account, message_ids, include_headers)
        except ProviderError:
            logger.exception("Gmail batch hydration failed; falling back to per-message fetch")
            return await self._fetch_messages_individually(account, message_ids, include_headers)

    def _parse_batch_response(
        self, response_body: bytes, account: Account, include_headers: bool, valid_message_ids: set[str]
    ) -> tuple[dict[str, Message], list[str]]:
        boundary = self._extract_batch_boundary(response_body)
        parts = response_body.split(boundary)
        messages: dict[str, Message] = {}
        rate_limited_ids: list[str] = []

        for raw_part in parts:
            part = raw_part.strip()
            if not part or part == b"--":
                continue
            if part.endswith(b"--"):
                part = part[:-2].rstrip()

            part_headers, nested_http = self._split_headers_body(part)
            status_code, nested_body = self._parse_nested_http_response(nested_http)

            if status_code == 404:
                continue
            if status_code == 429:
                raw_content_id = self._extract_content_id(part_headers)
                if raw_content_id is None:
                    raise ProviderRateLimitError("Gmail batch subrequest rate limited and content-id is missing")
                content_id = self._normalize_batch_content_id(raw_content_id, valid_message_ids)
                if content_id is None:
                    raise ProviderRateLimitError(
                        f"Gmail batch subrequest rate limited with unknown content-id: {raw_content_id}"
                    )
                rate_limited_ids.append(content_id)
                continue
            if status_code < 200 or status_code >= 300:
                raise ProviderError(
                    f"Gmail batch subrequest failed ({status_code}): {nested_body[:500].decode(errors='ignore')}",
                    status_code=status_code,
                )

            raw_message = json.loads(nested_body.decode())
            mapped = map_gmail_message(raw_message, account.uuid, include_headers=include_headers)
            messages[mapped.id] = mapped

        return messages, rate_limited_ids

    def _extract_batch_boundary(self, response_body: bytes) -> bytes:
        match = re.search(rb"--([^\r\n]+)", response_body)
        if not match:
            raise ProviderError("Invalid Gmail batch response: missing multipart boundary")
        return b"--" + match.group(1)

    def _split_headers_body(self, content: bytes) -> tuple[bytes, bytes]:
        separator = b"\r\n\r\n" if b"\r\n\r\n" in content else b"\n\n"
        sections = content.split(separator, maxsplit=1)
        if len(sections) != 2:
            raise ProviderError("Invalid Gmail batch response: malformed MIME part")
        return sections[0], sections[1]

    def _extract_content_id(self, headers: bytes) -> str | None:
        match = re.search(rb"^Content-ID:\s*<([^>]+)>", headers, flags=re.IGNORECASE | re.MULTILINE)
        if not match:
            return None
        return match.group(1).decode()

    def _normalize_batch_content_id(self, content_id: str, valid_message_ids: set[str]) -> str | None:
        candidates = [content_id]
        if content_id.lower().startswith("response-"):
            candidates.append(content_id[len("response-") :])

        for candidate in candidates:
            if candidate in valid_message_ids:
                return candidate
            if "+" in candidate:
                candidate_prefix = candidate.split("+", maxsplit=1)[0]
                if candidate_prefix in valid_message_ids:
                    return candidate_prefix

        return None

    def _parse_nested_http_response(self, nested_http: bytes) -> tuple[int, bytes]:
        status_line, rest = self._split_first_line(nested_http)
        tokens = status_line.split()
        if len(tokens) < 2:
            raise ProviderError(f"Invalid Gmail batch response: bad status line {status_line.decode(errors='ignore')}")
        status_code = int(tokens[1])
        _, body = self._split_headers_body(rest)
        return status_code, body.strip()

    def _split_first_line(self, content: bytes) -> tuple[bytes, bytes]:
        separator = b"\r\n" if b"\r\n" in content else b"\n"
        sections = content.split(separator, maxsplit=1)
        if len(sections) != 2:
            raise ProviderError("Invalid Gmail batch response: missing HTTP status line")
        return sections[0], sections[1]

    async def _fetch_messages_individually(
        self, account: Account, message_ids: list[str], include_headers: bool
    ) -> dict[str, Message]:
        semaphore = asyncio.Semaphore(FALLBACK_FETCH_CONCURRENCY)

        async def fetch(message_id: str) -> Message | None:
            async with semaphore:
                try:
                    return await self.get_message(account, message_id, include_headers=include_headers)
                except ProviderNotFoundError:
                    return None

        hydrated = await asyncio.gather(*[fetch(message_id) for message_id in message_ids])
        return {message.id: message for message in hydrated if message is not None}


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
