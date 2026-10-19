import asyncio
import json
import logging
import re
import uuid
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from app.api.payloads.labels import CreateLabelRequest, Label, UpdateLabelRequest
from app.api.payloads.messages import (
    AttachmentData,
    EmailAddress,
    Message,
    MessageAttachment,
)
from app.api.payloads.threads import Thread
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

# Shared backgroundColor and textColor palette:
# https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.labels#Color
GMAIL_LABEL_COLORS = frozenset(
    """
    #000000 #434343 #666666 #999999 #cccccc #efefef #f3f3f3 #ffffff
    #fb4c2f #ffad47 #fad165 #16a766 #43d692 #4a86e8 #a479e2 #f691b3
    #f6c5be #ffe6c7 #fef1d1 #b9e4d0 #c6f3de #c9daf8 #e4d7f5 #fcdee8
    #efa093 #ffd6a2 #fce8b3 #89d3b2 #a0eac9 #a4c2f4 #d0bcf1 #fbc8d9
    #e66550 #ffbc6b #fcda83 #44b984 #68dfa9 #6d9eeb #b694e8 #f7a7c0
    #cc3a21 #eaa041 #f2c960 #149e60 #3dc789 #3c78d8 #8e63ce #e07798
    #ac2b16 #cf8933 #d5ae49 #0b804b #2a9c68 #285bac #653e9b #b65775
    #822111 #a46a21 #aa8831 #076239 #1a764d #1c4587 #41236d #83334c
    #464646 #e7e7e7 #0d3472 #b6cff5 #0d3b44 #98d7e4 #3d188e #e3d7ff
    #711a36 #fbd3e0 #8a1c0a #f2b2a8 #7a2e0b #ffc8af #7a4706 #ffdeb5
    #594c05 #fbe983 #684e07 #fdedc1 #0b4f30 #b3efd3 #04502e #a2dcc1
    #c2c2c2 #4986e7 #2da2bb #b99aff #994a64 #f691b2 #ff7537 #ffad46
    #662e37 #ebdbde #cca6ac #094228 #42d692 #16a765
    """.split()
)

if TYPE_CHECKING:
    from app.controllers.providers.http import AuthorizedHttpClient


