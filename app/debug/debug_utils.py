"""
Debug utilities for production debugging.

Usage from a pod Python REPL:
    >>> from app.debug import DebugUtils
    >>> import asyncio
    >>>
    >>> # Initialize the debug utils
    >>> debug = DebugUtils()
    >>> await debug.init()
    >>>
    >>> # Get account by UUID
    >>> account = await debug.get_account_by_uuid("abc-123-def")
    >>>
    >>> # Get account by email
    >>> account = await debug.get_account_by_email("user@example.com")
    >>>
    >>> # List all folders for an account
    >>> folders = await debug.list_folders(account.uuid)
    >>>
    >>> # List messages in a folder
    >>> messages = await debug.list_messages(account.uuid, folder="INBOX", limit=10)
    >>>
    >>> # Get a specific message by ID
    >>> message = await debug.get_message_by_id(account.uuid, "<message-id@example.com>")
    >>>
    >>> # View account details
    >>> details = await debug.get_account_details(account.uuid)
    >>>
    >>> # List all accounts
    >>> accounts = await debug.list_all_accounts()
    >>>
    >>> # Close connections when done
    >>> await debug.close()

Or use the simpler synchronous wrapper:
    >>> from app.debug import debug_shell
    >>> debug_shell()
    # This starts an interactive shell with all utilities available
"""

import asyncio
import logging
import os
import subprocess
import sys
from contextlib import AbstractAsyncContextManager
from pprint import pprint
from typing import Any
from uuid import UUID

from fastapi_async_sqlalchemy import db
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.controllers.imap.connection import ConnectionManager
from app.controllers.imap.folder_utils import FolderUtils
from app.controllers.imap.message_controller import MessageController
from app.db import fastapi_sqlalchemy_context
from app.models import Account, Email
from app.models.account import AccountStatus
from app.repos.account import AccountRepo
from app.repos.email import EmailRepo


