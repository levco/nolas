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

                # Parse folder from response like: b'(\\Archive) "." "Archive"'
                if isinstance(line, bytes) and b'"' in line:
                    parts = line.split(b'"')
                    if len(parts) >= 3:
                        folder_name = parts[-2].decode("utf-8")
                        # TODO: Allow selecting what folders to include in a user?
                        # Ignore these folders by default.
                        if folder_name.lower() in ["drafts", "junk", "archive", "trash"]:
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
            if not isinstance(line, bytes) or b'"' not in line:
                return None

            # Extract folder name from IMAP LIST response
            # Format: (flags) "delimiter" "folder_name"
            parts = line.split(b'"')
            if len(parts) >= 3:
                folder_name = parts[-2].decode("utf-8")
                return folder_name.strip() if folder_name.strip() else None

        except Exception as e:
            logger.warning(f"Failed to parse folder from line {line.decode('utf-8', errors='ignore')}: {e}")

        return None
