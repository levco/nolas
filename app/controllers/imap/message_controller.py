import email
import logging
from email.message import Message as PythonEmailMessage
from email.utils import getaddresses
from typing import List, Optional

from app.api.models.messages import EmailAddress, Message, MessageAttachment
from app.controllers.imap.connection import ConnectionManager
from app.controllers.imap.folder_utils import FolderUtils
from app.controllers.imap.models import AccountConfig
from app.models.account import Account

logger = logging.getLogger(__name__)


class MessageController:
    """Controller for fetching email messages from IMAP servers."""

    def __init__(self, connection_manager: ConnectionManager):
        self.connection_manager = connection_manager

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

    async def get_message_by_id(self, account: Account, message_id: str) -> Optional[Message]:
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
            # Handle URL encoding in message_id (< and > might be URL encoded)
            import urllib.parse

            decoded_message_id = urllib.parse.unquote(message_id)

            # Get list of all folders using shared utility
            folders = await FolderUtils.get_account_folders(self.connection_manager, account_config)

            # Search for message by Message-ID across all folders
            # Ensure the message ID has angle brackets
            search_message_id = decoded_message_id if decoded_message_id.startswith("<") else f"<{decoded_message_id}>"

            logger.info(f"Searching for message ID: {search_message_id} in {len(folders)} folders")

            for folder in folders:
                connection = None
                try:
                    # Get connection for this folder
                    connection = await self.connection_manager.get_connection(account_config, folder)

                    # Search for the message in this folder using proper IMAP search syntax
                    # Use the correct format for aioimaplib
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
                            logger.info(f"Found message {search_message_id} in folder {folder} with UID {uid}")

                            # Fetch the message
                            fetch_result = await connection.fetch(uid, "(RFC822)")
                            if fetch_result and fetch_result[1]:
                                # Parse the IMAP fetch response properly
                                # The structure is usually: [b'response_line', [b'message_content', b')']]
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
                                                if (
                                                    isinstance(nested_item, (bytes, bytearray))
                                                    and len(nested_item) > 100
                                                ):
                                                    raw_message = bytes(nested_item)
                                                    break
                                            if raw_message:
                                                break

                                if raw_message is None:
                                    logger.warning(
                                        f"Could not extract message content from fetch result for UID {uid} in folder {folder}"
                                    )
                                    logger.debug(
                                        f"Fetch result structure: {type(fetch_result[1])}, length: {len(fetch_result[1])}"
                                    )
                                    if len(fetch_result[1]) > 0:
                                        logger.debug(
                                            f"First item type: {type(fetch_result[1][0])}, content: {fetch_result[1][0][:200] if hasattr(fetch_result[1][0], '__getitem__') else fetch_result[1][0]}"
                                        )
                                    continue

                                # Parse the email message
                                parsed_message = email.message_from_bytes(raw_message)

                                # Convert to Nylas format
                                nylas_message = await self._convert_to_nylas_format(
                                    parsed_message, uid, str(account.uuid), raw_message, folder
                                )

                                # Release connection back to pool
                                await self.connection_manager.release_connection(connection, account_config)

                                logger.info(f"Successfully retrieved message {search_message_id} from folder {folder}")
                                return nylas_message

                except Exception as folder_error:
                    logger.exception(f"Error searching folder {folder} for message {search_message_id}: {folder_error}")
                    continue
                finally:
                    if connection:
                        try:
                            await self.connection_manager.release_connection(connection, account_config)
                        except Exception:
                            pass

            # Message not found in any folder
            logger.info(f"Message with ID {search_message_id} not found in any of {len(folders)} folders")
            return None

        except Exception as e:
            logger.error(f"Error fetching message {message_id} for account {account.email}: {e}")
            return None

    async def _convert_to_nylas_format(
        self, msg: PythonEmailMessage, uid: str, grant_id: str, raw_message: bytes, folder: str = "INBOX"
    ) -> Message:
        """Convert Python email message to Nylas Message format."""

        # Extract basic headers
        subject_header = msg.get("Subject")
        subject = str(subject_header) if subject_header else ""  # type: ignore

        message_id_header = msg.get("Message-ID")
        message_id = str(message_id_header) if message_id_header else ""  # type: ignore

        date_header_value = msg.get("Date")
        date_header = str(date_header_value) if date_header_value else ""  # type: ignore

        # Parse date
        import time
        from email.utils import mktime_tz, parsedate_tz

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

        # Parse from addresses
        from_header = msg.get("From")
        from_addresses = self._parse_addresses(str(from_header) if from_header else "")  # type: ignore

        # Parse to addresses
        to_header = msg.get("To")
        to_addresses = self._parse_addresses(str(to_header) if to_header else "")  # type: ignore

        # Extract body
        body = self._extract_body(msg)

        # Extract references to other messages in the thread
        references = self._parse_references(msg)

        # Create snippet from body (first 100 chars)
        snippet = body[:100] + "..." if len(body) > 100 else body

        # Extract attachments
        attachments = self._extract_attachments(msg, grant_id)

        # Use the actual folder where the message was found
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
            # Handle multiple addresses
            addresses = getaddresses([address_string])
            result = []

            for name, email_addr in addresses:
                if email_addr:  # Only include if email is present
                    result.append(
                        EmailAddress(
                            name=name or email_addr,  # Use email as name if name is empty
                            email=email_addr,
                        )
                    )

            return result
        except Exception as e:
            logger.warning(f"Failed to parse addresses '{address_string}': {e}")
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

                if content_type == "text/plain":
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
                elif content_type == "text/html" and not body:
                    # Use HTML as fallback if no plain text
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

        except Exception as e:
            logger.warning(f"Failed to extract attachments: {e}")

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
        messages: List[Message] = []

        try:
            # Get IMAP connection
            connection = await self.connection_manager.get_connection(account_config, folder)

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

                except Exception as e:
                    logger.warning(f"Failed to process message UID {uid}: {e}")
                    continue

            # Release connection back to pool
            await self.connection_manager.release_connection(connection, account_config)

        except Exception as e:
            logger.error(f"Error listing messages for account {account.email}: {e}")

        return messages