class DebugUtils:
    """Utility class for debugging in production."""

    def __init__(self) -> None:
        """Initialize debug utilities."""
        self._logger = logging.getLogger(__name__)
        self._connection_manager: ConnectionManager | None = None
        self._message_controller: MessageController | None = None
        self._account_repo: AccountRepo | None = None
        self._email_repo: EmailRepo | None = None
        self._context_stack: AbstractAsyncContextManager[None] | None = None

    async def init(self) -> None:
        """Initialize database connection and controllers."""
        self._context_stack = fastapi_sqlalchemy_context()
        await self._context_stack.__aenter__()

        self._connection_manager = ConnectionManager()
        self._message_controller = MessageController(self._connection_manager)
        self._account_repo = AccountRepo()
        self._email_repo = EmailRepo()
        self._logger.info("Debug utils initialized")

    async def close(self) -> None:
        """Close all connections and cleanup."""
        if self._connection_manager:
            await self._connection_manager.close_all_connections()

        if self._context_stack:
            await self._context_stack.__aexit__(None, None, None)

        self._logger.info("Debug utils closed")

    async def get_account_by_uuid(self, account_uuid: str | UUID) -> Account | None:
        """
        Get account by UUID.

        Args:
            account_uuid: The account UUID (string or UUID object)

        Returns:
            Account object or None if not found
        """
        if not self._account_repo:
            raise RuntimeError("Debug utils not initialized. Call await debug.init() first.")

        try:
            uuid_str = str(account_uuid)
            query = select(Account).where(Account.uuid == uuid_str).options(selectinload(Account.app))
            result = await db.session.execute(query)
            account: Account | None = result.scalar_one_or_none()

            if account:
                self._logger.info(f"Found account: {account.email}")
                return account
            else:
                self._logger.warning(f"No account found with UUID: {uuid_str}")
                return None
        except Exception as e:
            self._logger.error(f"Error getting account by UUID: {e}")
            raise

    async def get_account_by_email(self, email: str) -> Account | None:
        """
        Get account by email address.

        Args:
            email: The email address to search for

        Returns:
            Account object or None if not found
        """
        if not self._account_repo:
            raise RuntimeError("Debug utils not initialized. Call await debug.init() first.")

        try:
            account = await self._account_repo.get_by_email(email)
            if account:
                self._logger.info(f"Found account: {account.email} (UUID: {account.uuid})")
                return account
            else:
                self._logger.warning(f"No account found with email: {email}")
                return None
        except Exception as e:
            self._logger.error(f"Error getting account by email: {e}")
            raise

    async def get_account_by_id(self, account_id: int) -> Account | None:
        """
        Get account by internal ID.

        Args:
            account_id: The account internal ID

        Returns:
            Account object or None if not found
        """
        if not self._account_repo:
            raise RuntimeError("Debug utils not initialized. Call await debug.init() first.")

        try:
            query = select(Account).where(Account.id == account_id).options(selectinload(Account.app))
            result = await db.session.execute(query)
            account: Account | None = result.scalar_one_or_none()

            if account:
                self._logger.info(f"Found account: {account.email} (UUID: {account.uuid})")
                return account
            else:
                self._logger.warning(f"No account found with ID: {account_id}")
                return None
        except Exception as e:
            self._logger.error(f"Error getting account by ID: {e}")
            raise

    async def list_all_accounts(self, status: AccountStatus | None = None) -> list[Account]:
        """
        List all accounts in the database.

        Args:
            status: Optional status filter (AccountStatus.active, .pending, .inactive)

        Returns:
            List of Account objects
        """
        if not self._account_repo:
            raise RuntimeError("Debug utils not initialized. Call await debug.init() first.")

        try:
            query = select(Account).options(selectinload(Account.app))

            if status:
                query = query.where(Account.status == status)

            result = await db.session.execute(query)
            accounts = list(result.scalars().all())

            self._logger.info(f"Found {len(accounts)} accounts")
            return accounts
        except Exception as e:
            self._logger.error(f"Error listing accounts: {e}")
            raise

    async def list_folders(self, account_identifier: str | UUID | int) -> list[str]:
        """
        List all folders for an account.

        Args:
            account_identifier: Account UUID, email, or internal ID

        Returns:
            List of folder names
        """
        account = await self._get_account(account_identifier)
        if not account:
            return []

        if not self._connection_manager:
            raise RuntimeError("Debug utils not initialized. Call await debug.init() first.")

        try:
            folders = await FolderUtils.get_account_folders(
                self._connection_manager,
                account,
                max_folders=100,  # Higher limit for debugging
            )
            self._logger.info(f"Found {len(folders)} folders for {account.email}: {folders}")
            return folders
        except Exception as e:
            self._logger.error(f"Error listing folders: {e}")
            raise

    async def list_messages(
        self, account_identifier: str | UUID | int, folder: str = "INBOX", limit: int = 20, offset: int = 0
    ) -> list[dict[str, Any]]:
        """
        List messages from a folder.

        Args:
            account_identifier: Account UUID, email, or internal ID
            folder: Folder name (default: "INBOX")
            limit: Maximum number of messages to return
            offset: Number of messages to skip

        Returns:
            List of message dictionaries
        """
        account = await self._get_account(account_identifier)
        if not account:
            return []

        if not self._message_controller:
            raise RuntimeError("Debug utils not initialized. Call await debug.init() first.")

        try:
            messages = await self._message_controller.list_messages(account, folder=folder, limit=limit, offset=offset)
            self._logger.info(f"Found {len(messages)} messages in {folder} for {account.email}")

            # Convert to dictionaries for easier viewing
            return [msg.model_dump(by_alias=True) for msg in messages]
        except Exception as e:
            self._logger.error(f"Error listing messages: {e}")
            raise

    async def get_message_by_id(
        self, account_identifier: str | UUID | int, message_id: str, folder: str | None = None
    ) -> dict[str, Any] | None:
        """
        Get a specific message by its Message-ID.

        Args:
            account_identifier: Account UUID, email, or internal ID
            message_id: The Message-ID to search for (e.g., '<abc123@domain.com>')
            folder: Optional folder hint to search first

        Returns:
            Message dictionary or None if not found
        """
        account = await self._get_account(account_identifier)
        if not account:
            return None

        if not self._message_controller:
            raise RuntimeError("Debug utils not initialized. Call await debug.init() first.")

        try:
            message_result = await self._message_controller.get_message_by_id(account, message_id, folder=folder)

            if message_result:
                self._logger.info(f"Found message: {message_id}")
                return message_result.message.model_dump(by_alias=True)
            else:
                self._logger.warning(f"Message not found: {message_id}")
                return None
        except Exception as e:
            self._logger.error(f"Error getting message: {e}")
            raise

    async def get_account_details(self, account_identifier: str | UUID | int) -> dict[str, Any] | None:
        """
        Get detailed information about an account.

        Args:
            account_identifier: Account UUID, email, or internal ID

        Returns:
            Dictionary with account details including stats
        """
        account = await self._get_account(account_identifier)
        if not account:
            return None

        try:
            # Get email count for this account
            query = select(Email).where(Email.account_id == account.id)
            result = await db.session.execute(query)
            emails = list(result.scalars().all())

            # Get folders
            folders = []
            try:
                folders = await self.list_folders(account.uuid)
            except Exception as e:
                self._logger.warning(f"Could not fetch folders: {e}")

            details = {
                "id": account.id,
                "uuid": str(account.uuid),
                "email": account.email,
                "provider": account.provider.value,
                "status": account.status.value,
                "app_id": account.app_id,
                "app_name": account.app.name if hasattr(account, "app") and account.app else None,
                "webhook_url": account.app.webhook_url if hasattr(account, "app") and account.app else None,
                "created_at": account.created_at.isoformat() if hasattr(account, "created_at") else None,
                "updated_at": account.updated_at.isoformat() if hasattr(account, "updated_at") else None,
                "provider_context": account.provider_context,
                "stats": {
                    "cached_emails": len(emails),
                    "folders": folders,
                    "folder_count": len(folders),
                },
            }

            self._logger.info(f"Retrieved details for account: {account.email}")
            return details
        except Exception as e:
            self._logger.error(f"Error getting account details: {e}")
            raise

    async def get_cached_emails(
        self, account_identifier: str | UUID | int, folder: str | None = None
    ) -> list[dict[str, Any]]:
        """
        Get emails cached in the database for an account.

        Args:
            account_identifier: Account UUID, email, or internal ID
            folder: Optional folder filter

        Returns:
            List of cached email records
        """
        account = await self._get_account(account_identifier)
        if not account:
            return []

        try:
            query = select(Email).where(Email.account_id == account.id)

            if folder:
                query = query.where(Email.folder == folder)

            result = await db.session.execute(query)
            emails = list(result.scalars().all())

            email_dicts = []
            for email_obj in emails:
                email_dicts.append(
                    {
                        "email_id": email_obj.email_id,
                        "thread_id": email_obj.thread_id,
                        "folder": email_obj.folder,
                        "uid": email_obj.uid,
                        "created_at": email_obj.created_at.isoformat() if hasattr(email_obj, "created_at") else None,
                    }
                )

            self._logger.info(f"Found {len(email_dicts)} cached emails for {account.email}")
            return email_dicts
        except Exception as e:
            self._logger.error(f"Error getting cached emails: {e}")
            raise

    async def _get_account(self, account_identifier: str | UUID | int) -> Account | None:
        """
        Helper method to get account from various identifier types.

        Args:
            account_identifier: Account UUID, email, or internal ID

        Returns:
            Account object or None
        """
        if isinstance(account_identifier, int):
            return await self.get_account_by_id(account_identifier)

        identifier_str = str(account_identifier)

        # Try UUID format first
        if len(identifier_str) == 36 or "-" in identifier_str:
            return await self.get_account_by_uuid(identifier_str)

        # Try email format
        if "@" in identifier_str:
            return await self.get_account_by_email(identifier_str)

        # Try as ID
        try:
            account_id = int(identifier_str)
            return await self.get_account_by_id(account_id)
        except ValueError:
            pass

        # Last try as UUID
        return await self.get_account_by_uuid(identifier_str)


