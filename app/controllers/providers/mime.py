from email import policy
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

from app.api.payloads.messages import AttachmentData, EmailAddress
from app.utils.message_utils import MessageUtils


def build_mime_message(
    to: list[EmailAddress],
    subject: str,
    body: str,
    from_: list[EmailAddress] | None = None,
    cc: list[EmailAddress] | None = None,
    bcc: list[EmailAddress] | None = None,
    reply_to: list[EmailAddress] | None = None,
    attachments: list[AttachmentData] | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    sender_domain: str | None = None,
) -> tuple[bytes, str]:
    """Build an RFC822 message. Returns (raw_bytes, message_id_header)."""
    message, message_id = build_email_message(
        to=to,
        subject=subject,
        body=body,
        from_=from_,
        cc=cc,
        bcc=bcc,
        reply_to=reply_to,
        attachments=attachments,
        in_reply_to=in_reply_to,
        references=references,
        sender_domain=sender_domain,
    )
    return message.as_bytes(), message_id


def build_email_message(
    to: list[EmailAddress],
    subject: str,
    body: str,
    from_: list[EmailAddress] | None = None,
    cc: list[EmailAddress] | None = None,
    bcc: list[EmailAddress] | None = None,
    reply_to: list[EmailAddress] | None = None,
    attachments: list[AttachmentData] | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    sender_domain: str | None = None,
) -> tuple[MIMEMultipart, str]:
    """Build an RFC822 email message. Returns (message, message_id_header)."""
    container_subtype = "mixed" if attachments else "alternative"
    message = MIMEMultipart(container_subtype, policy=policy.default)
    message["Subject"] = subject
    # Address objects, not str: policy.default's EmailPolicy folds/encodes them per RFC 2047,
    # but the Message.__setitem__ stub is typed str-only.
    message["To"] = MessageUtils.to_header_addresses(to)  # type: ignore[assignment]
    if from_:
        message["From"] = MessageUtils.to_header_addresses(from_)  # type: ignore[assignment]
    if cc:
        message["Cc"] = MessageUtils.to_header_addresses(cc)  # type: ignore[assignment]
    if bcc:
        message["Bcc"] = MessageUtils.to_header_addresses(bcc)  # type: ignore[assignment]
    if reply_to:
        message["Reply-To"] = MessageUtils.to_header_addresses(reply_to)  # type: ignore[assignment]
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references
    message["Date"] = formatdate(localtime=True)

    message_id = make_msgid(domain=sender_domain)
    message["Message-ID"] = message_id

    html_part = MIMEText(body, "html", "utf-8", policy=policy.default)  # type: ignore[arg-type]
    if attachments:
        alternative = MIMEMultipart("alternative", policy=policy.default)
        alternative.attach(html_part)
        message.attach(alternative)
        for attachment in attachments:
            _, _, subtype = (attachment.content_type or "application/octet-stream").partition("/")
            part = MIMEApplication(
                attachment.data, _subtype=subtype or "octet-stream", policy=policy.default  # type: ignore[arg-type]
            )
            part.replace_header("Content-Type", attachment.content_type or "application/octet-stream")
            part.add_header("Content-Disposition", "attachment", filename=attachment.filename)
            message.attach(part)
    else:
        message.attach(html_part)

    return message, message_id
