import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from app.api.payloads.messages import EmailAddress, Message, MessageAttachment, MessageHeader

logger = logging.getLogger(__name__)

# Graph junk folder display names normalized to the name Nylas exposes, so the
# downstream folder skip-list ("junk") keeps matching.
GRAPH_FOLDER_NAME_OVERRIDES = {
    "junk email": "Junk",
    "junk e-mail": "Junk",
}


def graph_folder_name(display_name: str) -> str:
    return GRAPH_FOLDER_NAME_OVERRIDES.get(display_name.lower(), display_name)


def _parse_recipients(recipients: list[dict[str, Any]] | None) -> list[EmailAddress]:
    result: list[EmailAddress] = []
    for recipient in recipients or []:
        email_address = recipient.get("emailAddress", {})
        address = email_address.get("address")
        if address:
            result.append(EmailAddress(name=email_address.get("name") or address, email=address))
    return result


def _to_epoch(iso_timestamp: str | None) -> int:
    if not iso_timestamp:
        return 0
    try:
        return int(datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00")).timestamp())
    except ValueError:
        logger.warning(f"Unparseable Graph timestamp: {iso_timestamp}")
        return 0


def map_graph_attachment(raw: dict[str, Any]) -> MessageAttachment:
    return MessageAttachment(
        id=raw["id"],
        filename=raw.get("name") or "attachment",
        size=int(raw.get("size", 0)),
        content_type=raw.get("contentType") or "application/octet-stream",
        is_inline=bool(raw.get("isInline", False)),
        content_id=raw.get("contentId"),
        content_disposition="inline" if raw.get("isInline") else "attachment",
    )


def map_graph_message(raw: dict[str, Any], grant_id: UUID, include_headers: bool = False) -> Message:
    body = raw.get("body", {}) or {}
    date = _to_epoch(raw.get("receivedDateTime") or raw.get("sentDateTime"))

    message_headers: list[MessageHeader] | None = None
    if include_headers:
        message_headers = [
            MessageHeader(name=h["name"], value=h["value"])
            for h in raw.get("internetMessageHeaders") or []
            if h.get("name") and "value" in h
        ]
        # Graph omits internetMessageHeaders on some messages; synthesize Message-ID.
        if raw.get("internetMessageId") and not any(h.name.lower() == "message-id" for h in message_headers):
            message_headers.append(MessageHeader(name="Message-ID", value=raw["internetMessageId"]))

    flag_status = (raw.get("flag") or {}).get("flagStatus", "notFlagged")

    return Message(
        id=raw["id"],
        grant_id=str(grant_id),
        object="message",
        thread_id=raw.get("conversationId", raw["id"]),
        subject=raw.get("subject") or "",
        body=body.get("content") or "",
        snippet=raw.get("bodyPreview") or "",
        from_=_parse_recipients([raw["from"]] if raw.get("from") else []),
        to=_parse_recipients(raw.get("toRecipients")),
        cc=_parse_recipients(raw.get("ccRecipients")),
        bcc=_parse_recipients(raw.get("bccRecipients")),
        reply_to=_parse_recipients(raw.get("replyTo")),
        date=date,
        created_at=date,
        unread=not raw.get("isRead", True),
        starred=flag_status == "flagged",
        folders=[raw["parentFolderId"]] if raw.get("parentFolderId") else [],
        attachments=[map_graph_attachment(a) for a in raw.get("attachments") or []],
        headers=message_headers,
    )
