"""
SMTP controller for sending emails.
"""

import asyncio
import html
import logging
import re
import smtplib
import uuid
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from typing import Any

from app.api.models.messages import EmailAddress
from app.api.models.send_messages import SendMessageData
from app.controllers.email.email_controller import EmailController
from app.controllers.imap.connection import ConnectionManager
from app.controllers.imap.folder_utils import FolderUtils
from app.models.account import Account
from app.utils.message_utils import MessageUtils


@dataclass
class _SMTPConfig:
    host: str
    port: int


class SMTPInvalidParameterError(Exception):
    """Exception raised when a parameter is invalid."""

    def __init__(self, parameter: str, value: Any) -> None:
        self.parameter = parameter
        self.value = value
        super().__init__(f"Invalid parameter: {parameter} with value: {value}")


class SMTPController:
    """Controller for sending emails via SMTP."""

    def __init__(self, email_controller: EmailController, connection_manager: ConnectionManager) -> None:
        self._logger = logging.getLogger(__name__)
        self._email_controller = email_controller
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
        reply_to_message_id: str | None = None,
    ) -> SendMessageData:
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
        if reply_to_message_id:
            # Check if the original message exists
            replied_message_result = await self._email_controller.get_message_by_id(account, reply_to_message_id)
            if not replied_message_result:
                raise SMTPInvalidParameterError("reply_to_message_id", reply_to_message_id)
            original_references = MessageUtils.parse_references(replied_message_result.raw_message)
            formatted_reply_id = MessageUtils.format_message_id(reply_to_message_id)

            if original_references:
                references = original_references + [formatted_reply_id]
            else:
                references = [formatted_reply_id]

        # Extract SMTP configuration from account
        smtp_config = self._get_smtp_config(account)

        # Create message (the formatted reply ID is already handled above)
        message = self._create_message(
            account=account,
            to=to,
            subject=subject,
            body=body,
            from_=from_,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            reply_to_message_id=reply_to_message_id,
            references=references,
        )

        message_id = await self._send_smtp_message(account, smtp_config, message, to, cc, bcc)

        # Small delay to avoid synchronization issues
        await asyncio.sleep(0.5)

        # Save a copy to the Sent folder via IMAP
        try:
            await self._save_to_sent_folder(account, message)
        except Exception as e:
            self._logger.warning(f"Failed to save sent message to Sent folder: {e}")

        return SendMessageData(
            id=message_id,
            subject=subject,
            from_=from_ or [EmailAddress(name=account.email, email=account.email)],
            to=to,
            cc=cc or [],
            bcc=bcc or [],
            reply_to=reply_to or [],
            reply_to_message_id=reply_to_message_id,
            body=body,
            attachments=[],
        )

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
    ) -> MIMEMultipart:
        """Create email message."""
        message = MIMEMultipart("alternative")

        # Set headers naturally
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

        # To header
        to_addresses = []
        for addr in to:
            if addr.name:
                to_addresses.append(f'"{addr.name}" <{addr.email}>')
            else:
                to_addresses.append(f'"{addr.email}" <{addr.email}>')
        message["To"] = ", ".join(to_addresses)

        # Cc header
        if cc and len(cc) > 0:
            cc_addresses = []
            for addr in cc:
                if addr.name:
                    cc_addresses.append(f'"{addr.name}" <{addr.email}>')
                else:
                    cc_addresses.append(f'"{addr.email}" <{addr.email}>')
            message["Cc"] = ", ".join(cc_addresses)

        # Bcc header
        if bcc and len(bcc) > 0:
            bcc_addresses = []
            for addr in bcc:
                if addr.name:
                    bcc_addresses.append(f'"{addr.name}" <{addr.email}>')
                else:
                    bcc_addresses.append(f'"{addr.email}" <{addr.email}>')
            message["Bcc"] = ", ".join(bcc_addresses)

        # Reply-To header
        if reply_to and len(reply_to) > 0:
            reply_to_addresses = []
            for addr in reply_to:
                if addr.name:
                    reply_to_addresses.append(f'"{addr.name}" <{addr.email}>')
                else:
                    reply_to_addresses.append(f'"{addr.email}" <{addr.email}>')
            message["Reply-To"] = ", ".join(reply_to_addresses)

        # In-Reply-To header and References
        if reply_to_message_id:
            message["In-Reply-To"] = MessageUtils.format_message_id(reply_to_message_id)
        if references:
            message["References"] = " ".join(references)

        # Generate Message-ID
        message_id = f"<{uuid.uuid4()}@{account.email.split('@')[1]}>"
        message["Message-ID"] = message_id

        # Body
        html_part = MIMEText(body, "html", "utf-8")
        message.attach(html_part)

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
            # Create SMTP connection
            server = smtplib.SMTP_SSL(smtp_config.host, smtp_config.port)

            # Authenticate
            server.login(account.email, account.credentials)

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
            self._logger.error(f"SMTP send failed: {e}")
            raise Exception(f"Failed to send email: {str(e)}")

    async def _save_to_sent_folder(self, account: Account, message: MIMEMultipart) -> None:
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
                return

            connection = await self._connection_manager.get_connection(account)
            if not connection:
                self._logger.warning(f"Could not get IMAP connection for account {account.id} to save to Sent folder")
                return

            try:
                # IMAP requires CRLF line endings, not just LF
                message_string = message.as_string()
                # Convert LF to CRLF for IMAP
                message_string = message_string.replace("\n", "\r\n")
                await connection.append(message_string.encode("utf-8"), sent_folder)
                self._logger.debug(f"Successfully saved message to {sent_folder} folder")
            finally:
                await self._connection_manager.close_connection(connection, account)

        except Exception as e:
            self._logger.error(f"Failed to save message to Sent folder: {e}")
