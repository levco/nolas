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
    message = MIMEMultipart("mixed") if attachments else MIMEMultipart("alternative")
    message["Subject"] = subject
    message["To"] = MessageUtils.format_email_addresses(to)
    if from_:
        message["From"] = MessageUtils.format_email_addresses(from_)
    if cc:
        message["Cc"] = MessageUtils.format_email_addresses(cc)
    if bcc:
        message["Bcc"] = MessageUtils.format_email_addresses(bcc)
    if reply_to:
        message["Reply-To"] = MessageUtils.format_email_addresses(reply_to)
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references
    message["Date"] = formatdate(localtime=True)

    message_id = make_msgid(domain=sender_domain)
    message["Message-ID"] = message_id

    html_part = MIMEText(body, "html", "utf-8")
    if attachments:
        alternative = MIMEMultipart("alternative")
        alternative.attach(html_part)
        message.attach(alternative)
        for attachment in attachments:
            maintype, _, subtype = (attachment.content_type or "application/octet-stream").partition("/")
            part = MIMEApplication(attachment.data, _subtype=subtype or "octet-stream")
            part.replace_header("Content-Type", attachment.content_type or "application/octet-stream")
            part.add_header("Content-Disposition", "attachment", filename=attachment.filename)
            message.attach(part)
    else:
        message.attach(html_part)

    return message.as_bytes(), message_id
