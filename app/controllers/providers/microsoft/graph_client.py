import base64
import logging
from datetime import UTC, datetime, timedelta
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
from app.controllers.providers.http import AuthorizedHttpClient
from app.controllers.providers.microsoft.mapper import graph_folder_name, map_graph_attachment, map_graph_message
from app.controllers.providers.microsoft.query import (
    build_graph_filter,
    build_graph_search,
    decode_cursor,
    encode_cursor,
)
from app.models.account import Account

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"

IMMUTABLE_ID_HEADER = {"Prefer": 'IdType="ImmutableId"'}

MESSAGE_SELECT_FIELDS = (
    "id,conversationId,subject,bodyPreview,body,from,toRecipients,ccRecipients,bccRecipients,"
    "replyTo,receivedDateTime,sentDateTime,isRead,isDraft,flag,parentFolderId,hasAttachments,internetMessageId"
)
ATTACHMENT_SELECT_FIELDS = "id,name,contentType,size,isInline"

# Attachments above this size use a Graph upload session instead of inline contentBytes.
LARGE_ATTACHMENT_THRESHOLD = 3 * 1024 * 1024
UPLOAD_CHUNK_SIZE = 4 * 1024 * 1024

# Graph caps mail subscriptions at 10080 minutes (7 days); renew comfortably earlier.
SUBSCRIPTION_LIFETIME = timedelta(days=6)


