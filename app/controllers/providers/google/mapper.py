import base64
import logging
from typing import Any
from uuid import UUID

from app.api.payloads.messages import EmailAddress, Message, MessageAttachment, MessageHeader
from app.utils.message_utils import MessageUtils

logger = logging.getLogger(__name__)

# Friendly names for Gmail system labels, aligned with the names Nylas exposes so
# downstream folder skip-lists keep working.
GMAIL_SYSTEM_LABEL_NAMES = {
    "INBOX": "Inbox",
    "SENT": "Sent",
    "DRAFT": "Drafts",
    "SPAM": "Junk",
    "TRASH": "Deleted",
    "IMPORTANT": "Important",
    "STARRED": "Starred",
    "UNREAD": "Unread",
    "CATEGORY_PERSONAL": "Personal",
    "CATEGORY_SOCIAL": "Social",
    "CATEGORY_PROMOTIONS": "Promotion",
    "CATEGORY_UPDATES": "Updates",
    "CATEGORY_FORUMS": "Forums",
}

# Label ids that are flags rather than folder-like containers.
NON_FOLDER_LABELS = {"UNREAD", "STARRED", "IMPORTANT"}


def gmail_label_name(label_id: str, raw_name: str | None = None) -> str:
    return GMAIL_SYSTEM_LABEL_NAMES.get(label_id, raw_name or label_id)


def decode_base64url(data: str) -> bytes:
    """Gmail base64url payloads may arrive unpadded; normalize before decoding."""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode())


def _decode_body(data: str) -> str:
    try:
        return decode_base64url(data).decode("utf-8", errors="ignore")
    except Exception:
        logger.exception("Failed to decode Gmail body part")
        return ""


def _walk_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    stack = [payload]
    while stack:
        part = stack.pop()
        parts.append(part)
        stack.extend(part.get("parts", []))
    return parts


def _header(headers: list[dict[str, str]], name: str) -> str | None:
    lowered = name.lower()
    for header in headers:
        if header.get("name", "").lower() == lowered:
            return header.get("value")
    return None


def extract_gmail_body(payload: dict[str, Any]) -> str:
    """Prefer the HTML part; fall back to plain text."""
    html = ""
    text = ""
    for part in _walk_parts(payload):
        mime_type = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if not data or part.get("filename"):
            continue
        if mime_type == "text/html" and not html:
            html = _decode_body(data)
        elif mime_type == "text/plain" and not text:
            text = _decode_body(data)
    return html or text


def extract_gmail_attachments(payload: dict[str, Any]) -> list[MessageAttachment]:
    attachments: list[MessageAttachment] = []
    for part in _walk_parts(payload):
        body = part.get("body", {})
        attachment_id = body.get("attachmentId")
        filename = part.get("filename")
        if not attachment_id:
            continue
        part_headers = part.get("headers", [])
        content_id = _header(part_headers, "Content-ID")
        disposition = (_header(part_headers, "Content-Disposition") or "").split(";")[0].strip().lower() or None
        attachments.append(
            MessageAttachment(
                id=attachment_id,
                filename=filename or (content_id or "attachment").strip("<>"),
                size=int(body.get("size", 0)),
                content_type=part.get("mimeType", "application/octet-stream"),
                is_inline=disposition == "inline" or (content_id is not None and not filename),
                content_id=content_id.strip("<>") if content_id else None,
                content_disposition=disposition,
            )
        )
    return attachments


def map_gmail_message(raw: dict[str, Any], grant_id: UUID, include_headers: bool = False) -> Message:
    payload = raw.get("payload", {})
    headers = payload.get("headers", [])
    label_ids = raw.get("labelIds", [])

    body = extract_gmail_body(payload)
    snippet = raw.get("snippet") or (body[:100] + "..." if len(body) > 100 else body)
    date = int(int(raw.get("internalDate", 0)) / 1000)

    message_headers: list[MessageHeader] | None = None
    if include_headers:
        message_headers = [
            MessageHeader(name=h["name"], value=h["value"]) for h in headers if h.get("name") and "value" in h
        ]

    folders = [label_id for label_id in label_ids if label_id not in NON_FOLDER_LABELS] or label_ids

    return Message(
        id=raw["id"],
        grant_id=str(grant_id),
        object="message",
        thread_id=raw.get("threadId", raw["id"]),
        subject=_header(headers, "Subject") or "",
        body=body,
        snippet=snippet,
        from_=MessageUtils.parse_addresses(_header(headers, "From") or ""),
        to=MessageUtils.parse_addresses(_header(headers, "To") or ""),
        cc=MessageUtils.parse_addresses(_header(headers, "Cc") or ""),
        bcc=MessageUtils.parse_addresses(_header(headers, "Bcc") or ""),
        reply_to=MessageUtils.parse_addresses(_header(headers, "Reply-To") or ""),
        date=date,
        created_at=date,
        unread="UNREAD" in label_ids,
        starred="STARRED" in label_ids,
        folders=folders,
        attachments=extract_gmail_attachments(payload),
        headers=message_headers,
    )


def parse_gmail_address_header(value: str) -> list[EmailAddress]:
    return MessageUtils.parse_addresses(value)
