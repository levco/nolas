import asyncio
import logging
import time

from aioimaplib import IMAP4_SSL, Response

from database import DatabaseManager
from lib.email_processor import EmailProcessor
from lib.imap.connection import ConnectionManager
from models import AccountConfig
from settings import settings

logger = logging.getLogger(__name__)


class IMAPListener:
    """Async IMAP listener that monitors folders for new emails."""

    def __init__(self, db_manager: DatabaseManager, email_processor: EmailProcessor):
        self.db_manager = db_manager
        self.email_processor = email_processor
        self.connection_manager = ConnectionManager()
        self.active_listeners: dict[str, asyncio.Task[None]] = {}  # account:folder -> task
        self.shutdown_event = asyncio.Event()
        self._listener_lock = asyncio.Lock()

    async def start_account_listener(self, account: AccountConfig) -> list[asyncio.Task[None]]:
        """Start listening to all folders for an account."""
        try:
            # Get list of folders
            folders = await self._get_account_folders(account)

            tasks = []
            for folder in folders:
                task = asyncio.create_task(self._listen_to_folder(account, folder))

                listener_key = f"{account.email}:{folder}"
                async with self._listener_lock:
                    self.active_listeners[listener_key] = task

                tasks.append(task)
                logger.info(f"Started listener for {account.email}:{folder}")

            return tasks

        except Exception as e:
            logger.error(f"Failed to start account listener for {account.email}: {e}")
            await self.db_manager.record_connection_health(account.email, "ALL", False, str(e))
            return []

    async def _get_account_folders(self, account: AccountConfig) -> list[str]:
        """Get list of folders for an account."""
        try:
            connection = await self.connection_manager.get_connection(account)

            response = await connection.list('""', "*")
            folders = []

            # Parse LIST response
            for line in response.lines:
                # Skip the completion line
                if b"LIST completed" in line:
                    continue

                # Parse folder from response like: b'(\\Archive) "." "Archive"'
                if isinstance(line, bytes) and b'"' in line:
                    parts = line.split(b'"')
                    if len(parts) >= 3:
                        folder_name = parts[-2].decode("utf-8")
                        if folder_name.lower() != "sent":
                            continue
                        folders.append(folder_name)

            await self.connection_manager.release_connection(connection, account.email)

            # Limit folders per account to prevent resource exhaustion
            if len(folders) > 15:
                folders = folders[:15]
                logger.warning(f"Limited {account.email} to first 15 folders")

            logger.info(f"Found {len(folders)} folders for {account.email}: {folders}")
            return folders

        except Exception as e:
            logger.exception(f"Failed to get folders for {account.email}: {e}")
            return []

    async def _listen_to_folder(self, account: AccountConfig, folder: str) -> None:
        """Listen to a specific folder for new emails."""
        connection = None
        consecutive_failures = 0
        max_failures = 5

        while not self.shutdown_event.is_set():
            try:
                # Get connection for this folder
                connection = await self.connection_manager.get_connection(account, folder)

                # Initialize UID tracking
                last_seen_uid = await self.db_manager.get_last_seen_uid(account.email, folder)

                status_response = await connection.status(folder, "(UIDNEXT)")
                # Parse UIDNEXT from response
                uidnext = self._parse_uidnext(status_response)

                if uidnext is None:
                    logger.warning(f"Could not get UIDNEXT for {account.email}:{folder}")
                    uidnext = last_seen_uid + 1

                # Process any existing new messages
                if last_seen_uid < uidnext - 1:
                    await self._process_new_messages(connection, account, folder, last_seen_uid)

                # Record successful connection
                await self.db_manager.record_connection_health(account.email, folder, True)
                consecutive_failures = 0

                # Start IDLE monitoring
                await self._idle_monitor(connection, account, folder)

            except asyncio.CancelledError:
                logger.info(f"Listener cancelled for {account.email}:{folder}")
                break

            except Exception as e:
                consecutive_failures += 1
                error_msg = str(e)

                logger.error(
                    f"Error in listener for {account.email}:{folder} (failure {consecutive_failures}): {error_msg}"
                )

                # Record connection health
                await self.db_manager.record_connection_health(account.email, folder, False, error_msg)

                # Close problematic connection
                if connection:
                    try:
                        await self.connection_manager.close_connection(connection, account.email)
                    except Exception:
                        pass
                    connection = None

                # Exponential backoff with max failures
                if consecutive_failures >= max_failures:
                    logger.error(f"Max failures reached for {account.email}:{folder}, stopping listener")
                    break

                backoff_time = min(300, 15 * (2**consecutive_failures))  # Max 5 minutes
                await asyncio.sleep(backoff_time)

            finally:
                if connection:
                    try:
                        await self.connection_manager.release_connection(connection, account.email)
                    except Exception:
                        pass

        # Clean up
        listener_key = f"{account.email}:{folder}"
        async with self._listener_lock:
            self.active_listeners.pop(listener_key, None)

        logger.info(f"Stopped listener for {account.email}:{folder}")

    def _parse_uidnext(self, status_response: Response) -> int | None:
        """Parse UIDNEXT from IMAP STATUS response."""
        try:
            for line in status_response.lines:
                if b"UIDNEXT" in line:
                    # Example line: b'"Archive" (UIDNEXT 1)'
                    # Find the part inside parentheses
                    start = line.find(b"(")
                    end = line.find(b")", start)
                    if start != -1 and end != -1:
                        inside = line[start + 1 : end]
                        # inside: b'UIDNEXT 1'
                        parts = inside.split()
                        for i, part in enumerate(parts):
                            if part == b"UIDNEXT" and i + 1 < len(parts):
                                try:
                                    return int(parts[i + 1])
                                except Exception:
                                    continue
            return None
        except Exception:
            return None

    async def _idle_monitor(self, connection: IMAP4_SSL, account: AccountConfig, folder: str) -> None:
        """Monitor folder using IMAP IDLE."""
        try:
            # Start IDLE
            await self.connection_manager.start_idle(connection, account.email)

            idle_start_time = time.time()
            max_idle_time = settings.imap.idle_timeout

            while not self.shutdown_event.is_set():
                try:
                    # Wait for IDLE response with timeout
                    response = await asyncio.wait_for(
                        connection.wait_hello_from_server(),
                        timeout=30,  # Check every 30 seconds
                    )

                    # Check if it's an EXISTS response (new message)
                    if b"EXISTS" in response:
                        logger.info(f"New message detected in {account.email}:{folder}")
                        await self.connection_manager.stop_idle(connection, account.email)

                        # Process new messages
                        last_seen_uid = await self.db_manager.get_last_seen_uid(account.email, folder)
                        await self._process_new_messages(connection, account, folder, last_seen_uid)

                        # Restart IDLE
                        await self.connection_manager.start_idle(connection, account.email)
                        idle_start_time = time.time()

                    # Check if IDLE has been running too long (refresh connection)
                    if time.time() - idle_start_time > max_idle_time:
                        logger.info(f"Refreshing IDLE connection for {account.email}:{folder}")
                        await self.connection_manager.stop_idle(connection, account.email)
                        break  # Exit IDLE loop to refresh connection

                except asyncio.TimeoutError:
                    # Timeout is expected, continue monitoring
                    continue

                except Exception as e:
                    logger.warning(f"IDLE error for {account.email}:{folder}: {e}")
                    break

        finally:
            try:
                await self.connection_manager.stop_idle(connection, account.email)
            except Exception:
                pass

    async def _process_new_messages(
        self, connection: IMAP4_SSL, account: AccountConfig, folder: str, last_seen_uid: int
    ) -> None:
        """Process new messages in the folder."""
        try:
            # Search for new messages
            search_response = await connection.search(f"UID {last_seen_uid + 1}:*")
            uids = self._parse_search_response(search_response)

            if uids:
                # Fetch message data
                fetch_response = await connection.fetch(",".join(map(str, uids)), "RFC822")
                messages = self._parse_fetch_response(fetch_response)

                for uid, raw_message in messages.items():
                    await self.email_processor.process_email(account, folder, uid, raw_message)

                    # Update UID tracking
                    # await self.db_manager.update_last_seen_uid(
                    #     account.email, folder, uid
                    # )

            logger.info(f"Processed new messages for {account.email}:{folder}")

        except Exception as e:
            logger.error(f"Failed to process new messages for {account.email}:{folder}: {e}")
            raise

    def _parse_search_response(self, search_response: Response) -> list[int]:
        """Parse UIDs from SEARCH response."""
        uids: list[int] = []
        try:
            for line in search_response.lines:
                # The line may look like: b'1 2 3 4 5 6 7 8'
                if isinstance(line, bytes):
                    line_str = line.decode("utf-8", errors="ignore").strip()
                else:
                    line_str = str(line).strip()
                # Skip lines that are completion messages
                if "SEARCH completed" in line_str or "OK" in line_str:
                    continue
                # Split by whitespace and collect digits
                for part in line_str.split():
                    if part.isdigit():
                        uids.append(int(part))
        except Exception as e:
            logger.error(f"Failed to parse search response: {e}")
        return uids

    def _parse_fetch_response(self, fetch_response: Response) -> dict[int, bytes]:
        """Parse messages from FETCH response."""
        messages = {}
        try:
            lines = fetch_response.lines
            i = 0
            while i < len(lines):
                line = lines[i]
                # Look for the FETCH header line, e.g. b'1 FETCH (RFC822 {1437}'
                if isinstance(line, (bytes, bytearray)) and b"FETCH" in line and b"RFC822" in line:
                    # Try to extract UID if present, else use the sequence number
                    uid = None
                    # Try to extract UID from the line, fallback to sequence number
                    if b"UID " in line:
                        try:
                            uid_part = line.split(b"UID ")[1]
                            uid_str = uid_part.split(b" ")[0]
                            uid = int(uid_str)
                        except Exception:
                            pass
                    if uid is None:
                        # Fallback: get the sequence number at the start of the line
                        try:
                            uid = int(line.split(b" ")[0])
                        except Exception:
                            continue  # skip if can't parse

                    # The next line should be the message bytes (bytearray)
                    if i + 1 < len(lines) and isinstance(lines[i + 1], (bytes, bytearray)):
                        message_bytes = lines[i + 1]
                        messages[uid] = bytes(message_bytes)
                        i += 2  # Skip to the line after the message
                        # Optionally, skip the closing b')' line if present
                        if i < len(lines) and lines[i] == b")":
                            i += 1
                        continue
                i += 1
        except Exception as e:
            logger.error(f"Failed to parse fetch response: {e}")
        return messages

    async def stop_listener(self, account_email: str, folder: str) -> None:
        """Stop a specific listener."""
        listener_key = f"{account_email}:{folder}"

        async with self._listener_lock:
            task = self.active_listeners.get(listener_key)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

                self.active_listeners.pop(listener_key, None)
                logger.info(f"Stopped listener for {account_email}:{folder}")

    async def stop_account_listeners(self, account_email: str) -> None:
        """Stop all listeners for an account."""
        tasks_to_cancel = []

        async with self._listener_lock:
            for listener_key in list(self.active_listeners.keys()):
                if listener_key.startswith(f"{account_email}:"):
                    task = self.active_listeners.pop(listener_key)
                    if not task.done():
                        tasks_to_cancel.append(task)

        # Cancel tasks
        for task in tasks_to_cancel:
            task.cancel()

        # Wait for cancellation
        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

        logger.info(f"Stopped all listeners for {account_email}")

    async def stop_all_listeners(self) -> None:
        """Stop all active listeners."""
        self.shutdown_event.set()

        tasks_to_cancel = []
        async with self._listener_lock:
            for task in self.active_listeners.values():
                if not task.done():
                    tasks_to_cancel.append(task)
            self.active_listeners.clear()

        # Cancel all tasks
        for task in tasks_to_cancel:
            task.cancel()

        # Wait for cancellation
        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

        # Close all connections
        await self.connection_manager.close_all_connections()

        logger.info("Stopped all IMAP listeners")

    async def get_listener_stats(self) -> dict[str, int]:
        """Get statistics about active listeners."""
        async with self._listener_lock:
            total_listeners = len(self.active_listeners)
            active_listeners = sum(1 for task in self.active_listeners.values() if not task.done())

            return {
                "total_listeners": total_listeners,
                "active_listeners": active_listeners,
                "failed_listeners": total_listeners - active_listeners,
            }
