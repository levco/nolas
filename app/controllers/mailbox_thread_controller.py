import asyncio
import base64
from dataclasses import asdict, replace
from datetime import datetime, timedelta
import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.api.payloads.threads import Thread
from app.controllers.jobs.payloads import MailboxThreadIndexJobPayload
from app.controllers.providers.base import ListThreadsParams, ListThreadsResult
from app.controllers.providers.exceptions import ProviderError, ProviderNotFoundError
from app.controllers.providers.google.gmail_client import FALLBACK_FETCH_CONCURRENCY
from app.controllers.providers.registry import ProviderRegistry
from app.models.account import Account, AccountProvider, AccountStatus
from app.models.job import JobType
from app.models.mailbox_thread import MailboxThreadIndexPhase, MailboxThreadIndexState
from app.repos.account import AccountRepo
from app.repos.job import JobRepo
from app.repos.mailbox_thread import MailboxThreadRepo
from settings import settings

INDEX_CURSOR_PREFIX = "index:"


def _two_years_ago(now: datetime) -> int:
    try:
        return int(now.replace(year=now.year - 2).timestamp())
    except ValueError:
        return int(now.replace(year=now.year - 2, day=28).timestamp())


class MailboxThreadCursor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str
    date: int
    thread_id: str


class MicrosoftThreadIndexProgress(BaseModel):
    inbox_id: str
    sent_id: str
    delta_links: dict[str, str] = Field(default_factory=dict)
    folder_index: int = Field(default=0, ge=0, le=1)
    next_sync_at: int = 0
    received_after: int | None = None


