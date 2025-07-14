"""
SMTP controller for sending emails.
"""

import logging
import smtplib
import uuid
from dataclasses import dataclass
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from typing import Any

from app.api.payloads.messages import (
    AttachmentData,
    EmailAddress,
    MessageAttachment,
    SendMessageData,
)
from app.controllers.email.message import MessageResult, SendMessageResult
from app.controllers.imap.connection import ConnectionManager
from app.controllers.imap.folder_utils import FolderUtils
from app.models.account import Account
from app.utils.message_utils import MessageUtils
from app.utils.password import PasswordUtils


@dataclass
class _SMTPConfig:
    host: str
    port: int


class SMTPException(Exception):
    """Exception raised when an SMTP error occurs."""

    pass


class SMTPInvalidParameterError(Exception):
    """Exception raised when a parameter is invalid."""

    def __init__(self, parameter: str, value: Any) -> None:
        self.parameter = parameter
        self.value = value
        super().__init__(f"Invalid parameter: {parameter} with value: {value}")


class SMTPController:
    """Controller for sending emails via SMTP."""

    def __init__(self, connection_manager: ConnectionManager) -> None:
        self._logger = logging.getLogger(__name__)
        self._connection_manager = connection_manager

    async def send_email(
        self,
        account: Account,
        to: list[EmailAddress],
        subject: str,
        body: str,
        from_: list[EmailAddress] | None = None,
        cc: list[EmailAddress] | None = None,
        bcc: list[EmailAddress] | None = None,
        reply_to: list[EmailAddress] | None = None,
        replied_message: MessageResult | None = None,
        attachments: list[AttachmentData] | None = None,
    ) -> SendMessageResult:
        """
        Send an email via SMTP.

        Args:
            account: The account to send from
            to: List of recipients [{"name": "Name", "email": "email@example.com"}]
            subject: Email subject
            body: The HTML email body
            from_: Optional sender override
            cc: Optional CC recipients
            bcc: Optional BCC recipients
            reply_to: Optional reply-to addresses
            reply_to_message_id: Optional message ID to reply to

        Returns:
            Dictionary containing sent message details
        """
        references: list[str] = []
        if replied_message:
            original_references = MessageUtils.parse_references(replied_message.raw_message)
            formatted_reply_id = MessageUtils.format_message_id(replied_message.message.id)

            if original_references:
                references = original_references + [formatted_reply_id]
            else:
                references = [formatted_reply_id]

        smtp_config = self._get_smtp_config(account)

        message = self._create_message(
            account=account,
            to=to,
            subject=subject,
            body=body,
            from_=from_,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            reply_to_message_id=replied_message.message.id if replied_message else None,
            references=references,
            attachments=attachments,
        )

        message_id = await self._send_smtp_message(account, smtp_config, message, to, cc, bcc)

        try:
            sent_folder = await self._save_to_sent_folder(account, message)
        except Exception as e:
            self._logger.warning(f"Failed to save sent message to Sent folder: {e}")

        attachments_data = []
        if attachments:
            for i, attachment in enumerate(attachments):
                attachments_data.append(
                    MessageAttachment(
                        id=f"att_{i + 1}",
                        filename=attachment.filename,
                        size=len(attachment.data),
                        content_type=attachment.content_type,
                    )
                )

        data = SendMessageData(
            id=message_id,
            subject=subject,
            from_=from_ or [EmailAddress(name=account.email, email=account.email)],
            to=to,
            cc=cc or [],
            bcc=bcc or [],
            reply_to=reply_to or [],
            reply_to_message_id=replied_message.message.id if replied_message else None,
            body=body,
            attachments=attachments_data,
        )

        thread_id = replied_message.message.thread_id if replied_message else message_id
        return SendMessageResult(message=data, message_id=message_id, thread_id=thread_id, folder=sent_folder)

    async def login(self, email: str, password: str, host: str, port: int) -> smtplib.SMTP_SSL | None:
        """Login to the SMTP server."""
        try:
            server = smtplib.SMTP_SSL(host, port)
            response = server.login(email, password)
            if response[0] != 235:
                return None
            return server
        except Exception:
            self._logger.warning("Failed to login to SMTP server", stack_info=True)
            return None

    def _get_smtp_config(self, account: Account) -> _SMTPConfig:
        """Extract SMTP configuration from account."""
        provider_context = account.provider_context

        smtp_host = provider_context.get("smtp_host")
        smtp_port = provider_context.get("smtp_port", 465)

        if not smtp_host or not smtp_port:
            raise ValueError("SMTP host and port are required")

        config = _SMTPConfig(host=smtp_host, port=smtp_port)

        return config

    def _create_message(
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
        references: list[str] | None = None,
        attachments: list[AttachmentData] | None = None,
    ) -> MIMEMultipart:
        """Create email message."""
        # Use "mixed" if there are attachments, otherwise "alternative"
        message_type = "mixed" if attachments else "alternative"
        message = MIMEMultipart(message_type)

        message["Subject"] = subject
        message["Date"] = formatdate(localtime=True)

        # From header
        if from_ and len(from_) > 0:
            sender = from_[0]
            if sender.name:
                message["From"] = f'"{sender.name}" <{sender.email}>'
            else:
                message["From"] = f'"{sender.email}" <{sender.email}>'
        else:
            message["From"] = f'"{account.email}" <{account.email}>'

        # To, Cc, Bcc headers
        if to:
            message["To"] = MessageUtils.format_email_addresses(to)
        if cc:
            message["Cc"] = MessageUtils.format_email_addresses(cc)
        if bcc:
            message["Bcc"] = MessageUtils.format_email_addresses(bcc)

        # Reply-To header
        if reply_to:
            message["Reply-To"] = MessageUtils.format_email_addresses(reply_to)

        # In-Reply-To header and References
        if reply_to_message_id:
            message["In-Reply-To"] = MessageUtils.format_message_id(reply_to_message_id)
        if references:
            message["References"] = " ".join(references)

        # Generate Message-ID
        message_id = f"<{uuid.uuid4()}@{account.email.split('@')[1]}>"
        message["Message-ID"] = message_id

        # Body
        if attachments:
            # When there are attachments, create a separate container for text content
            text_container = MIMEMultipart("alternative")
            html_part = MIMEText(body, "html", "utf-8")
            text_container.attach(html_part)
            message.attach(text_container)
        else:
            # No attachments, attach HTML directly
            html_part = MIMEText(body, "html", "utf-8")
            message.attach(html_part)

        if attachments:
            for attachment in attachments:
                attachment_part = MIMEApplication(attachment.data, name=attachment.filename)
                attachment_part.add_header("Content-Disposition", f"attachment; filename={attachment.filename}")
                if attachment.content_type:
                    attachment_part.set_type(attachment.content_type)
                message.attach(attachment_part)

        return message

    async def _send_smtp_message(
        self,
        account: Account,
        smtp_config: _SMTPConfig,
        message: MIMEMultipart,
        to: list[EmailAddress],
        cc: list[EmailAddress] | None = None,
        bcc: list[EmailAddress] | None = None,
    ) -> str:
        """Send message via SMTP."""
        try:
            server = await self.login(
                account.email,
                password=PasswordUtils.decrypt_password(account.credentials),
                host=smtp_config.host,
                port=smtp_config.port,
            )
            if not server:
                raise SMTPException("Failed to login to SMTP server")

            # Prepare recipient list
            recipients = [addr.email for addr in to]
            if cc:
                recipients.extend([addr.email for addr in cc])
            if bcc:
                recipients.extend([addr.email for addr in bcc])

            text = message.as_string()
            server.sendmail(account.email, recipients, text)
            server.quit()

            # Extract Message-ID from headers (now guaranteed to exist)
            message_id = message["Message-ID"]
            self._logger.info(f"Email sent successfully: {message_id}")
            return message_id

        except Exception as e:
            raise SMTPException(f"Failed to send email: {e}")

    async def _save_to_sent_folder(self, account: Account, message: MIMEMultipart) -> str | None:
        """Save a copy of the sent message to the Sent folder via IMAP."""
        # Common Sent folder names to try
        sent_folder_names = ["Sent", "SENT", "Sent Items", "Sent Mail", "Sent Messages"]

        try:
            # Use FolderUtils to get all folders for the account
            all_folders = await FolderUtils.get_account_folders(self._connection_manager, account)

            # Find the first matching Sent folder
            sent_folder = None
            for folder_name in sent_folder_names:
                if folder_name in all_folders:
                    sent_folder = folder_name
                    break

            if not sent_folder:
                self._logger.warning("No existing sent folder found")
                return None

            connection = await self._connection_manager.get_connection(account)
            if not connection:
                self._logger.warning(f"Could not get IMAP connection for account {account.id} to save to Sent folder")
                return None

            try:
                # IMAP requires CRLF line endings, not just LF
                message_string = message.as_string()
                # Convert LF to CRLF for IMAP
                message_string = message_string.replace("\n", "\r\n")
                await connection.append(message_string.encode("utf-8"), sent_folder, flags="\\Seen")
            finally:
                await self._connection_manager.close_connection(connection, account)

            return sent_folder

        except Exception as e:
            self._logger.error(f"Failed to save message to Sent folder: {e}")
            return None
