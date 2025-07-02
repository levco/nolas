import email
import logging
import time
import urllib.parse
from email.message import Message as PythonEmailMessage
from email.utils import getaddresses, mktime_tz, parsedate_tz
from typing import Any

from app.api.models.messages import EmailAddress, Message, MessageAttachment
from app.controllers.imap.connection import ConnectionManager
from app.controllers.imap.folder_utils import FolderUtils
from app.controllers.imap.models import AccountConfig
from app.models.account import Account


class MessageController:
    """Controller for fetching email messages from IMAP servers."""

    def __init__(self, connection_manager: ConnectionManager):
        self._logger = logging.getLogger(__name__)
        self._connection_manager = connection_manager

    async def get_message_by_id(self, account: Account, message_id: str) -> Message | None:
        """
        Fetch a message by its Message-ID from IMAP server across all folders.

        Args:
            account: The account to search in
            message_id: The Message-ID to search for (e.g., '<abc123@domain.com>')

        Returns:
            Message object in Nylas format or None if not found
        """
        account_config = self._account_to_config(account)

        try:
            # Decode and format the message ID
            search_message_id = self._decode_message_id(message_id)

            # Get list of all folders using shared utility
            folders = await FolderUtils.get_account_folders(self._connection_manager, account_config)

            self._logger.info(f"Searching for message ID: {search_message_id} in {len(folders)} folders")

            for folder in folders:
                connection = None
                try:
                    # Get connection for this folder
                    connection = await self._connection_manager.get_connection(account_config, folder)

                    # Search for the message in this folder
                    uid = await self._search_message_in_folder(connection, search_message_id, folder)

                    if uid:
                        # Fetch the message
                        nylas_message = await self._fetch_message_from_folder(connection, uid, account, folder)

                        if nylas_message:
                            # Release connection back to pool
                            await self._connection_manager.release_connection(connection, account_config)
                            self._logger.info(
                                f"Successfully retrieved message {search_message_id} from folder {folder}"
                            )
                            return nylas_message

                except Exception as folder_error:
                    self._logger.exception(
                        f"Error searching folder {folder} for message {search_message_id}: {folder_error}"
                    )
                    continue
                finally:
                    if connection:
                        try:
                            await self._connection_manager.release_connection(connection, account_config)
                        except Exception:
                            pass

            self._logger.info(f"Message with ID {search_message_id} not found in any of {len(folders)} folders")
            return None

        except Exception:
            self._logger.exception(f"Error fetching message {message_id} for account {account.email}")
            return None

    def _account_to_config(self, account: Account) -> AccountConfig:
        """Convert database Account model to AccountConfig for IMAP operations."""
        return AccountConfig(
            id=account.id,
            email=account.email,
            credentials=account.credentials,
            provider=account.provider.value,
            provider_context=account.provider_context,
            webhook_url=account.provider_context.get("webhook_url", ""),
        )

    def _decode_message_id(self, message_id: str) -> str:
        """
        Decode URL-encoded message ID and ensure proper format with angle brackets.

        Args:
            message_id: The Message-ID to decode and format

        Returns:
            Properly formatted Message-ID with angle brackets
        """
        decoded_message_id = urllib.parse.unquote(message_id)
        return decoded_message_id if decoded_message_id.startswith("<") else f"<{decoded_message_id}>"

    async def _search_message_in_folder(self, connection: Any, search_message_id: str, folder: str) -> str | None:
        """
        Search for a message by Message-ID in a specific folder.

        Args:
            connection: IMAP connection object
            search_message_id: The Message-ID to search for (with angle brackets)
            folder: The folder name being searched

        Returns:
            UID of the message if found, None otherwise
        """
        try:
            search_criteria = f'HEADER Message-ID "{search_message_id}"'
            result = await connection.search(search_criteria)

            if result and result[1] and result[1][0]:
                # Parse the response
                response_bytes = result[1][0]
                if isinstance(response_bytes, bytes):
                    uids_str = response_bytes.decode().strip()
                else:
                    uids_str = str(response_bytes).strip()

                # Split UIDs and filter out empty strings
                uids = [uid for uid in uids_str.split() if uid.isdigit()]

                if uids:
                    uid = uids[0]
                    self._logger.info(f"Found message {search_message_id} in folder {folder} with UID {uid}")
                    return uid

            return None
        except Exception:
            self._logger.exception(f"Error searching for message {search_message_id} in folder {folder}")
            return None

    def _extract_raw_message_from_fetch_result(self, fetch_result: Any, uid: str, folder: str) -> bytes | None:
        """
        Extract raw message bytes from IMAP fetch result.

        Args:
            fetch_result: The result from IMAP fetch command
            uid: The UID of the message
            folder: The folder name

        Returns:
            Raw message bytes if found, None otherwise
        """
        if not fetch_result or not fetch_result[1]:
            return None

        raw_message = None

        # Handle different possible response structures
        if len(fetch_result[1]) > 0:
            # Try to find the actual message content
            for item in fetch_result[1]:
                if isinstance(item, (bytes, bytearray)) and len(item) > 100:
                    # This is likely the message content
                    raw_message = bytes(item)
                    break
                elif isinstance(item, list) and len(item) > 0:
                    # Check inside nested lists
                    for nested_item in item:
                        if isinstance(nested_item, (bytes, bytearray)) and len(nested_item) > 100:
                            raw_message = bytes(nested_item)
                            break
                    if raw_message:
                        break

        if raw_message is None:
            self._logger.warning(
                f"Could not extract message content from fetch result for UID {uid} in folder {folder}"
            )
            self._logger.debug(f"Fetch result structure: {type(fetch_result[1])}, length: {len(fetch_result[1])}")
            if len(fetch_result[1]) > 0:
                self._logger.debug(
                    f"First item type: {type(fetch_result[1][0])}, "
                    f"content: {
                        (fetch_result[1][0][:200] if hasattr(fetch_result[1][0], '__getitem__') else fetch_result[1][0])
                    }"
                )

        return raw_message

    async def _fetch_message_from_folder(
        self, connection: Any, uid: str, account: Account, folder: str
    ) -> Message | None:
        """
        Fetch and parse a message from a folder given its UID.

        Args:
            connection: IMAP connection object
            uid: The UID of the message to fetch
            account: The account object
            folder: The folder name

        Returns:
            Message object in Nylas format or None if fetch fails
        """
        try:
            # Fetch the message
            fetch_result = await connection.fetch(uid, "(RFC822)")

            # Extract raw message bytes
            raw_message = self._extract_raw_message_from_fetch_result(fetch_result, uid, folder)
            if raw_message is None:
                return None
            parsed_message = email.message_from_bytes(raw_message)

            # Convert to Nylas format
            nylas_message = await self._convert_to_nylas_format(parsed_message, uid, str(account.uuid), folder)

            return nylas_message

        except Exception:
            self._logger.exception(f"Error fetching message UID {uid} from folder {folder}")
            return None

    async def _convert_to_nylas_format(self, msg: PythonEmailMessage, uid: str, grant_id: str, folder: str) -> Message:
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
        from_addresses = self._parse_addresses(str(from_header) if from_header else "")  # type: ignore
        to_header = msg.get("To")
        to_addresses = self._parse_addresses(str(to_header) if to_header else "")  # type: ignore
        body = self._extract_body(msg)
        references = self._parse_references(msg)
        snippet = body[:100] + "..." if len(body) > 100 else body  # Create snippet from body (first 100 chars)
        attachments = self._extract_attachments(msg, grant_id)
        folders = [folder]

        return Message(
            starred=False,  # Default to false, could be enhanced with IMAP flags
            unread=True,  # Default to true, could be enhanced with IMAP flags
            folders=folders,
            grant_id=grant_id,
            date=timestamp,
            attachments=attachments,
            from_=from_addresses,
            id=message_id,
            object="message",
            snippet=snippet,
            subject=subject,
            thread_id=references[0] if references else message_id,
            to=to_addresses,
            created_at=timestamp,
            body=body,
        )

    def _parse_addresses(self, address_string: str) -> list[EmailAddress]:
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
            self._logger.exception(f"Failed to parse addresses '{address_string}'")
            return []

    def _parse_references(self, msg: PythonEmailMessage) -> list[str]:
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

    def _extract_body(self, msg: PythonEmailMessage) -> str:
        """Extract the body text from an email message."""
        body = ""

        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition", ""))

                # Skip attachments
                if "attachment" in content_disposition:
                    continue

                if content_type == "text/html" and not body:
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
                elif content_type == "text/plain":
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
                    break
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

    def _extract_attachments(self, msg: PythonEmailMessage, grant_id: str) -> list[MessageAttachment]:
        """Extract attachments from an email message."""
        attachments = []

        try:
            if msg.is_multipart():
                for i, part in enumerate(msg.walk()):
                    content_disposition = str(part.get("Content-Disposition", ""))

                    if "attachment" in content_disposition:
                        filename = part.get_filename()
                        if filename:
                            content_type = part.get_content_type()
                            payload = part.get_payload(decode=True)
                            size = len(payload) if payload else 0

                            attachment = MessageAttachment(
                                id=f"att_{i}",
                                grant_id=grant_id,
                                filename=filename,
                                size=size,
                                content_type=content_type,
                                is_inline=False,
                                content_disposition=content_disposition,
                            )
                            attachments.append(attachment)

        except Exception:
            self._logger.exception("Failed to extract attachments")

        return attachments

    async def list_messages(
        self, account: Account, folder: str = "INBOX", limit: int = 50, offset: int = 0
    ) -> list[Message]:
        """
        List messages from a folder.

        Args:
            account: The account to list messages from
            folder: The folder to list messages from (default: "INBOX")
            limit: Maximum number of messages to return
            offset: Number of messages to skip

        Returns:
            List of Message objects in Nylas format
        """
        account_config = self._account_to_config(account)
        messages: list[Message] = []

        try:
            connection = await self._connection_manager.get_connection(account_config, folder)

            # Get all message UIDs
            result = await connection.search("ALL")
            if not result or not result[1]:
                return messages

            uids = result[1][0].decode().split()

            # Apply offset and limit
            start_idx = offset
            end_idx = min(offset + limit, len(uids))
            selected_uids = uids[start_idx:end_idx]

            # Fetch messages
            for uid in selected_uids:
                try:
                    fetch_result = await connection.fetch(uid, "(RFC822)")
                    if fetch_result and fetch_result[1]:
                        raw_message = fetch_result[1][0][1]
                        if isinstance(raw_message, bytes):
                            parsed_message = email.message_from_bytes(raw_message)
                        else:
                            parsed_message = email.message_from_string(raw_message)

                        nylas_message = await self._convert_to_nylas_format(
                            parsed_message, uid, str(account.uuid), raw_message
                        )
                        messages.append(nylas_message)

                except Exception:
                    self._logger.exception(f"Failed to process message UID {uid}")
                    continue

            # Release connection back to pool
            await self._connection_manager.release_connection(connection, account_config)

        except Exception:
            self._logger.exception(f"Error listing messages for account {account.email}")

        return messages