class MailboxThreadController:
    def __init__(
        self, registry: ProviderRegistry, thread_repo: MailboxThreadRepo, account_repo: AccountRepo, job_repo: JobRepo
    ) -> None:
        self._registry = registry
        self._thread_repo = thread_repo
        self._account_repo = account_repo
        self._job_repo = job_repo

    @staticmethod
    def is_enabled(provider: AccountProvider) -> bool:
        return settings.use_thread_index and provider in {AccountProvider.google, AccountProvider.microsoft}

    @staticmethod
    def history_thread_ids(history: dict[str, Any]) -> list[str]:
        thread_ids: set[str] = set()
        for entry in history.get("history", []):
            messages = list(entry.get("messages", []))
            for field in ("messagesAdded", "messagesDeleted", "labelsAdded", "labelsRemoved"):
                messages.extend(change.get("message", {}) for change in entry.get(field, []))
            thread_ids.update(message["threadId"] for message in messages if message.get("threadId"))
        return sorted(thread_ids)

    async def list_threads(self, account: Account, params: ListThreadsParams) -> ListThreadsResult:
        folders = [params.in_] if isinstance(params.in_, str) else sorted(set(params.in_ or []))
        multiple_folders = len(folders) > 1
        params = replace(params, in_=folders if multiple_folders else folders[0] if folders else None)
        if (
            account.provider not in {AccountProvider.google, AccountProvider.microsoft}
            or params.search_query_native
            or not folders
            or (account.provider == AccountProvider.google and not set(folders) <= {"INBOX", "SENT"})
        ):
            return await self._registry.get_client(account).list_threads(account, self.provider_params(params))
        state = await self._thread_repo.get_state(account.id)
        if account.provider == AccountProvider.microsoft and state is not None:
            if not state.provider_state:
                return await self._registry.get_client(account).list_threads(account, self.provider_params(params))
            progress = MicrosoftThreadIndexProgress.model_validate(state.provider_state)
            aliases = {"inbox": progress.inbox_id, "sent": progress.sent_id, "sentitems": progress.sent_id}
            folders = sorted({aliases.get(folder.lower(), folder) for folder in folders})
            params = replace(params, in_=folders if len(folders) > 1 else folders[0])
            if not set(folders) <= {progress.inbox_id, progress.sent_id}:
                return await self._registry.get_client(account).list_threads(account, self.provider_params(params))
        if state is None:
            if await self._thread_repo.create_state(account.id):
                await self._enqueue(account.id)
        if (
            state is None
            or (params.page_token and not params.page_token.startswith(INDEX_CURSOR_PREFIX))
            or (
                state.phase != MailboxThreadIndexPhase.ready
                and (multiple_folders or not await self._thread_repo.has_two_month_history(account.id))
            )
        ):
            # shortcut: provider pagination keeps its old ordering limits until a refresh uses the index.
            return await self._registry.get_client(account).list_threads(account, self.provider_params(params))
        filters = asdict(params)
        filters.pop("limit")
        filters.pop("page_token")
        scope = hashlib.sha256(json.dumps([account.id, filters], sort_keys=True).encode()).hexdigest()
        after = None
        if params.page_token:
            try:
                cursor = MailboxThreadCursor.model_validate_json(
                    base64.b64decode(params.page_token[len(INDEX_CURSOR_PREFIX) :], altchars=b"-_", validate=True)
                )
                if cursor.scope != scope:
                    raise ValueError("Cursor belongs to another mailbox or filter")
            except (ValueError, ValidationError) as exc:
                raise ProviderError("Invalid thread cursor. Refresh the inbox.", status_code=400) from exc
            after = cursor.date, cursor.thread_id
        refs = await self._thread_repo.page(account.id, params, after)
        page = refs[: params.limit]
        result = [Thread.model_validate(ref.cached_thread) for ref in page]
        next_cursor = None
        if len(refs) > params.limit:
            last = page[-1]
            cursor = MailboxThreadCursor(
                scope=scope, date=int(last.last_message_date.timestamp()), thread_id=last.thread_id
            )
            next_cursor = INDEX_CURSOR_PREFIX + base64.urlsafe_b64encode(cursor.model_dump_json().encode()).decode()
        return ListThreadsResult(threads=result, next_cursor=next_cursor)

    @staticmethod
    def provider_params(params: ListThreadsParams) -> ListThreadsParams:
        if params.page_token and params.page_token.startswith(INDEX_CURSOR_PREFIX):
            if isinstance(params.in_, list):
                raise ProviderError(
                    "Thread index is unavailable for this query. Refresh the email list.", status_code=400
                )
            return replace(params, page_token=None)
        return params

    async def _enqueue(self, account_id: int, thread_ids: list[str] | None = None, *, delay_seconds: int = 5) -> None:
        payload = MailboxThreadIndexJobPayload(account_id=account_id, thread_ids=thread_ids or [])
        if thread_ids:
            await self._job_repo.enqueue(JobType.mailbox_thread_index, payload.model_dump())
        else:
            # Leave provider quota headroom for interactive requests between backfill batches.
            available_at = await self._job_repo.db_now() + timedelta(seconds=delay_seconds)
            await self._job_repo.enqueue(JobType.mailbox_thread_index, payload.model_dump(), available_at=available_at)

    async def rebuild(self, account_id: int) -> None:
        await self._account_repo.acquire_notification_lock(account_id)
        state = await self._thread_repo.get_state(account_id)
        if state is None:
            if not await self._thread_repo.create_state(account_id):
                return
        else:
            await self._thread_repo.reset(state)
        await self._enqueue(account_id)

    async def refresh_threads(self, account: Account, thread_ids: list[str]) -> None:
        await self._account_repo.acquire_notification_lock(account.id)
        state = await self._thread_repo.get_state(account.id)
        if state is not None:
            await self._refresh_threads(account, thread_ids, received_after=await self._index_since(state))

    async def _index_since(self, state: MailboxThreadIndexState) -> int:
        received_after = (state.provider_state or {}).get("received_after")
        if isinstance(received_after, int):
            return received_after
        return _two_years_ago(await self._job_repo.db_now())

    async def _refresh_threads(self, account: Account, thread_ids: list[str], *, received_after: int) -> None:
        client = (
            self._registry.get_graph() if account.provider == AccountProvider.microsoft else self._registry.get_gmail()
        )
        for start in range(0, len(thread_ids), FALLBACK_FETCH_CONCURRENCY):
            batch = thread_ids[start : start + FALLBACK_FETCH_CONCURRENCY]
            messages = await asyncio.gather(*(client.get_thread_messages(account, thread_id) for thread_id in batch))
            for thread_id, thread_messages in zip(batch, messages, strict=True):
                thread_messages = [message for message in thread_messages if message.date >= received_after]
                if account.provider == AccountProvider.microsoft:
                    await self._thread_repo.upsert(account.id, thread_id, thread_messages, provider=account.provider)
                else:
                    await self._thread_repo.upsert(account.id, thread_id, thread_messages)

    async def refresh_microsoft_message(self, account: Account, message_id: str) -> None:
        thread_ids = await self._thread_repo.thread_ids_for_messages(account.id, [message_id])
        await self.refresh_threads(account, thread_ids)

    async def process_index_job(self, payload: MailboxThreadIndexJobPayload) -> None:
        account = await self._account_repo.get_by_id_with_app(payload.account_id)
        if account is None or account.status != AccountStatus.active or not self.is_enabled(account.provider):
            return
        # Share the notification lock so backfill and live changes cannot overwrite each other's progress.
        await self._account_repo.acquire_notification_lock(account.id)
        if payload.thread_ids:
            await self.refresh_threads(account, payload.thread_ids)
            return
        state = await self._thread_repo.get_state(account.id)
        if state is None:
            await self._thread_repo.create_state(account.id)
            state = await self._thread_repo.get_state(account.id)
            if state is None:
                raise ProviderError("Failed to initialize mailbox thread index state.")
        if account.provider == AccountProvider.microsoft:
            await self._process_microsoft_index(account, state)
            return
        if state.phase == MailboxThreadIndexPhase.ready:
            return
        received_after = await self._index_since(state)
        state.provider_state = {"received_after": received_after}
        gmail = self._registry.get_gmail()
        if state.history_id is None:
            profile = await gmail.get_profile(account)
            state.history_id = str(profile["historyId"])
        if state.phase == MailboxThreadIndexPhase.backfill:
            thread_ids, next_token = await gmail.list_thread_ids(
                account, ListThreadsParams(limit=25, page_token=state.page_token, latest_message_after=received_after)
            )
            if next_token and next_token == state.page_token:
                raise ProviderError("Google returned a repeated thread page token.")
            await self._refresh_threads(account, thread_ids, received_after=received_after)
            state.page_token = next_token
            if not next_token:
                state.phase = MailboxThreadIndexPhase.catchup
        else:
            try:
                history = await gmail.list_history(account, state.history_id, state.page_token, limit=50)
            except ProviderNotFoundError:
                await self._thread_repo.reset(state)
                await self._enqueue(account.id)
                return
            thread_ids = self.history_thread_ids(history)
            await self._refresh_threads(account, thread_ids, received_after=received_after)
            next_token = history.get("nextPageToken")
            if next_token and next_token == state.page_token:
                raise ProviderError("Google returned a repeated history page token.")
            state.page_token = next_token
            if not next_token:
                state.phase = MailboxThreadIndexPhase.ready
        await self._thread_repo.flush()
        if state.phase != MailboxThreadIndexPhase.ready:
            await self._enqueue(account.id)

    async def _process_microsoft_index(self, account: Account, state: MailboxThreadIndexState) -> None:
        graph = self._registry.get_graph()
        now = int((await self._job_repo.db_now()).timestamp())
        if state.provider_state:
            progress = MicrosoftThreadIndexProgress.model_validate(state.provider_state)
        else:
            inbox = await graph.get_folder(account, "inbox")
            sent = await graph.get_folder(account, "sentitems")
            if inbox is None or sent is None:
                raise ProviderNotFoundError("Microsoft Inbox or Sent Items folder is missing.")
            progress = MicrosoftThreadIndexProgress(inbox_id=inbox.id, sent_id=sent.id)
        if progress.received_after is None:
            progress.received_after = await self._index_since(state)
            state.provider_state = progress.model_dump(mode="json")
        if (
            state.phase == MailboxThreadIndexPhase.ready
            and progress.folder_index == 0
            and not state.page_token
            and now < progress.next_sync_at
        ):
            return
        folder_id = [progress.inbox_id, progress.sent_id][progress.folder_index]
        cursor = state.page_token or progress.delta_links.get(folder_id)
        try:
            changes = await graph.list_thread_changes(
                account, folder_id, cursor, received_after=progress.received_after
            )
        except ProviderError as exc:
            if exc.status_code in {404, 410} or any(
                code in exc.message for code in ("syncStateNotFound", "InvalidDeltaToken")
            ):
                await self._thread_repo.reset(state)
                await self._enqueue(account.id)
                return
            raise
        next_link = changes.get("@odata.nextLink")
        delta_link = changes.get("@odata.deltaLink")
        if next_link and next_link == cursor:
            raise ProviderError("Microsoft returned a repeated delta page token.")
        if not next_link and not isinstance(delta_link, str):
            raise ProviderError("Microsoft omitted the delta cursor.")
        entries = changes.get("value", [])
        # Tombstones carry only message IDs; the local membership index identifies their threads.
        thread_ids = set(
            await self._thread_repo.thread_ids_for_messages(account.id, [entry["id"] for entry in entries])
        )
        thread_ids.update(entry["conversationId"] for entry in entries if entry.get("conversationId"))
        await self._refresh_threads(account, sorted(thread_ids), received_after=progress.received_after)
        state.page_token = next_link
        delay = 5 if entries else 0
        if not next_link:
            assert isinstance(delta_link, str)
            progress.delta_links[folder_id] = delta_link
            progress.folder_index += 1
            if progress.folder_index == 2:
                progress.folder_index = 0
                if state.phase == MailboxThreadIndexPhase.backfill:
                    state.phase = MailboxThreadIndexPhase.catchup
                else:
                    state.phase = MailboxThreadIndexPhase.ready
                    delay = 300
                    progress.next_sync_at = now + delay
        state.provider_state = progress.model_dump(mode="json")
        await self._thread_repo.flush()
        await self._enqueue(account.id, delay_seconds=delay)
