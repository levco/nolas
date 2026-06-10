import re
from typing import Any

from app.api.payloads.messages import Message

BOUNCE_SENDER_PATTERN = re.compile(r"(mailer-daemon|postmaster)@", re.IGNORECASE)
BOUNCE_SUBJECT_PATTERN = re.compile(
    r"undeliverable|undelivered|delivery status notification|mail delivery failed|failure notice|returned mail",
    re.IGNORECASE,
)
SMTP_CODE_PATTERN = re.compile(r"\b([45]\d{2})[ -]")
EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def detect_bounce(message: Message) -> dict[str, Any] | None:
    """Best-effort DSN detection. Returns a Nylas message.bounce_detected object or None."""
    sender = message.from_[0].email if message.from_ else ""
    is_bounce_sender = bool(BOUNCE_SENDER_PATTERN.search(sender))
    is_bounce_subject = bool(BOUNCE_SUBJECT_PATTERN.search(message.subject or ""))
    if not (is_bounce_sender or is_bounce_subject):
        return None
    if not is_bounce_sender and not is_bounce_subject:
        return None

    body_text = re.sub(r"<[^>]+>", " ", message.body or "")[:5000]
    code_match = SMTP_CODE_PATTERN.search(body_text)

    own_address = {address.email.lower() for address in message.to}
    bounced = [
        candidate
        for candidate in dict.fromkeys(EMAIL_PATTERN.findall(body_text))
        if candidate.lower() not in own_address and not BOUNCE_SENDER_PATTERN.search(candidate)
    ]
    if not bounced:
        return None

    return {
        "bounce_date": str(message.date),
        "bounce_reason": (message.subject or "Delivery failure")[:255],
        "bounced_addresses": ",".join(bounced[:10]),
        "code": code_match.group(1) if code_match else "",
        "object": "message",
        "origin": {
            "id": message.id,
            "subject": message.subject,
            "to": bounced[:10],
            "type": "message",
        },
    }
