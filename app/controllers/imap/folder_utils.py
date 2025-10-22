import logging
from typing import List

from app.controllers.imap.connection import ConnectionManager
from app.models import Account

logger = logging.getLogger(__name__)


class FolderUtils:
    """Utility class for IMAP folder operations."""

    @staticmethod
    async def get_account_folders(
        connection_manager: ConnectionManager, account: Account, max_folders: int = 15
    ) -> List[str]:
        """
        Get list of folders for an account.

        Args:
            connection_manager: The connection manager to use
            account: The account configuration
            max_folders: Maximum number of folders to return (default: 15)

        Returns:
            List of folder names
        """
        try:
            connection = await connection_manager.get_connection_or_fail(account)

            response = await connection.list('""', "*")
            folders = []

            # Parse LIST response
            for line in response.lines:
                # Skip the completion line
                if b"LIST completed" in line or b"OK" in line:
                    continue

                # Parse folder from response like: b'(\\Archive) "." "Archive"' or b'(\\HasNoChildren) "/" INBOX'
                folder_name = FolderUtils.parse_folder_from_list_response(line)
                if folder_name:
                    # TODO: Allow selecting what folders to include in a user?
                    # Ignore these folders by default.
                    if folder_name.lower() in ["drafts", "junk", "archive", "trash", "spam"]:
                        continue

                    # Skip empty folder names
                    if folder_name.strip():
                        folders.append(folder_name)

            await connection_manager.close_connection(connection, account)

            # Limit folders per account to prevent resource exhaustion
            if len(folders) > max_folders:
                folders = folders[:max_folders]
                logger.warning(f"Limited {account.email} to first {max_folders} folders")

            logger.info(f"Found {len(folders)} folders for {account.email}: {folders}")
            return folders

        except Exception:
            logger.exception(f"Failed to get folders for {account.email}")
            # Return common default folders as fallback
            return ["INBOX", "Sent"]

    @staticmethod
    def parse_folder_from_list_response(line: bytes) -> str | None:
        """
        Parse folder name from IMAP LIST response line.

        Args:
            line: Raw IMAP LIST response line

        Returns:
            Folder name or None if parsing fails
        """
        try:
            if not isinstance(line, bytes):
                return None

            # Extract folder name from IMAP LIST response
            # Format: (flags) "delimiter" "folder_name" OR (flags) "delimiter" folder_name
            # Example: b'(\\Drafts \\HasNoChildren) "/" Drafts'
            # Example: b'(\\Sent \\HasNoChildren) "/" "Sent Items"'

            # Find the end of flags (closing parenthesis)
            flags_end = line.find(b")")
            if flags_end == -1:
                return None

            # Everything after the flags
            after_flags = line[flags_end + 1 :].strip()

            # Find the delimiter (first quoted string)
            # The delimiter is always quoted, e.g., "/"
            first_quote = after_flags.find(b'"')
            if first_quote == -1:
                return None

            # Find the closing quote of the delimiter
            second_quote = after_flags.find(b'"', first_quote + 1)
            if second_quote == -1:
                return None

            # Everything after the delimiter is the folder name
            folder_part = after_flags[second_quote + 1 :].strip()

            if not folder_part:
                return None

            # If folder name is quoted, extract it
            if folder_part.startswith(b'"') and folder_part.endswith(b'"'):
                folder_name = folder_part[1:-1].decode("utf-8")
            else:
                # Unquoted folder name
                folder_name = folder_part.decode("utf-8")

            return folder_name.strip() if folder_name.strip() else None

        except Exception as e:
            logger.warning(f"Failed to parse folder from line {line.decode('utf-8', errors='ignore')}: {e}")

        return None
