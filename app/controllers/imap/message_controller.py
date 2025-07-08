import email
import logging
import urllib.parse
from email.message import Message as PythonEmailMessage
from typing import Any

from app.api.models.messages import Message
from app.controllers.email.message import MessageResult
from app.controllers.imap.connection import ConnectionManager
from app.controllers.imap.folder_utils import FolderUtils
from app.models import Account
from app.utils.message_utils import MessageUtils


class MessageController:
    """Controller for fetching email messages from IMAP servers."""

    def __init__(self, connection_manager: ConnectionManager):
        self._logger = logging.getLogger(__name__)
        self._connection_manager = connection_manager

    async def get_message_by_id(
        self, account: Account, message_id: str, folder: str | None = None, uid: str | None = None
    ) -> MessageResult | None:
        """
        Fetch a message by its Message-ID from IMAP server across all folders.

        Args:
            account: The account to search in
            message_id: The Message-ID to search for (e.g., '<abc123@domain.com>')

        Returns:
            Message object in Nylas format or None if not found
        """
        try:
            # Decode and format the message ID
            search_message_id = self._decode_message_id(message_id)
            if folder is not None:
                # Search first in the specified folder with the provided UID.
                message = await self._get_message_from_folder(account, search_message_id, folder, uid)
                if message:
                    return message

            folders = await FolderUtils.get_account_folders(self._connection_manager, account)
            self._logger.info(f"Searching for message ID: {search_message_id} in {len(folders)} folders")
            for search_folder in folders:
                if search_folder == folder:
                    continue

                message = await self._get_message_from_folder(account, search_message_id, search_folder)
                if message:
                    return message

            self._logger.info(f"Message with ID {search_message_id} not found in any of {len(folders)} folders")
            return None

        except Exception:
            self._logger.exception(f"Error fetching message {message_id} for account {account.email}")
            return None

    async def _get_message_from_folder(
        self, account: Account, search_message_id: str, folder: str, uid: str | None = None
    ) -> MessageResult | None:
        """Search for a message by Message-ID in a specific folder."""
        connection = None
        try:
            # Get connection for this folder
            connection = await self._connection_manager.get_connection(account, folder)

            if uid is not None:
                raw_message = await self._fetch_message_from_folder(connection, uid, account, folder)
                if raw_message:
                    self._logger.info(
                        f"Successfully retrieved message {search_message_id} from folder {folder} using UID {uid}"
                    )
                    nylas_message = MessageUtils.convert_to_nylas_format(raw_message, account.uuid, folder)
                    return MessageResult(message=nylas_message, raw_message=raw_message, uid=uid)

            # If the UID is not provided or message not found, search for the message in this folder.
            uid = await self._search_message_in_folder(connection, search_message_id, folder)
            if uid:
                raw_message = await self._fetch_message_from_folder(connection, uid, account, folder)
                if raw_message:
                    self._logger.info(f"Successfully retrieved message {search_message_id} from folder {folder}")
                    nylas_message = MessageUtils.convert_to_nylas_format(raw_message, account.uuid, folder)
                    return MessageResult(message=nylas_message, raw_message=raw_message, uid=uid)

        except Exception as folder_error:
            self._logger.exception(f"Error searching folder {folder} for message {search_message_id}: {folder_error}")
        finally:
            if connection:
                try:
                    # Close the connection instead of releasing it back to the pool to prevent connection leaks.
                    await self._connection_manager.close_connection(connection, account)
                except Exception:
                    pass
        return None

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
    ) -> PythonEmailMessage | None:
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
            return parsed_message

        except Exception:
            self._logger.exception(f"Error fetching message UID {uid} from folder {folder}")
            return None

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
        messages: list[Message] = []

        try:
            connection = await self._connection_manager.get_connection(account, folder)

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

                        nylas_message = MessageUtils.convert_to_nylas_format(parsed_message, account.uuid, folder)
                        messages.append(nylas_message)

                except Exception:
                    self._logger.exception(f"Failed to process message UID {uid}")
                    continue

            # Close connection instead of releasing back to pool to prevent leaks
            await self._connection_manager.close_connection(connection, account)

        except Exception:
            self._logger.exception(f"Error listing messages for account {account.email}")

        return messages