class GraphClient(ProviderClient):
    """Microsoft Graph implementation of the provider interface."""

    def __init__(self, http_client: AuthorizedHttpClient) -> None:
        self._http = http_client

    def _message_select(self, include_headers: bool) -> str:
        if include_headers:
            return f"{MESSAGE_SELECT_FIELDS},internetMessageHeaders"
        return MESSAGE_SELECT_FIELDS

    async def get_message(self, account: Account, message_id: str, include_headers: bool = False) -> Message | None:
        try:
            raw = await self._http.request(
                account,
                "GET",
                f"{GRAPH_API_BASE}/me/messages/{message_id}",
                params={
                    "$select": self._message_select(include_headers),
                    "$expand": f"attachments($select={ATTACHMENT_SELECT_FIELDS})",
                },
                headers=IMMUTABLE_ID_HEADER,
            )
        except ProviderNotFoundError:
            return None
        return map_graph_message(raw, account.uuid, include_headers=include_headers)

    async def list_messages(self, account: Account, params: ListMessagesParams) -> ListMessagesResult:
        if params.page_token:
            # Cursor is a full @odata.nextLink URL.
            response = await self._http.request(
                account, "GET", decode_cursor(params.page_token), headers=IMMUTABLE_ID_HEADER
            )
            return self._build_list_result(account, response, params)

        query: dict[str, Any] = {
            "$top": min(params.limit, 100),
            "$select": self._message_select(params.include_headers),
            "$expand": f"attachments($select={ATTACHMENT_SELECT_FIELDS})",
        }

        search = build_graph_search(params)
        if search:
            # $search cannot be combined with $filter/$orderby.
            query["$search"] = f'"{search}"'
        else:
            graph_filter = build_graph_filter(params)
            if graph_filter:
                query["$filter"] = graph_filter
            if not params.search_query_native:
                # $orderby properties must also appear in $filter; receivedDateTime is
                # guaranteed present via the isDraft/receivedDateTime clauses.
                if params.received_after is not None or params.received_before is not None:
                    query["$orderby"] = "receivedDateTime desc"

        response = await self._http.request(
            account, "GET", f"{GRAPH_API_BASE}/me/messages", params=query, headers=IMMUTABLE_ID_HEADER
        )
        return self._build_list_result(account, response, params)

    def _build_list_result(
        self, account: Account, response: dict[str, Any], params: ListMessagesParams
    ) -> ListMessagesResult:
        messages = [
            map_graph_message(raw, account.uuid, include_headers=params.include_headers)
            for raw in response.get("value", [])
            if not raw.get("isDraft", False)
        ]
        next_link = response.get("@odata.nextLink")
        return ListMessagesResult(messages=messages, next_cursor=encode_cursor(next_link) if next_link else None)

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
        if reply_to_message_id:
            draft = await self._http.request(
                account,
                "POST",
                f"{GRAPH_API_BASE}/me/messages/{reply_to_message_id}/createReply",
                json_body={},
                headers=IMMUTABLE_ID_HEADER,
            )
            patch_body = self._build_message_body(to, subject, body, cc, bcc, reply_to)
            draft = await self._http.request(
                account,
                "PATCH",
                f"{GRAPH_API_BASE}/me/messages/{draft['id']}",
                json_body=patch_body,
                headers=IMMUTABLE_ID_HEADER,
            )
        else:
            draft = await self._http.request(
                account,
                "POST",
                f"{GRAPH_API_BASE}/me/messages",
                json_body=self._build_message_body(to, subject, body, cc, bcc, reply_to),
                headers=IMMUTABLE_ID_HEADER,
            )

        draft_id = draft["id"]
        for attachment in attachments or []:
            await self._add_attachment(account, draft_id, attachment)

        await self._http.request(
            account,
            "POST",
            f"{GRAPH_API_BASE}/me/messages/{draft_id}/send",
            headers=IMMUTABLE_ID_HEADER,
            expect_json=False,
        )
        return ProviderSendResult(message_id=draft_id, thread_id=draft.get("conversationId", draft_id))

    def _build_message_body(
        self,
        to: list[EmailAddress],
        subject: str,
        body: str,
        cc: list[EmailAddress] | None,
        bcc: list[EmailAddress] | None,
        reply_to: list[EmailAddress] | None,
    ) -> dict[str, Any]:
        def recipients(addresses: list[EmailAddress] | None) -> list[dict[str, Any]]:
            return [
                {"emailAddress": {"address": address.email, "name": address.name or address.email}}
                for address in addresses or []
            ]

        message: dict[str, Any] = {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body},
            "toRecipients": recipients(to),
        }
        if cc:
            message["ccRecipients"] = recipients(cc)
        if bcc:
            message["bccRecipients"] = recipients(bcc)
        if reply_to:
            message["replyTo"] = recipients(reply_to)
        return message

    async def _add_attachment(self, account: Account, draft_id: str, attachment: AttachmentData) -> None:
        if len(attachment.data) <= LARGE_ATTACHMENT_THRESHOLD:
            await self._http.request(
                account,
                "POST",
                f"{GRAPH_API_BASE}/me/messages/{draft_id}/attachments",
                json_body={
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": attachment.filename,
                    "contentType": attachment.content_type,
                    "contentBytes": base64.b64encode(attachment.data).decode(),
                },
                headers=IMMUTABLE_ID_HEADER,
            )
            return

        session = await self._http.request(
            account,
            "POST",
            f"{GRAPH_API_BASE}/me/messages/{draft_id}/attachments/createUploadSession",
            json_body={
                "AttachmentItem": {
                    "attachmentType": "file",
                    "name": attachment.filename,
                    "contentType": attachment.content_type,
                    "size": len(attachment.data),
                }
            },
            headers=IMMUTABLE_ID_HEADER,
        )
        upload_url = session.get("uploadUrl")
        if not upload_url:
            raise ProviderError(f"Graph upload session missing uploadUrl: {session}")

        total = len(attachment.data)
        offset = 0
        while offset < total:
            chunk = attachment.data[offset : offset + UPLOAD_CHUNK_SIZE]
            end = offset + len(chunk) - 1
            await self._http.request(
                account,
                "PUT",
                upload_url,
                data=chunk,
                headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end}/{total}",
                },
                expect_json=False,
            )
            offset += len(chunk)

    async def get_attachment_metadata(
        self, account: Account, message_id: str, attachment_id: str
    ) -> MessageAttachment | None:
        try:
            raw = await self._http.request(
                account,
                "GET",
                f"{GRAPH_API_BASE}/me/messages/{message_id}/attachments/{attachment_id}",
                params={"$select": ATTACHMENT_SELECT_FIELDS},
                headers=IMMUTABLE_ID_HEADER,
            )
        except ProviderNotFoundError:
            return None
        return map_graph_attachment(raw)

    async def download_attachment(
        self, account: Account, message_id: str, attachment_id: str
    ) -> AttachmentContent | None:
        try:
            raw = await self._http.request(
                account,
                "GET",
                f"{GRAPH_API_BASE}/me/messages/{message_id}/attachments/{attachment_id}",
                headers=IMMUTABLE_ID_HEADER,
            )
        except ProviderNotFoundError:
            return None
        content_bytes = raw.get("contentBytes")
        if content_bytes is None:
            # Non-file attachments (item/reference) have no binary payload.
            return None
        return AttachmentContent(
            data=base64.b64decode(content_bytes),
            content_type=raw.get("contentType") or "application/octet-stream",
            filename=raw.get("name") or "attachment",
        )

    async def get_folder(self, account: Account, folder_id: str) -> FolderData | None:
        try:
            folder = await self._http.request(
                account, "GET", f"{GRAPH_API_BASE}/me/mailFolders/{folder_id}", headers=IMMUTABLE_ID_HEADER
            )
        except ProviderNotFoundError:
            return None
        return FolderData(
            id=folder["id"],
            name=graph_folder_name(folder.get("displayName", folder["id"])),
            total_count=folder.get("totalItemCount"),
            unread_count=folder.get("unreadItemCount"),
        )

    # --- Profile / subscription helpers (used by connect, notifications, and renewal worker) ---

    async def get_profile(self, account: Account) -> dict[str, Any]:
        return dict(
            await self._http.request(
                account, "GET", f"{GRAPH_API_BASE}/me", params={"$select": "mail,userPrincipalName,id"}
            )
        )

    async def create_subscription(self, account: Account, notification_url: str, client_state: str) -> dict[str, Any]:
        expiration = (datetime.now(UTC) + SUBSCRIPTION_LIFETIME).strftime("%Y-%m-%dT%H:%M:%SZ")
        return dict(
            await self._http.request(
                account,
                "POST",
                f"{GRAPH_API_BASE}/subscriptions",
                json_body={
                    "changeType": "created",
                    "notificationUrl": notification_url,
                    "resource": "/me/messages",
                    "expirationDateTime": expiration,
                    "clientState": client_state,
                },
            )
        )

    async def renew_subscription(self, account: Account, subscription_id: str) -> dict[str, Any]:
        expiration = (datetime.now(UTC) + SUBSCRIPTION_LIFETIME).strftime("%Y-%m-%dT%H:%M:%SZ")
        return dict(
            await self._http.request(
                account,
                "PATCH",
                f"{GRAPH_API_BASE}/subscriptions/{subscription_id}",
                json_body={"expirationDateTime": expiration},
            )
        )

    async def delete_subscription(self, account: Account, subscription_id: str) -> None:
        try:
            await self._http.request(
                account, "DELETE", f"{GRAPH_API_BASE}/subscriptions/{subscription_id}", expect_json=False
            )
        except ProviderNotFoundError:
            pass
