import logging
from datetime import UTC, datetime

from app.api.payloads.messages import AttachmentData, EmailAddress, Message, MessageAttachment
from app.api.payloads.threads import Thread
from app.controllers.email.email_controller import EmailController
from app.controllers.imap.connection import ConnectionManager
from app.controllers.imap.folder_utils import FolderUtils
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
from app.controllers.providers.threads import build_threads_from_messages, filter_threads
from app.models.account import Account
from app.utils.message_utils import MessageUtils

logger = logging.getLogger(__name__)

# Hard cap on how many UIDs we hydrate per folder when listing over IMAP.
MAX_UIDS_PER_FOLDER = 200


class ImapProviderAdapter(ProviderClient):
    """Adapts the existing IMAP controllers to the provider interface.

    get_message/send/attachments delegate to the pre-existing IMAP code paths.
    list_messages implements server-side IMAP SEARCH across folders (previously a stub).
    """

    def __init__(self, email_controller: EmailController, connection_manager: ConnectionManager) -> None:
        self._email_controller = email_controller
        self._connection_manager = connection_manager

    async def get_message(self, account: Account, message_id: str, include_headers: bool = False) -> Message | None:
        result = await self._email_controller.get_message_by_id(account, message_id)
        return result.message if result else None

    async def list_messages(self, account: Account, params: ListMessagesParams) -> ListMessagesResult:
        criteria = self._build_search_criteria(params)
        messages: list[Message] = []
        seen_ids: set[str] = set()

        folders = await FolderUtils.get_account_folders(self._connection_manager, account)
        for folder in folders:
            if len(messages) >= params.limit:
                break
            connection = None
            try:
                connection = await self._connection_manager.get_connection_or_fail(account, folder)
                uids: list[str] = []
                for criterion in criteria:
                    result = await connection.search(criterion)  # type: ignore[attr-defined]
                    if result and result[1] and result[1][0]:
                        raw = result[1][0]
                        decoded = raw.decode() if isinstance(raw, bytes) else str(raw)
                        uids.extend(uid for uid in decoded.split() if uid.isdigit())
                # Newest first, dedupe, cap.
                unique_uids = sorted({int(uid) for uid in uids}, reverse=True)[:MAX_UIDS_PER_FOLDER]
                for uid in unique_uids:
                    if len(messages) >= params.limit:
                        break
                    fetch_result = await connection.fetch(str(uid), "(RFC822)")  # type: ignore[attr-defined]
                    raw_message = self._extract_raw(fetch_result)
                    if raw_message is None:
                        continue
                    import email as email_lib

                    parsed = email_lib.message_from_bytes(raw_message)
                    message = MessageUtils.convert_to_nylas_format(parsed, account.uuid, folder)
                    if message.id in seen_ids:
                        continue
                    if not self._matches(message, params):
                        continue
                    seen_ids.add(message.id)
                    messages.append(message)
            except Exception:
                logger.exception(f"IMAP list_messages failed for {account.email}:{folder}")
            finally:
                if connection:
                    try:
                        await self._connection_manager.close_connection(connection, account)
                    except Exception:
                        pass

        messages.sort(key=lambda m: m.date, reverse=True)
        return ListMessagesResult(messages=messages[: params.limit])

    async def list_threads(self, account: Account, params: ListThreadsParams) -> ListThreadsResult:
        message_params = ListMessagesParams(
            limit=min(max(params.limit * 5, params.limit), MAX_UIDS_PER_FOLDER),
            in_=params.in_,
            from_=params.from_,
            any_email=params.any_email,
            subject=params.subject,
            received_after=params.latest_message_after,
            received_before=params.latest_message_before,
            search_query_native=params.search_query_native,
        )
        message_result = await self.list_messages(account, message_params)
        threads = build_threads_from_messages(message_result.messages)
        filtered = filter_threads(threads, message_result.messages, params)
        return ListThreadsResult(threads=filtered[: params.limit], next_cursor=None)

    async def get_thread(self, account: Account, thread_id: str) -> Thread | None:
        message_params = ListMessagesParams(thread_id=thread_id, limit=MAX_UIDS_PER_FOLDER)
        message_result = await self.list_messages(account, message_params)
        if not message_result.messages:
            return None
        threads = build_threads_from_messages(message_result.messages)
        return threads[0] if threads else None

    def _build_search_criteria(self, params: ListMessagesParams) -> list[str]:
        """Each entry is an independent IMAP SEARCH whose results are unioned."""
        base: list[str] = []
        if params.received_after is not None:
            base.append(f"SINCE {datetime.fromtimestamp(params.received_after, tz=UTC).strftime('%d-%b-%Y')}")
        if params.received_before is not None:
            base.append(f"BEFORE {datetime.fromtimestamp(params.received_before, tz=UTC).strftime('%d-%b-%Y')}")
        if params.subject:
            escaped = params.subject.replace('"', '\\"')
            base.append(f'SUBJECT "{escaped}"')
        base_criteria = " ".join(base)

        criteria: list[str] = []
        if params.thread_id:
            thread_id = MessageUtils.format_message_id(params.thread_id)
            criteria.append(f'{base_criteria} HEADER References "{thread_id}"'.strip())
            criteria.append(f'{base_criteria} HEADER Message-ID "{thread_id}"'.strip())
        elif params.any_email:
            for email_address in params.any_email:
                for field in ("FROM", "TO", "CC"):
                    criteria.append(f'{base_criteria} {field} "{email_address}"'.strip())
        elif params.from_:
            criteria.append(f'{base_criteria} FROM "{params.from_}"'.strip())
        else:
            criteria.append(base_criteria or "ALL")
        return criteria

    def _matches(self, message: Message, params: ListMessagesParams) -> bool:
        if params.received_after is not None and message.date < params.received_after:
            return False
        if params.received_before is not None and message.date > params.received_before:
            return False
        return True

    def _extract_raw(self, fetch_result: object) -> bytes | None:
        if not fetch_result or not fetch_result[1]:  # type: ignore[index]
            return None
        for item in fetch_result[1]:  # type: ignore[index]
            if isinstance(item, (bytes, bytearray)) and len(item) > 100:
                return bytes(item)
            if isinstance(item, list):
                for nested in item:
                    if isinstance(nested, (bytes, bytearray)) and len(nested) > 100:
                        return bytes(nested)
        return None

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
        result = await self._email_controller.send_email(
            account=account,
            to=to,
            subject=subject,
            body=body,
            from_=from_,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            reply_to_message_id=reply_to_message_id,
            attachments=attachments,
        )
        return ProviderSendResult(message_id=result.message_id, thread_id=result.thread_id)

    async def get_attachment_metadata(
        self, account: Account, message_id: str, attachment_id: str
    ) -> MessageAttachment | None:
        result = await self._email_controller.get_message_by_id(account, message_id)
        if result is None or result.message is None:
            return None
        for attachment in result.message.attachments:
            if attachment.id == attachment_id:
                return attachment
        return None

    async def download_attachment(
        self, account: Account, message_id: str, attachment_id: str
    ) -> AttachmentContent | None:
        result = await self._email_controller.get_message_by_id(account, message_id)
        if result is None or result.message is None:
            return None
        attachment = next((a for a in result.message.attachments if a.id == attachment_id), None)
        if attachment is None:
            return None
        content = MessageUtils.extract_attachment_content(result.raw_message, attachment_id)
        if content is None:
            return None
        return AttachmentContent(data=content, content_type=attachment.content_type, filename=attachment.filename)

    async def get_folder(self, account: Account, folder_id: str) -> FolderData | None:
        # IMAP folder ids are the folder names themselves.
        return FolderData(id=folder_id, name=folder_id)