class GmailClient(ProviderClient):
    """Gmail API implementation of the provider interface."""

    def __init__(self, http_client: "AuthorizedHttpClient") -> None:
        self._http = http_client

    async def list_labels(self, account: Account) -> list[Label]:
        raw = await self._http.request(account, "GET", f"{GMAIL_API_BASE}/labels")
        labels = [self._map_label(account, label) for label in raw.get("labels", [])]
        # labels.list omits colors; hydrate user labels with bounded concurrency.
        user_labels = [label for label in labels if not label.system_label]
        details: dict[str, Label] = {}
        for start in range(0, len(user_labels), FALLBACK_FETCH_CONCURRENCY):
            batch = user_labels[start : start + FALLBACK_FETCH_CONCURRENCY]
            fetched = await asyncio.gather(*(self.get_label(account, label.id) for label in batch))
            details.update((label.id, label) for label in fetched)
        return [details.get(label.id, label) for label in labels]

    async def get_label(self, account: Account, label_id: str) -> Label:
        raw = await self._http.request(account, "GET", f"{GMAIL_API_BASE}/labels/{quote(label_id, safe='')}")
        return self._map_label(account, raw)

    async def create_label(self, account: Account, request: CreateLabelRequest) -> Label:
        body: dict[str, Any] = {"name": request.name}
        if request.color is not None or request.text_color is not None:
            body["color"] = self._label_color(request.color, request.text_color)
        raw = await self._http.request(account, "POST", f"{GMAIL_API_BASE}/labels", json_body=body)
        return self._map_label(account, raw)

    async def update_label(self, account: Account, label_id: str, request: UpdateLabelRequest) -> Label:
        current = await self.get_label(account, label_id)
        if current.system_label:
            raise ProviderError("Gmail system labels cannot be edited.", status_code=400)
        body: dict[str, Any] = {}
        if request.name is not None:
            body["name"] = request.name
        color_fields = request.model_fields_set & {"color", "text_color"}
        if color_fields:
            if any(getattr(request, key) is None for key in color_fields):
                raise ProviderError("Gmail label colors cannot be cleared; specify a palette color.", status_code=400)
            body["color"] = self._label_color(
                request.color if "color" in color_fields else current.color,
                request.text_color if "text_color" in color_fields else current.text_color,
            )
        raw = await self._http.request(
            account, "PATCH", f"{GMAIL_API_BASE}/labels/{quote(label_id, safe='')}", json_body=body
        )
        return self._map_label(account, raw)

    async def delete_label(self, account: Account, label_id: str) -> None:
        current = await self.get_label(account, label_id)
        if current.system_label:
            raise ProviderError("Gmail system labels cannot be deleted.", status_code=400)
        await self._http.request(
            account, "DELETE", f"{GMAIL_API_BASE}/labels/{quote(label_id, safe='')}", expect_json=False
        )

    async def update_message_labels(self, account: Account, message_id: str, labels: list[str]) -> Message:
        message = await self.get_message(account, message_id)
        if message is None:
            raise ProviderNotFoundError()
        target = list(dict.fromkeys(labels))
        add = [label for label in target if label not in message.labels]
        remove = [label for label in message.labels if label not in target]
        if not add and not remove:
            return message
        await self._http.request(
            account,
            "POST",
            f"{GMAIL_API_BASE}/messages/{quote(message_id, safe='')}/modify",
            json_body={"addLabelIds": add, "removeLabelIds": remove},
        )
        message = await self.get_message(account, message_id)
        if message is None:
            raise ProviderNotFoundError()
        return message

    async def get_message(self, account: Account, message_id: str, include_headers: bool = False) -> Message | None:
        try:
            raw = await self._http.request(
                account, "GET", f"{GMAIL_API_BASE}/messages/{quote(message_id, safe='')}", params={"format": "full"}
            )
        except ProviderNotFoundError:
            return None
        return map_gmail_message(raw, account.uuid, include_headers=include_headers)

    async def update_message_unread(self, account: Account, message_id: str, unread: bool) -> Message | None:
        message = await self.get_message(account, message_id)
        if message is None:
            return None

        if unread:
            await self._http.request(
                account,
                "POST",
                f"{GMAIL_API_BASE}/messages/{message_id}/modify",
                json_body={"addLabelIds": ["UNREAD"]},
            )
        else:
            # Gmail exposes a native thread mutation, so clearing UNREAD does
            # not require fetching and updating every sibling individually.
            await self._http.request(
                account,
                "POST",
                f"{GMAIL_API_BASE}/threads/{message.thread_id}/modify",
                json_body={"removeLabelIds": ["UNREAD"]},
            )

        message.unread = unread
        message.labels = [label for label in message.labels if label != "UNREAD"]
        if unread:
            message.labels.append("UNREAD")
        return message

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

    async def get_thread(self, account: Account, thread_id: str) -> Thread | None:
        try:
            thread = await self._http.request(
                account, "GET", f"{GMAIL_API_BASE}/threads/{thread_id}", params={"format": "full"}
            )
        except ProviderNotFoundError:
            return None

        messages = [
            map_gmail_message(raw, account.uuid, include_headers=False)
            for raw in thread.get("messages", [])
            if "DRAFT" not in raw.get("labelIds", [])
        ]
        if not messages:
            return None
        threads = build_threads_from_messages(messages)
        return threads[0] if threads else None

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
        labels: list[str] | None = None,
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

        try:
            response = await self._http.request(
                account,
                "POST",
                f"{GMAIL_UPLOAD_BASE}/messages/send",
                params={"uploadType": "multipart"},
                data=multipart_body,
                headers={"Content-Type": f"multipart/related; boundary={boundary}"},
            )
        except ProviderError as e:
            logger.exception(f"Gmail send failed: {e}")
            raise e

        message_id = response.get("id")
        if not message_id:
            raise ProviderError(f"Gmail send did not return a message id: {response}")
        assigned_labels = list(response.get("labelIds") or [])
        if labels:
            try:
                labeled = await self._http.request(
                    account,
                    "POST",
                    f"{GMAIL_API_BASE}/messages/{quote(message_id, safe='')}/modify",
                    json_body={"addLabelIds": list(dict.fromkeys(labels))},
                )
                assigned_labels = list(labeled.get("labelIds") or [])
            except Exception:
                # Sending succeeded; reporting failure here could cause the caller to send a duplicate.
                logger.exception("Email sent but could not apply labels; message_id=%s", message_id)
        return ProviderSendResult(
            message_id=message_id, thread_id=response.get("threadId", message_id), labels=assigned_labels
        )

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
        # Do not restrict historyTypes here: message.updated notifications depend
        # on labelAdded/labelRemoved records (including Gmail's UNREAD label).
        params: dict[str, Any] = {"startHistoryId": start_history_id}
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

    def _label_color(self, color: str | None, text_color: str | None) -> dict[str, str]:
        background = "#ffffff" if color is None else color.lower()
        text = "#000000" if text_color is None else text_color.lower()
        if background not in GMAIL_LABEL_COLORS or text not in GMAIL_LABEL_COLORS:
            raise ProviderError("Gmail label colors must be #RRGGBB values from Gmail's palette.", status_code=400)
        return {"backgroundColor": background, "textColor": text}

    def _map_label(self, account: Account, raw: dict[str, Any]) -> Label:
        color = raw.get("color") or {}
        return Label(
            id=raw["id"],
            grant_id=str(account.uuid),
            name=gmail_label_name(raw["id"], raw.get("name")),
            color=color.get("backgroundColor"),
            text_color=color.get("textColor"),
            system_label=raw.get("type") == "system",
        )

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
