import asyncio
import email
import logging
import time
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
    """Async IMAP listener that monitors folders for new emails."""

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
                self._logger.info(f"Started listener for {account.email}:{folder}")

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

        tasks_to_cancel = []
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
        """Listen to a specific folder for new emails."""
        connection = None
        consecutive_failures = 0
        consecutive_idle_failures = 0
        max_failures = 5
        max_idle_failures = 3

        while not self._shutdown_event.is_set():
            try:
                # Get connection for this folder
                connection = await self._connection_manager.get_connection(account, folder)

                # Initialize UID tracking
                last_seen_uid = await self._get_last_seen_uid(account.id, folder)
                status_response = await connection.status(folder, "(UIDNEXT)")
                uidnext = self._parse_uidnext(status_response)

                if uidnext is None:
                    self._logger.warning(f"Could not get UIDNEXT for {account.email}:{folder}")
                    uidnext = last_seen_uid + 1

                # Process any existing new messages
                if last_seen_uid < uidnext - 1:
                    await self._process_new_messages(connection, account, folder, last_seen_uid)

                # Record successful connection
                await self._record_connection_health(account.id, folder, True)
                consecutive_failures = 0

                # Start IDLE monitoring with retry logic
                idle_retry_count = 0
                max_idle_retries = 3

                while not self._shutdown_event.is_set() and idle_retry_count < max_idle_retries:
                    try:
                        await self._idle_monitor(connection, account, folder)
                        # If we get here, IDLE monitoring returned normally (e.g., for connection refresh)
                        consecutive_idle_failures = 0
                        break
                    except Exception as idle_error:
                        idle_retry_count += 1
                        consecutive_idle_failures += 1

                        self._logger.warning(
                            f"IDLE monitoring failed for {account.email}:{folder} "
                            f"(attempt {idle_retry_count}/{max_idle_retries}): {idle_error}"
                        )

                        # If this is an IDLE-specific error and we haven't exceeded max retries,
                        # try to restart IDLE without closing the connection
                        if idle_retry_count < max_idle_retries:
                            await asyncio.sleep(min(5 * idle_retry_count, 30))  # Progressive backoff
                            continue
                        else:
                            # Too many IDLE failures, treat as connection failure
                            raise idle_error

                # If we've had too many consecutive IDLE failures, close connection and restart
                if consecutive_idle_failures >= max_idle_failures:
                    self._logger.error(
                        f"Too many consecutive IDLE failures for {account.email}:{folder}, "
                        "closing connection and restarting"
                    )
                    if connection:
                        await self._connection_manager.close_connection(connection, account)
                        connection = None
                    consecutive_idle_failures = 0
                    await asyncio.sleep(30)  # Wait before retrying

            except asyncio.CancelledError:
                self._logger.info(f"Listener cancelled for {account.email}:{folder}")
                break

            except Exception as e:
                consecutive_failures += 1
                error_msg = str(e)

                self._logger.exception(
                    f"Error in listener for {account.email}:{folder} (failure {consecutive_failures}): {error_msg}"
                )

                await self._record_connection_health(account.id, folder, False, error_msg)

                # Close problematic connection
                if connection:
                    try:
                        await self._connection_manager.close_connection(connection, account)
                    except Exception:
                        pass
                    connection = None

                # Exponential backoff with max failures
                if consecutive_failures >= max_failures:
                    self._logger.error(f"Max failures reached for {account.email}:{folder}, stopping listener")
                    break

                backoff_time = min(300, 15 * (2**consecutive_failures))  # Max 5 minutes
                await asyncio.sleep(backoff_time)

            finally:
                if connection:
                    try:
                        await self._connection_manager.release_connection(connection, account)
                    except Exception:
                        pass

        # Clean up
        listener_key = f"{account.email}:{folder}"
        async with self._listener_lock:
            self._active_listeners.pop(listener_key, None)

        self._logger.info(f"Stopped listener for {account.email}:{folder}")

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

    async def _idle_monitor(self, connection: IMAP4_SSL, account: Account, folder: str) -> None:
        """Monitor folder using IMAP IDLE."""
        try:
            # Mark connection as idle in manager
            await self._connection_manager.start_idle(connection, account)

            idle_start_time = time.time()
            max_idle_time = settings.imap.idle_timeout

            while not self._shutdown_event.is_set():
                idle_task = None
                try:
                    # Check for shutdown at the beginning of each iteration
                    if self._shutdown_event.is_set():
                        self._logger.debug(f"Shutdown requested for {account.email}:{folder}, exiting IDLE monitor")
                        return

                    # Check if we need to refresh the connection
                    if time.time() - idle_start_time > max_idle_time:
                        self._logger.info(f"Refreshing IDLE connection for {account.email}:{folder}")
                        idle_start_time = time.time()
                        # Connection refresh happens in the outer loop, just continue here
                        return

                    # Start IDLE session
                    self._logger.debug(f"Starting IDLE session for {account.email}:{folder}")
                    idle_task = await connection.idle_start(timeout=30)

                    # Monitor for server push notifications
                    idle_timeout_count = 0
                    max_idle_timeouts = 5  # Allow up to 5 consecutive timeouts before restarting IDLE

                    while not self._shutdown_event.is_set():
                        try:
                            # Check if connection has pending data
                            if not connection.has_pending_idle():
                                self._logger.debug(
                                    f"No pending IDLE data for {account.email}:{folder}, checking again..."
                                )
                                # Use shorter sleep and check for shutdown more frequently
                                for _ in range(10):  # Check shutdown every 0.1 seconds for 1 second total
                                    if self._shutdown_event.is_set():
                                        return
                                    await asyncio.sleep(0.1)
                                continue

                            # Wait for server push notification with shorter timeout for more responsive shutdown
                            response = await asyncio.wait_for(connection.wait_server_push(), timeout=30)
                            idle_timeout_count = 0  # Reset timeout counter on successful response

                            # Check if it's an EXISTS response (new message)
                            exists_found = False
                            if isinstance(response, list):
                                exists_found = any(b"EXISTS" in item for item in response if isinstance(item, bytes))
                                self._logger.debug(f"IDLE response (list) for {account.email}:{folder}: {response!r}")
                            elif isinstance(response, bytes):
                                exists_found = b"EXISTS" in response
                                self._logger.debug(f"IDLE response (bytes) for {account.email}:{folder}: {response!r}")

                            if exists_found:
                                self._logger.info(f"New message detected in {account.email}:{folder}")

                                # Stop IDLE session
                                if idle_task:
                                    connection.idle_done()
                                    await asyncio.wait_for(idle_task, timeout=10)
                                    idle_task = None

                                # Process new messages
                                last_seen_uid = await self._get_last_seen_uid(account.id, folder)
                                await self._process_new_messages(connection, account, folder, last_seen_uid)

                                # Break to restart IDLE session
                                break

                        except asyncio.TimeoutError:
                            # Check for shutdown before handling timeout
                            if self._shutdown_event.is_set():
                                return

                            # Timeout waiting for server push
                            idle_timeout_count += 1
                            self._logger.debug(
                                f"IDLE timeout {idle_timeout_count} for {account.email}:{folder}, continuing..."
                            )

                            # If we've had too many consecutive timeouts, restart IDLE session
                            if idle_timeout_count >= max_idle_timeouts:
                                self._logger.debug(
                                    f"Too many IDLE timeouts for {account.email}:{folder}, restarting IDLE session"
                                )
                                break

                            continue

                        except Exception as e:
                            self._logger.error(f"Error waiting for server push in {account.email}:{folder}: {e}")
                            break

                    # Clean up IDLE session if it exited normally
                    if idle_task:
                        self._logger.debug(f"Cleaning up IDLE session for {account.email}:{folder}")
                        try:
                            connection.idle_done()
                            await asyncio.wait_for(idle_task, timeout=5)  # Shorter timeout for faster shutdown
                        except asyncio.TimeoutError:
                            self._logger.warning(
                                f"Timeout cleaning up IDLE session for {account.email}:{folder}, forcing cleanup"
                            )
                            # Force cancel the idle task if it's stuck
                            if idle_task and not idle_task.done():
                                idle_task.cancel()
                                try:
                                    await asyncio.wait_for(idle_task, timeout=2)
                                except asyncio.TimeoutError:
                                    self._logger.error(f"Failed to force cancel IDLE task for {account.email}:{folder}")
                        except Exception as e:
                            self._logger.warning(f"Error cleaning up IDLE session for {account.email}:{folder}: {e}")
                        finally:
                            idle_task = None

                except Exception as e:
                    self._logger.error(f"IDLE session error for {account.email}:{folder}: {e}")
                    if idle_task:
                        try:
                            connection.idle_done()
                            await asyncio.wait_for(idle_task, timeout=2)  # Shorter timeout for faster shutdown
                        except asyncio.TimeoutError:
                            self._logger.warning(
                                f"Timeout during error cleanup for {account.email}:{folder}, force cancelling"
                            )
                            idle_task.cancel()
                            try:
                                await asyncio.wait_for(idle_task, timeout=1)
                            except asyncio.TimeoutError:
                                pass
                        except Exception:
                            pass
                        idle_task = None

                    # Short delay before retrying, but check for shutdown
                    for _ in range(50):  # Check shutdown every 0.1 seconds for 5 seconds total
                        if self._shutdown_event.is_set():
                            return
                        await asyncio.sleep(0.1)

        finally:
            try:
                # Force cleanup any remaining idle task
                if "idle_task" in locals() and idle_task and not idle_task.done():
                    self._logger.debug(f"Force cleaning up remaining IDLE task for {account.email}:{folder}")
                    try:
                        connection.idle_done()
                        await asyncio.wait_for(idle_task, timeout=2)
                    except asyncio.TimeoutError:
                        idle_task.cancel()
                        try:
                            await asyncio.wait_for(idle_task, timeout=1)
                        except asyncio.TimeoutError:
                            pass
                    except Exception:
                        pass

                await asyncio.wait_for(self._connection_manager.stop_idle(connection, account), timeout=5)
                self._logger.info(f"IDLE monitoring stopped for {account.email}:{folder}")
            except asyncio.TimeoutError:
                self._logger.warning(f"Timeout stopping IDLE for {account.email}:{folder}")
            except Exception:
                pass

    async def _process_new_messages(
        self, connection: IMAP4_SSL, account: Account, folder: str, last_seen_uid: int
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

                for uid, message_bytes in messages.items():
                    raw_message = email.message_from_bytes(message_bytes)
                    nylas_message = await self._email_processor.process_email(account, folder, uid, raw_message)

                    # Update UID tracking
                    await self._update_last_seen_uid(account.id, folder, uid)
                    await self._upsert_cache(account, raw_message, folder, uid, nylas_message.thread_id)

            self._logger.info(f"Processed new messages for {account.email}:{folder}")
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
