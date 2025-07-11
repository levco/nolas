import asyncio
import email
import logging
import random
from email.message import Message

from aioimaplib import IMAP4_SSL, Response

from app.constants.emails import HEADER_MESSAGE_ID
from app.controllers.imap.connection import ConnectionManager
from app.controllers.imap.email_processor import EmailProcessor
from app.controllers.imap.folder_utils import FolderUtils
from app.models import Account, Email
from app.repos.connection_health import ConnectionHealthRepo
from app.repos.email import EmailRepo
from app.repos.uid_tracking import UidTrackingRepo
from settings import settings


class IMAPListener:
    """Async IMAP listener that polls folders for new emails."""

    def __init__(
        self,
        connection_health_repo: ConnectionHealthRepo,
        uid_tracking_repo: UidTrackingRepo,
        email_repo: EmailRepo,
        connection_manager: ConnectionManager,
        email_processor: EmailProcessor,
    ):
        self._logger = logging.getLogger(__name__)
        self._active_listeners: dict[str, asyncio.Task[None]] = {}  # account:folder -> task
        self._shutdown_event = asyncio.Event()
        self._listener_lock = asyncio.Lock()

        self._connection_health_repo = connection_health_repo
        self._uid_tracking_repo = uid_tracking_repo
        self._email_repo = email_repo
        self._connection_manager = connection_manager
        self._email_processor = email_processor

    async def start_account_listener(self, account: Account) -> list[asyncio.Task[None]]:
        """Start listening to all folders for an account."""
        await self._email_processor.init_session()

        try:
            # Get list of folders using shared utility
            folders = await FolderUtils.get_account_folders(self._connection_manager, account)

            tasks = []
            for folder in folders:
                task = asyncio.create_task(self._listen_to_folder(account, folder))

                listener_key = f"{account.email}:{folder}"
                async with self._listener_lock:
                    if listener_key in self._active_listeners:
                        self._logger.warning(f"Listener already active for {listener_key}")
                        continue

                    self._active_listeners[listener_key] = task

                tasks.append(task)
                self._logger.info(f"Started polling for {account.email}:{folder}")

            return tasks

        except Exception as e:
            self._logger.error(f"Failed to start account listener for {account.email}: {e}")
            await self._record_connection_health(account.id, "ALL", False, str(e))
            return []

    async def stop_listener(self, account_email: str, folder: str) -> None:
        """Stop a specific listener."""
        listener_key = f"{account_email}:{folder}"

        async with self._listener_lock:
            task = self._active_listeners.get(listener_key)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

                self._active_listeners.pop(listener_key, None)
                self._logger.info(f"Stopped listener for {account_email}:{folder}")

    async def stop_account_listeners(self, account_email: str) -> None:
        """Stop all listeners for an account."""
        tasks_to_cancel: list[asyncio.Task[None]] = []

        async with self._listener_lock:
            for listener_key in list(self._active_listeners.keys()):
                if listener_key.startswith(f"{account_email}:"):
                    task = self._active_listeners.pop(listener_key)
                    if not task.done():
                        tasks_to_cancel.append(task)

        # Cancel tasks
        for task in tasks_to_cancel:
            task.cancel()

        # Wait for cancellation
        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

        self._logger.info(f"Stopped all listeners for {account_email}")

    async def stop_all_listeners(self) -> None:
        """Stop all active listeners."""
        self._shutdown_event.set()

        tasks_to_cancel: list[asyncio.Task[None]] = []
        async with self._listener_lock:
            for task in self._active_listeners.values():
                if not task.done():
                    tasks_to_cancel.append(task)
            self._active_listeners.clear()

        # Cancel all tasks
        for task in tasks_to_cancel:
            task.cancel()

        # Wait for cancellation with timeout
        if tasks_to_cancel:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks_to_cancel, return_exceptions=True),
                    timeout=30,  # 30 second timeout
                )
            except asyncio.TimeoutError:
                self._logger.warning("Timeout waiting for listeners to cancel, forcing shutdown")
                # Force cleanup any remaining tasks
                for task in tasks_to_cancel:
                    if not task.done():
                        self._logger.warning(f"Force cancelling stuck task: {task}")
                        task.cancel()

                # Give a short grace period for force cancellation
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*[t for t in tasks_to_cancel if not t.done()], return_exceptions=True), timeout=5
                    )
                except asyncio.TimeoutError:
                    self._logger.error("Some tasks failed to cancel even after force cancellation")

        # Close all connections with timeout
        try:
            await asyncio.wait_for(self._connection_manager.close_all_connections(), timeout=10)
        except asyncio.TimeoutError:
            self._logger.warning("Timeout closing connections, some may remain open")

        try:
            await asyncio.wait_for(self._email_processor.close_session(), timeout=5)
        except asyncio.TimeoutError:
            self._logger.warning("Timeout closing email processor session")

        self._logger.info("Stopped all IMAP listeners")

    async def get_listener_stats(self) -> dict[str, int]:
        """Get statistics about active listeners."""
        async with self._listener_lock:
            total_listeners = len(self._active_listeners)
            active_listeners = sum(1 for task in self._active_listeners.values() if not task.done())

            return {
                "total_listeners": total_listeners,
                "active_listeners": active_listeners,
                "failed_listeners": total_listeners - active_listeners,
            }

    async def _listen_to_folder(self, account: Account, folder: str) -> None:
        """Poll a specific folder for new emails."""

        consecutive_failures = 0
        max_failures = 5
        poll_interval = settings.imap.poll_interval

        # Add jitter to prevent thundering herd - spread polls across the interval
        jitter = random.uniform(0, min(settings.imap.poll_jitter_max, poll_interval * 0.5))
        self._logger.debug(f"Starting polling for {account.email}:{folder} with {jitter:.1f}s jitter")
        await asyncio.sleep(jitter)

        while not self._shutdown_event.is_set():
            connection: IMAP4_SSL | None = None
            try:
                connection = await self._connection_manager.get_connection_or_fail(account, folder)
                search_response = await connection.search("ALL")
                all_uids = self._parse_search_response(search_response)
                last_seen_uid = await self._get_last_seen_uid(account.id, folder)
                new_uids = [uid for uid in all_uids if uid > last_seen_uid]
                if new_uids:
                    self._logger.info(f"Found {len(new_uids)} new messages for {account.email}:{folder}: {new_uids}")
                    await self._process_new_messages_by_uids(connection, account, folder, new_uids)
                else:
                    self._logger.debug(f"No new messages for {account.email}:{folder}")

                # Record successful poll
                await self._record_connection_health(account.id, folder, True)
                consecutive_failures = 0

                await self._connection_manager.close_connection(connection, account)
                connection = None

                # Wait for next poll interval, checking for shutdown frequently
                for _ in range(poll_interval * 10):
                    if self._shutdown_event.is_set():
                        return
                    await asyncio.sleep(0.5)

            except asyncio.CancelledError:
                self._logger.info(f"Polling cancelled for {account.email}:{folder}")
                break

            except Exception as e:
                consecutive_failures += 1
                error_msg = str(e)

                self._logger.warning(
                    f"Polling error for {account.email}:{folder} (failure {consecutive_failures}): {error_msg}"
                )

                await self._record_connection_health(account.id, folder, False, error_msg)

                # Close connection on error
                if connection:
                    try:
                        await self._connection_manager.close_connection(connection, account)
                    except Exception:
                        pass
                    connection = None

                # Check if we should stop this folder
                if consecutive_failures >= max_failures:
                    self._logger.error(f"Max failures reached for {account.email}:{folder}, stopping polling")
                    break

                # Exponential backoff for errors, but not too long
                backoff_time = min(120, 10 * consecutive_failures)  # Max 2 minutes
                self._logger.debug(f"Backing off for {backoff_time}s after error")

                for _ in range(int(backoff_time * 10)):  # Check shutdown every 0.1 seconds
                    if self._shutdown_event.is_set():
                        return
                    await asyncio.sleep(0.1)

        # Clean up
        listener_key = f"{account.email}:{folder}"
        async with self._listener_lock:
            self._active_listeners.pop(listener_key, None)

        self._logger.info(f"Stopped polling for {account.email}:{folder}")

    async def _get_last_seen_uid(self, account_id: int, folder: str) -> int:
        """Get the last seen UID for an account/folder combination using repository."""
        try:
            return await self._uid_tracking_repo.get_last_seen_uid(account_id, folder)
        except Exception as e:
            self._logger.exception(f"Failed to get last seen UID: {e}")
        return 0

    async def _update_last_seen_uid(self, account_id: int, folder: str, uid: int) -> None:
        """Update the last seen UID for an account/folder combination using repository."""
        try:
            await self._uid_tracking_repo.update_last_seen_uid(account_id, folder, uid)
        except Exception:
            self._logger.exception("Failed to update last seen UID")

    async def _record_connection_health(
        self, account_id: int, folder: str, success: bool, error_message: str | None = None
    ) -> None:
        """Record connection health status using repository."""
        try:
            if success:
                await self._connection_health_repo.record_success(account_id, folder)
            else:
                await self._connection_health_repo.record_failure(account_id, folder, error_message or "Unknown error")
        except Exception:
            self._logger.exception("Failed to record connection health")

    async def _process_new_messages_by_uids(
        self, connection: IMAP4_SSL, account: Account, folder: str, new_uids: list[int]
    ) -> None:
        """Process new messages in the folder based on a list of UIDs."""
        try:
            # Fetch message data for each UID
            fetch_response = await connection.fetch(",".join(map(str, new_uids)), "RFC822")
            messages = self._parse_fetch_response(fetch_response)

            for uid, message_bytes in messages.items():
                raw_message = email.message_from_bytes(message_bytes)
                nylas_message = await self._email_processor.process_email(account, folder, uid, raw_message)

                # Update UID tracking
                await self._update_last_seen_uid(account.id, folder, uid)
                await self._upsert_cache(account, raw_message, folder, uid, nylas_message.thread_id)

            self._logger.info(f"Processed {len(new_uids)} new messages for {account.email}:{folder}")
            await self._uid_tracking_repo.commit()

        except Exception as e:
            self._logger.error(f"Failed to process new messages for {account.email}:{folder}: {e}")
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
            self._logger.error(f"Failed to parse search response: {e}")
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
            self._logger.error(f"Failed to parse fetch response: {e}")
        return messages

    async def _upsert_cache(
        self, account: Account, raw_message: Message, folder: str, uid: int, thread_id: str
    ) -> None:
        """Update or create the cache with the new message."""
        try:
            message_id = raw_message.get(HEADER_MESSAGE_ID)
            if message_id is None:
                self._logger.warning(f"Message ID is missing for {account.email}:{folder}:{uid}")
                return

            email = await self._email_repo.get_by_account_and_uid_or_email_id(account.id, folder, uid, message_id)
            if email:
                if email.email_id != message_id or email.uid != uid or email.folder != folder:
                    await self._email_repo.update(
                        email, {"email_id": message_id, "uid": uid, "folder": folder, "thread_id": thread_id}
                    )
            else:
                await self._email_repo.add(
                    Email(account_id=account.id, folder=folder, uid=uid, email_id=message_id, thread_id=thread_id)
                )
        except Exception:
            self._logger.exception("Failed to update cache")