# Helper functions for easier use
async def quick_debug(account_identifier: str | UUID | int) -> None:
    """
    Quick debug function to print account details.

    Usage:
        >>> await quick_debug("user@example.com")
        >>> await quick_debug("abc-123-def")
    """
    debug = DebugUtils()
    try:
        await debug.init()

        # Get and print account details
        details = await debug.get_account_details(account_identifier)
        if details:
            print("\n=== Account Details ===")
            pprint(details)

            # List some recent messages from INBOX
            print("\n=== Recent Messages (INBOX, last 5) ===")
            messages = await debug.list_messages(account_identifier, folder="INBOX", limit=5)
            for i, msg in enumerate(messages, 1):
                print(f"\n{i}. Subject: {msg.get('subject')}")
                print(f"   From: {msg.get('from')}")
                print(f"   Date: {msg.get('date')}")
                print(f"   ID: {msg.get('id')}")
        else:
            print(f"Account not found: {account_identifier}")
    finally:
        await debug.close()


def debug_shell() -> None:
    """
    Start an interactive debug shell with utilities loaded.

    Usage:
        >>> from app.debug import debug_shell
        >>> debug_shell()
    """
    # Find the debug_startup.py file
    startup_script = os.path.join(os.path.dirname(__file__), "debug_startup.py")

    if not os.path.exists(startup_script):
        print("Error: debug_startup.py not found")
        print("Please use: make debug-shell")
        sys.exit(1)

    env = os.environ.copy()
    env["PYTHONSTARTUP"] = startup_script

    result = subprocess.run([sys.executable, "-m", "asyncio"], env=env)
    sys.exit(result.returncode)


