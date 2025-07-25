import logging
import time
from email.message import Message as PythonEmailMessage
from email.utils import getaddresses, mktime_tz, parsedate_tz
from uuid import UUID

from app.api.payloads.messages import EmailAddress, Message, MessageAttachment

logger = logging.getLogger(__name__)


class MessageUtils:
    """Utility class for converting IMAP messages to Nylas Message format."""

    @staticmethod
    def convert_to_nylas_format(msg: PythonEmailMessage, grant_id: UUID, folder: str) -> Message:
        """Convert Python email message to Nylas Message format."""

        # Extract basic headers
        subject = msg.get("Subject") or ""
        message_id = msg.get("Message-ID") or ""
        date_header = msg.get("Date") or ""

        try:
            if date_header:
                date_tuple = parsedate_tz(date_header)
                if date_tuple:
                    timestamp = int(mktime_tz(date_tuple))
                else:
                    timestamp = int(time.time())
            else:
                timestamp = int(time.time())
        except Exception:
            timestamp = int(time.time())

        from_header = msg.get("From")
        from_addresses = MessageUtils.parse_addresses(str(from_header) if from_header else "")  # type: ignore
        to_header = msg.get("To")
        to_addresses = MessageUtils.parse_addresses(str(to_header) if to_header else "")  # type: ignore
        body = MessageUtils.extract_body(msg)
        references = MessageUtils.parse_references(msg)
        snippet = body[:100] + "..." if len(body) > 100 else body  # Create snippet from body (first 100 chars)
        attachments = MessageUtils.extract_attachments(msg)
        folders = [folder]

        return Message(
            starred=False,  # Default to false, could be enhanced with IMAP flags
            unread=True,  # Default to true, could be enhanced with IMAP flags
            folders=folders,
            grant_id=str(grant_id),
            date=timestamp,
            attachments=attachments,
            from_=from_addresses,
            id=message_id,
            object="message",
            snippet=snippet,
            subject=subject,
            thread_id=references[0] if references else message_id,
            to=to_addresses,
            body=body,
        )

    @staticmethod
    def extract_body(msg: PythonEmailMessage) -> str:
        """Extract the body text from an email message."""
        body = ""

        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition", ""))

                # Skip attachments
                if "attachment" in content_disposition:
                    continue

                if content_type == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        if isinstance(payload, bytes):
                            charset = part.get_content_charset() or "utf-8"
                            try:
                                body = payload.decode(charset)
                            except UnicodeDecodeError:
                                body = payload.decode("utf-8", errors="ignore")
                        else:
                            body = str(payload)
                        break
                elif content_type == "text/plain" and not body:
                    # Use plain text as fallback if no HTML.
                    payload = part.get_payload(decode=True)
                    if payload:
                        if isinstance(payload, bytes):
                            charset = part.get_content_charset() or "utf-8"
                            try:
                                body = payload.decode(charset)
                            except UnicodeDecodeError:
                                body = payload.decode("utf-8", errors="ignore")
                        else:
                            body = str(payload)
        else:
            # Single part message
            payload = msg.get_payload(decode=True)
            if payload:
                if isinstance(payload, bytes):
                    charset = msg.get_content_charset() or "utf-8"
                    try:
                        body = payload.decode(charset)
                    except UnicodeDecodeError:
                        body = payload.decode("utf-8", errors="ignore")
                else:
                    body = str(payload)

        return body.strip()

    @staticmethod
    def parse_references(msg: PythonEmailMessage) -> list[str]:
        """
        Parse References and In-Reply-To headers to extract referenced Message-IDs.

        Args:
            msg: The email message to parse

        Returns:
            List of referenced Message-IDs (including angle brackets)
        """
        references: list[str] = []

        references_header = msg.get("References")
        if references_header:
            # References are separated by whitespace
            ref_ids = references_header.split()
            for ref_id in ref_ids:
                ref_id = ref_id.strip()
                if ref_id and ref_id.startswith("<") and ref_id.endswith(">"):
                    references.append(ref_id)

        return references

    @staticmethod
    def parse_addresses(address_string: str) -> list[EmailAddress]:
        """Parse email address string into EmailAddress objects."""
        if not address_string:
            return []

        try:
            addresses = getaddresses([address_string])
            result: list[EmailAddress] = []

            for name, email_addr in addresses:
                if email_addr:
                    result.append(EmailAddress(name=name or email_addr, email=email_addr))

            return result
        except Exception:
            logger.exception(f"Failed to parse addresses '{address_string}'")
            return []

    @staticmethod
    def format_message_id(message_id: str) -> str:
        """Format a message ID to include angle brackets."""
        if not message_id.startswith("<"):
            message_id = f"<{message_id}>"
        if not message_id.endswith(">"):
            message_id = f"{message_id}>"
        return message_id

    @staticmethod
    def format_email_addresses(addresses: list[EmailAddress]) -> str:
        """Format a list of email addresses for email headers."""
        if not addresses:
            return ""

        formatted_addresses = []
        for addr in addresses:
            if addr.name:
                formatted_addresses.append(f'"{addr.name}" <{addr.email}>')
            else:
                formatted_addresses.append(f'"{addr.email}" <{addr.email}>')

        return ", ".join(formatted_addresses)

    @staticmethod
    def extract_attachments(msg: PythonEmailMessage) -> list[MessageAttachment]:
        """Extract attachments from an email message."""
        attachments = []

        try:
            if msg.is_multipart():
                attachment_index = 1
                for part in msg.walk():
                    content_disposition = str(part.get("Content-Disposition", ""))

                    if "attachment" in content_disposition:
                        filename = part.get_filename()
                        if filename:
                            content_type = part.get_content_type()
                            payload = part.get_payload(decode=True)
                            size = len(payload) if payload else 0

                            attachment = MessageAttachment(
                                id=f"att_{attachment_index}",
                                filename=filename,
                                size=size,
                                content_type=content_type,
                                is_inline=False,
                            )
                            attachments.append(attachment)
                            attachment_index += 1

        except Exception:
            logger.exception("Failed to extract attachments")

        return attachments

    @staticmethod
    def extract_attachment_content(msg: PythonEmailMessage, attachment_id: str) -> bytes | None:
        """Extract the content of a specific attachment from an email message."""
        try:
            if msg.is_multipart():
                attachment_index = 1
                for part in msg.walk():
                    content_disposition = str(part.get("Content-Disposition", ""))

                    if "attachment" in content_disposition:
                        filename = part.get_filename()
                        if filename:
                            current_attachment_id = f"att_{attachment_index}"
                            if current_attachment_id == attachment_id:
                                payload = part.get_payload(decode=True)
                                if payload and isinstance(payload, bytes):
                                    return payload
                            attachment_index += 1

        except Exception:
            logger.exception(f"Failed to extract attachment content for {attachment_id}")

        return None