# Convenience wrapper for simple cases
class SimpleDebug:
    """
    Simplified synchronous-style wrapper for common debug operations.
    Uses asyncio.run() internally so you don't need to manage async/await.

    Usage:
        >>> from app.debug import SimpleDebug
        >>> sd = SimpleDebug()
        >>> account = sd.get_account("user@example.com")
        >>> folders = sd.list_folders(account.uuid)
    """

    def __init__(self) -> None:
        self._debug: DebugUtils | None = None

    def _ensure_debug(self) -> None:
        """Ensure debug utils are initialized."""
        if self._debug is None:
            self._debug = DebugUtils()
            asyncio.run(self._debug.init())

    def get_account(self, identifier: str | UUID | int) -> Account | None:
        """Get account by UUID, email, or ID."""
        self._ensure_debug()

        if self._debug is None:
            raise RuntimeError("Failed to initialize debug utils")

        if isinstance(identifier, int):
            return asyncio.run(self._debug.get_account_by_id(identifier))
        elif "@" in str(identifier):
            return asyncio.run(self._debug.get_account_by_email(str(identifier)))
        else:
            return asyncio.run(self._debug.get_account_by_uuid(identifier))

    def list_folders(self, account_identifier: str | UUID | int) -> list[str]:
        """List folders for an account."""
        self._ensure_debug()

        if self._debug is None:
            raise RuntimeError("Failed to initialize debug utils")

        return asyncio.run(self._debug.list_folders(account_identifier))

    def list_messages(
        self, account_identifier: str | UUID | int, folder: str = "INBOX", limit: int = 20
    ) -> list[dict[str, Any]]:
        """List messages from a folder."""
        self._ensure_debug()

        if self._debug is None:
            raise RuntimeError("Failed to initialize debug utils")

        return asyncio.run(self._debug.list_messages(account_identifier, folder, limit))

    def get_account_details(self, account_identifier: str | UUID | int) -> dict[str, Any] | None:
        """Get account details."""
        self._ensure_debug()

        if self._debug is None:
            raise RuntimeError("Failed to initialize debug utils")

        return asyncio.run(self._debug.get_account_details(account_identifier))

    def close(self) -> None:
        """Close connections."""
        if self._debug:
            asyncio.run(self._debug.close())
