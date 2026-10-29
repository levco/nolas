from dataclasses import replace
from datetime import UTC, datetime
from typing import NamedTuple
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.api.payloads.messages import EmailAddress, Message, MessageHeader
from app.controllers.jobs.payloads import MailboxThreadIndexJobPayload
from app.controllers.mailbox_thread_controller import MailboxThreadController
from app.controllers.mailbox_thread_controller import MicrosoftThreadIndexProgress
from app.controllers.mailbox_thread_controller import _two_years_ago
from app.controllers.notifications.incoming_controller import IncomingNotificationController
from app.controllers.providers.base import FolderData, ListThreadsParams
from app.controllers.providers.base import ListThreadsResult
from app.controllers.providers.exceptions import ProviderError, ProviderNotFoundError, ProviderRateLimitError
from app.controllers.providers.microsoft.graph_client import GraphClient
from app.controllers.providers.threads import build_threads_from_messages
from app.models.account import Account, AccountProvider, AccountStatus
from app.models.mailbox_thread import MailboxThread, MailboxThreadIndexPhase, MailboxThreadIndexState
from app.repos.mailbox_thread import MailboxThreadRepo


class ControllerFixture(NamedTuple):
    subject: MailboxThreadController
    thread_repo: AsyncMock
    job_repo: AsyncMock
    registry: Mock
    account_repo: AsyncMock


@pytest.fixture
def account() -> Account:
    return Account(id=1, app_id=1, provider=AccountProvider.google, status=AccountStatus.active)


@pytest.fixture
def controller(account: Account) -> ControllerFixture:
    gmail = AsyncMock()
    registry = Mock(get_gmail=Mock(return_value=gmail))
    thread_repo = AsyncMock()
    thread_repo.has_two_month_history.return_value = False
    thread_repo.get_state.return_value = MailboxThreadIndexState(
        account_id=account.id, phase=MailboxThreadIndexPhase.ready
    )
    account_repo = AsyncMock()
    account_repo.get_by_id_with_app.return_value = account
    job_repo = AsyncMock()
    job_repo.db_now.return_value = datetime(2026, 10, 29, tzinfo=UTC)
    subject = MailboxThreadController(registry, thread_repo, account_repo, job_repo)
    return ControllerFixture(subject, thread_repo, job_repo, registry, account_repo)


@pytest.fixture
def microsoft_controller(controller: ControllerFixture, account: Account) -> ControllerFixture:
    account.provider = AccountProvider.microsoft
    controller.registry.get_graph.return_value = AsyncMock()
    controller.thread_repo.thread_ids_for_messages.return_value = []
    state = controller.thread_repo.get_state.return_value
    state.provider_state = MicrosoftThreadIndexProgress(
        inbox_id="inbox-id", sent_id="sent-id", received_after=int(datetime(2024, 10, 29, tzinfo=UTC).timestamp())
    ).model_dump()
    return controller


def message(thread_id: str, date: int) -> Message:
    return Message(
        id=f"message-{thread_id}",
        thread_id=thread_id,
        date=date,
        grant_id="grant",
        subject="Subject",
        body="Body",
        snippet="Preview",
        from_=[],
        starred=False,
        unread=False,
        folders=["INBOX", "SENT"],
        object="message",
    )


def reference(thread_id: str, date: int) -> MailboxThread:
    cached = build_threads_from_messages([message(thread_id, date)])[0]
    cached.latest_draft_or_message.body = ""
    return MailboxThread(
        thread_id=thread_id,
        last_message_id=f"message-{thread_id}",
        last_message_date=datetime.fromtimestamp(date, UTC),
        cached_thread=cached.model_dump(mode="json", by_alias=True),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("folder", ["INBOX", "SENT"])
async def test_google_pages_index_without_any_provider_calls(
    controller: ControllerFixture, account: Account, folder: str
) -> None:
    # Equal timestamps straddle a page boundary; the thread ID breaks ties.
    refs = [reference("a", 300), reference("c", 200), reference("b", 200), reference("d", 100)]
    controller.thread_repo.page.side_effect = [refs[:3], refs[2:]]
    params = ListThreadsParams(in_=folder, limit=2)

    first = await controller.subject.list_threads(account, params)
    second = await controller.subject.list_threads(account, replace(params, page_token=first.next_cursor))

    assert [thread.id for thread in first.threads + second.threads] == ["a", "c", "b", "d"]
    assert first.next_cursor and second.next_cursor is None
    assert controller.thread_repo.page.await_args_list[1].args[-1] == (200, "c")
    assert first.threads[0].snippet == "Preview"
    assert first.threads[0].latest_draft_or_message.body == ""
    controller.registry.get_gmail.assert_not_called()
    controller.registry.get_graph.assert_not_called()
    controller.registry.get_client.assert_not_called()
    controller.job_repo.enqueue.assert_not_awaited()
    controller.thread_repo.has_two_month_history.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("native_search,folder", [("subject:report", "INBOX"), (None, None)])
async def test_other_queries_keep_provider_pagination(
    controller: ControllerFixture, account: Account, native_search: str | None, folder: str | None
) -> None:
    provider = AsyncMock()
    expected = ListThreadsResult([], "provider-cursor")
    provider.list_threads.return_value = expected
    controller.registry.get_client.return_value = provider
    params = ListThreadsParams(in_=folder, search_query_native=native_search)
    assert await controller.subject.list_threads(account, params) is expected
    controller.thread_repo.get_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_index_uses_sent_reply_date_for_an_inbox_thread() -> None:
    repo = MailboxThreadRepo()
    session = AsyncMock()
    received = message("thread", 100).model_copy(update={"id": "received", "folders": ["INBOX"]})
    sent = message("thread", 300).model_copy(update={"id": "sent", "folders": ["SENT"]})
    with patch.object(repo, "_db", Mock(session=session)):
        await repo.upsert(1, "thread", [received, sent])
    values = session.execute.await_args.args[0].compile().params
    assert values["last_message_id"] == "sent"
    assert values["last_message_date"] == datetime.fromtimestamp(300, UTC)
    assert set(values["folders"]) == {"INBOX", "SENT"}
    assert "body" not in values and "credentials" not in values


@pytest.mark.parametrize("field", ["latest_message_after", "latest_message_before"])
def test_out_of_range_epoch_filter_is_a_bad_request(field: str) -> None:
    params = ListThreadsParams()
    setattr(params, field, 10**30)
    with pytest.raises(ProviderError) as error:
        MailboxThreadRepo.page_query(1, params)
    assert error.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["account", "folder"])
async def test_cursor_cannot_be_reused_for_another_mailbox_or_filter(
    controller: ControllerFixture, account: Account, changed: str
) -> None:
    controller.thread_repo.page.return_value = [reference("a", 200), reference("b", 100)]
    first = await controller.subject.list_threads(account, ListThreadsParams(in_="INBOX", limit=1))
    params = ListThreadsParams(in_="SENT" if changed == "folder" else "INBOX", limit=1, page_token=first.next_cursor)
    if changed == "account":
        account = Account(id=2, provider=AccountProvider.google)
    with pytest.raises(ProviderError) as error:
        await controller.subject.list_threads(account, params)
    assert error.value.status_code == 400
    assert controller.thread_repo.page.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [AccountProvider.google, AccountProvider.microsoft])
@pytest.mark.parametrize("phase", [MailboxThreadIndexPhase.backfill, MailboxThreadIndexPhase.catchup])
@pytest.mark.parametrize("has_history", [False, True])
async def test_in_progress_index_uses_cache_once_it_has_two_month_history(
    controller: ControllerFixture,
    account: Account,
    provider: AccountProvider,
    phase: MailboxThreadIndexPhase,
    has_history: bool,
) -> None:
    account.provider = provider
    state = controller.thread_repo.get_state.return_value
    state.phase = phase
    if provider == AccountProvider.microsoft:
        state.provider_state = MicrosoftThreadIndexProgress(inbox_id="inbox-id", sent_id="sent-id").model_dump()
    controller.thread_repo.has_two_month_history.return_value = has_history
    controller.thread_repo.page.return_value = [reference("a", 300), reference("b", 200)]
    provider_client = AsyncMock(list_threads=AsyncMock(return_value=ListThreadsResult([], "provider-next")))
    controller.registry.get_client.return_value = provider_client
    params = ListThreadsParams(in_="INBOX", limit=1)

    result = await controller.subject.list_threads(account, params)

    controller.thread_repo.has_two_month_history.assert_awaited_once_with(account.id)
    assert state.phase == phase
    controller.job_repo.enqueue.assert_not_awaited()
    if has_history:
        assert [thread.id for thread in result.threads] == ["a"]
        assert result.next_cursor and result.next_cursor.startswith("index:")
        controller.thread_repo.page.return_value = [reference("b", 200)]
        second = await controller.subject.list_threads(account, replace(params, page_token=result.next_cursor))
        assert [thread.id for thread in second.threads] == ["b"]
        assert second.next_cursor is None
        assert controller.thread_repo.page.await_args.args[-1] == (300, "a")
        provider_client.list_threads.assert_not_awaited()
    else:
        assert result.next_cursor == "provider-next"
        provider_client.list_threads.assert_awaited_once()
        controller.thread_repo.page.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [AccountProvider.google, AccountProvider.microsoft])
@pytest.mark.parametrize("phase", [None, MailboxThreadIndexPhase.backfill, MailboxThreadIndexPhase.catchup])
async def test_preparing_index_returns_provider_page_and_only_enqueues_once(
    controller: ControllerFixture, account: Account, provider: AccountProvider, phase: MailboxThreadIndexPhase | None
) -> None:
    account.provider = provider
    controller.thread_repo.get_state.return_value = (
        None if phase is None else MailboxThreadIndexState(account_id=account.id, phase=phase)
    )
    controller.thread_repo.create_state.return_value = True
    controller.thread_repo.has_two_month_history.return_value = True
    expected = ListThreadsResult([build_threads_from_messages([message("a", 100)])[0]], "provider-next")
    provider_client = AsyncMock(list_threads=AsyncMock(return_value=expected))
    controller.registry.get_client.return_value = provider_client
    params = ListThreadsParams(in_="INBOX", limit=10, page_token="provider-previous", unread=True)
    assert await controller.subject.list_threads(account, params) is expected
    provider_client.list_threads.assert_awaited_once_with(account, params)
    if phase is None:
        controller.job_repo.enqueue.assert_awaited_once()
    else:
        controller.job_repo.enqueue.assert_not_awaited()
    controller.registry.get_gmail.assert_not_called()
    controller.thread_repo.page.assert_not_awaited()
    controller.thread_repo.has_two_month_history.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [AccountProvider.google, AccountProvider.microsoft])
async def test_provider_pagination_survives_index_becoming_ready(
    controller: ControllerFixture, account: Account, provider: AccountProvider
) -> None:
    account.provider = provider
    state = MailboxThreadIndexState(account_id=account.id, phase=MailboxThreadIndexPhase.backfill)
    controller.thread_repo.get_state.return_value = state
    expected = [ListThreadsResult([], "provider-page-2"), ListThreadsResult([])]
    provider_client = AsyncMock(list_threads=AsyncMock(side_effect=expected))
    controller.registry.get_client.return_value = provider_client
    params = ListThreadsParams(in_="INBOX", limit=10)
    first = await controller.subject.list_threads(account, params)
    state.phase = MailboxThreadIndexPhase.ready
    if provider == AccountProvider.microsoft:
        state.provider_state = MicrosoftThreadIndexProgress(inbox_id="INBOX", sent_id="SENT").model_dump()
    second_params = replace(params, page_token=first.next_cursor)
    second = await controller.subject.list_threads(account, second_params)
    assert first is expected[0] and second is expected[1]
    provider_client.list_threads.assert_awaited_with(account, second_params)
    controller.thread_repo.page.assert_not_awaited()
    # A fresh first page switches to the finished index without contacting the provider.
    controller.thread_repo.page.return_value = []
    refreshed = await controller.subject.list_threads(account, params)
    assert refreshed.threads == []
    controller.thread_repo.page.assert_awaited_once()
    assert provider_client.list_threads.await_count == 2


@pytest.mark.asyncio
async def test_rebuilding_index_restarts_provider_pagination_without_forwarding_index_cursor(
    controller: ControllerFixture, account: Account
) -> None:
    controller.thread_repo.page.return_value = [reference("a", 200), reference("b", 100)]
    params = ListThreadsParams(in_="INBOX", limit=1)
    first = await controller.subject.list_threads(account, params)
    assert first.next_cursor and first.next_cursor.startswith("index:")
    controller.thread_repo.get_state.return_value.phase = MailboxThreadIndexPhase.backfill
    provider_client = AsyncMock(list_threads=AsyncMock(return_value=ListThreadsResult([], "provider-next")))
    controller.registry.get_client.return_value = provider_client
    result = await controller.subject.list_threads(account, replace(params, page_token=first.next_cursor))
    assert result.next_cursor == "provider-next"
    provider_client.list_threads.assert_awaited_once_with(account, params)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,params",
    [
        (AccountProvider.google, ListThreadsParams(in_="INBOX", search_query_native="from:person@example.com")),
        (AccountProvider.google, ListThreadsParams()),
        (AccountProvider.google, ListThreadsParams(in_="custom-label")),
        (AccountProvider.microsoft, ListThreadsParams(in_="custom-folder")),
    ],
)
async def test_provider_fallback_never_receives_an_index_cursor(
    controller: ControllerFixture, account: Account, provider: AccountProvider, params: ListThreadsParams
) -> None:
    account.provider = provider
    if provider == AccountProvider.microsoft:
        controller.thread_repo.get_state.return_value.provider_state = MicrosoftThreadIndexProgress(
            inbox_id="inbox", sent_id="sent"
        ).model_dump()
    provider_client = AsyncMock(list_threads=AsyncMock(return_value=ListThreadsResult([])))
    controller.registry.get_client.return_value = provider_client
    params.page_token = "index:cursor"
    await controller.subject.list_threads(account, params)
    provider_client.list_threads.assert_awaited_once_with(account, replace(params, page_token=None))
    assert params.page_token == "index:cursor"
    controller.thread_repo.page.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_refresh_locks_before_reading_index_state(controller: ControllerFixture, account: Account) -> None:
    async def state_after_lock(account_id: int) -> None:
        controller.account_repo.acquire_notification_lock.assert_awaited_once_with(account_id)

    controller.thread_repo.get_state.side_effect = state_after_lock
    await controller.subject.refresh_threads(account, ["thread"])


@pytest.mark.asyncio
async def test_invalid_index_cursor_never_reaches_provider(controller: ControllerFixture, account: Account) -> None:
    with pytest.raises(ProviderError) as error:
        await controller.subject.list_threads(account, ListThreadsParams(in_="INBOX", page_token="index:malformed!"))
    assert error.value.status_code == 400
    controller.registry.get_client.assert_not_called()
    controller.thread_repo.page.assert_not_awaited()


@pytest.mark.asyncio
async def test_google_returns_cached_head_without_fetching_live_provider_state(
    controller: ControllerFixture, account: Account
) -> None:
    ref = reference("a", 100)
    controller.thread_repo.page.return_value = [ref]
    gmail = controller.registry.get_gmail.return_value
    gmail.get_thread.return_value = build_threads_from_messages([message("a", 500)])[0]
    result = await controller.subject.list_threads(account, ListThreadsParams(in_="INBOX"))
    assert result.threads[0].latest_message_received_date == 100
    assert result.threads[0].model_dump(by_alias=True) == ref.cached_thread
    controller.registry.get_gmail.assert_not_called()
    gmail.get_thread.assert_not_awaited()
    gmail.get_thread_messages.assert_not_awaited()
    controller.job_repo.enqueue.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [AccountProvider.google, AccountProvider.microsoft])
async def test_index_job_initializes_missing_state_and_fetches_its_first_batch(
    controller: ControllerFixture, account: Account, provider: AccountProvider
) -> None:
    account.provider = provider
    state = MailboxThreadIndexState(account_id=account.id, phase=MailboxThreadIndexPhase.backfill)
    controller.thread_repo.get_state.side_effect = [None, state]
    now = int(controller.job_repo.db_now.return_value.timestamp())
    client = AsyncMock()
    client.get_thread_messages.return_value = [message("thread", now)]
    if provider == AccountProvider.google:
        controller.registry.get_gmail.return_value = client
        client.get_profile.return_value = {"historyId": "start"}
        client.list_thread_ids.return_value = (["thread"], "next")
    else:
        controller.registry.get_graph.return_value = client
        client.get_folder.side_effect = [FolderData(id="inbox", name="Inbox"), FolderData(id="sent", name="Sent")]
        client.list_thread_changes.return_value = {
            "value": [{"id": "message-thread", "conversationId": "thread"}],
            "@odata.nextLink": "next",
        }
        controller.thread_repo.thread_ids_for_messages.return_value = []

    await controller.subject.process_index_job(MailboxThreadIndexJobPayload(account_id=account.id))

    controller.account_repo.acquire_notification_lock.assert_awaited_once_with(account.id)
    controller.thread_repo.create_state.assert_awaited_once_with(account.id)
    client.get_thread_messages.assert_awaited_once_with(account, "thread")
    assert controller.thread_repo.upsert.await_args.args == (account.id, "thread", [message("thread", now)])
    assert state.phase == MailboxThreadIndexPhase.backfill and state.page_token == "next"
    controller.job_repo.enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_state_that_cannot_be_initialized_is_retried_instead_of_completed(
    controller: ControllerFixture, account: Account
) -> None:
    controller.thread_repo.get_state.return_value = None
    with pytest.raises(ProviderError, match="Failed to initialize"):
        await controller.subject.process_index_job(MailboxThreadIndexJobPayload(account_id=account.id))
    controller.thread_repo.create_state.assert_awaited_once_with(account.id)
    controller.registry.get_gmail.assert_not_called()
    controller.registry.get_graph.assert_not_called()


@pytest.mark.asyncio
async def test_backfill_resumes_one_batch_then_catches_changes_before_becoming_ready(
    controller: ControllerFixture, account: Account
) -> None:
    state = MailboxThreadIndexState(
        account_id=1, phase=MailboxThreadIndexPhase.backfill, history_id=None, page_token=None
    )
    controller.thread_repo.get_state.return_value = state
    gmail = controller.registry.get_gmail()
    gmail.get_profile.return_value = {"historyId": "start"}
    gmail.list_thread_ids.side_effect = [(["a"], "next"), (["b"], None)]
    gmail.get_thread_messages.side_effect = lambda _, thread_id: [message(thread_id, 100)]
    job = MailboxThreadIndexJobPayload(account_id=account.id)

    await controller.subject.process_index_job(job)
    assert (state.phase, state.page_token, state.history_id) == (MailboxThreadIndexPhase.backfill, "next", "start")
    cutoff = int(datetime(2024, 10, 29, tzinfo=UTC).timestamp())
    assert gmail.list_thread_ids.await_args.args[1].latest_message_after == cutoff
    assert state.provider_state == {"received_after": cutoff}
    assert gmail.list_thread_ids.await_count == 1
    controller.job_repo.db_now.return_value = datetime(2026, 11, 1, tzinfo=UTC)
    await controller.subject.process_index_job(job)
    assert (state.phase, state.page_token) == (MailboxThreadIndexPhase.catchup, None)
    assert gmail.list_thread_ids.await_args.args[1].page_token == "next"
    assert gmail.list_thread_ids.await_args.args[1].latest_message_after == cutoff
    gmail.list_history.return_value = {
        "history": [{"messagesDeleted": [{"message": {"threadId": "a"}}]}],
    }
    gmail.get_thread_messages.return_value = []
    gmail.get_thread_messages.side_effect = None
    await controller.subject.process_index_job(job)
    assert state.phase == MailboxThreadIndexPhase.ready
    controller.thread_repo.upsert.assert_awaited_with(account.id, "a", [])
    assert gmail.get_profile.await_count == 1
    assert controller.job_repo.enqueue.await_count == 2


@pytest.mark.asyncio
async def test_live_google_updates_survive_backfill_and_arrivals_between_catchup_pages(
    controller: ControllerFixture, account: Account
) -> None:
    account.email = "user@example.com"
    account.provider_context = {"history_id": "notification-start"}
    state = MailboxThreadIndexState(
        account_id=account.id, phase=MailboxThreadIndexPhase.backfill, history_id="index-start"
    )
    controller.thread_repo.get_state.return_value = state
    controller.account_repo.get_all_by_email_and_provider.return_value = [account]
    gmail = controller.registry.get_gmail()
    now = int(datetime(2026, 10, 29, tzinfo=UTC).timestamp())
    source = {"existing": [message("existing", now)]}
    indexed: dict[str, list[Message]] = {}

    async def store(account_id: int, thread_id: str, messages: list[Message]) -> None:
        indexed[thread_id] = messages

    controller.thread_repo.upsert.side_effect = store
    gmail.get_thread_messages.side_effect = lambda _, thread_id: source[thread_id]
    gmail.list_thread_ids.side_effect = [(["existing"], "next"), (["existing"], None)]
    incoming = IncomingNotificationController(
        controller.account_repo, AsyncMock(), controller.job_repo, gmail, AsyncMock(), controller.subject
    )
    job = MailboxThreadIndexJobPayload(account_id=account.id)
    await controller.subject.process_index_job(job)

    # A reply arrives before this thread is fetched again by backfill.
    source["existing"] = [message("existing", now + 1)]
    gmail.list_history.return_value = {"history": [{"messages": [{"threadId": "existing"}]}]}
    await incoming.process_google_notification(account.email, "reply")
    assert (state.phase, state.page_token) == (MailboxThreadIndexPhase.backfill, "next")
    assert indexed["existing"][0].date == now + 1
    await controller.subject.process_index_job(job)
    assert (state.phase, state.page_token) == (MailboxThreadIndexPhase.catchup, None)
    assert indexed["existing"][0].date == now + 1

    gmail.list_history.return_value = {"history": [], "nextPageToken": "history-next"}
    await controller.subject.process_index_job(job)
    source["new-thread"] = [message("new-thread", now + 2)]
    gmail.list_history.return_value = {"history": [{"messages": [{"threadId": "new-thread"}]}]}
    await incoming.process_google_notification(account.email, "new-mail")
    assert (state.phase, state.page_token) == (MailboxThreadIndexPhase.catchup, "history-next")
    assert indexed["new-thread"][0].date == now + 2

    # The final catch-up page need not include mail covered by a live notification.
    gmail.list_history.return_value = {"history": []}
    await controller.subject.process_index_job(job)
    assert state.phase == MailboxThreadIndexPhase.ready
    assert {thread: messages[0].date for thread, messages in indexed.items()} == {
        "existing": now + 1,
        "new-thread": now + 2,
    }
    assert controller.account_repo.acquire_notification_lock.await_count == 8


@pytest.mark.asyncio
async def test_expired_history_restarts_backfill_instead_of_publishing_partial_index(
    controller: ControllerFixture, account: Account
) -> None:
    state = MailboxThreadIndexState(account_id=1, phase=MailboxThreadIndexPhase.catchup, history_id="expired")
    controller.thread_repo.get_state.return_value = state
    controller.registry.get_gmail().list_history.side_effect = ProviderNotFoundError()
    await controller.subject.process_index_job(MailboxThreadIndexJobPayload(account_id=account.id))
    controller.thread_repo.reset.assert_awaited_once_with(state)
    controller.job_repo.enqueue.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ProviderError("Temporary failure"), ProviderRateLimitError("Quota exceeded")])
async def test_failed_batch_keeps_its_cursor_for_retry(
    controller: ControllerFixture, account: Account, error: ProviderError
) -> None:
    state = MailboxThreadIndexState(
        account_id=1, phase=MailboxThreadIndexPhase.backfill, history_id="start", page_token="retry-this"
    )
    controller.thread_repo.get_state.return_value = state
    gmail = controller.registry.get_gmail()
    gmail.list_thread_ids.return_value = (["a"], "next")
    gmail.get_thread_messages.side_effect = error
    with pytest.raises(ProviderError):
        await controller.subject.process_index_job(MailboxThreadIndexJobPayload(account_id=account.id))
    assert state.page_token == "retry-this" and state.phase == MailboxThreadIndexPhase.backfill
    controller.job_repo.enqueue.assert_not_awaited()
    gmail.get_thread_messages.side_effect = None
    gmail.get_thread_messages.return_value = [message("a", 100)]
    await controller.subject.process_index_job(MailboxThreadIndexJobPayload(account_id=account.id))
    params = gmail.list_thread_ids.await_args.args[1]
    assert params.page_token == "retry-this" and params.limit == 25
    assert state.page_token == "next" and state.phase == MailboxThreadIndexPhase.backfill
    controller.job_repo.enqueue.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("folder", ["inbox-id", "sent-id", "INBOX", "SENT"])
async def test_microsoft_pages_are_globally_ordered_without_any_provider_calls(
    microsoft_controller: ControllerFixture, account: Account, folder: str
) -> None:
    controller = microsoft_controller
    refs = [reference("a", 300), reference("c", 200), reference("b", 200), reference("d", 100)]
    controller.thread_repo.page.side_effect = [refs[:3], refs[2:]]
    params = ListThreadsParams(in_=folder, limit=2)
    first = await controller.subject.list_threads(account, params)
    second = await controller.subject.list_threads(account, replace(params, page_token=first.next_cursor))
    assert [thread.id for thread in first.threads + second.threads] == ["a", "c", "b", "d"]
    assert first.next_cursor and second.next_cursor is None
    assert controller.thread_repo.page.await_args_list[1].args[-1] == (200, "c")
    assert controller.thread_repo.page.await_args.args[1].in_ == (
        "inbox-id" if "inbox" in folder.lower() else "sent-id"
    )
    controller.registry.get_graph.assert_not_called()
    controller.registry.get_gmail.assert_not_called()
    controller.registry.get_client.assert_not_called()
    controller.job_repo.enqueue.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("folder", ["INBOX", "SENT", "custom-folder"])
@pytest.mark.parametrize("page_token", [None, "provider-next", "index:previous"])
async def test_microsoft_uninitialized_folders_keep_provider_pagination_despite_old_cached_threads(
    microsoft_controller: ControllerFixture, account: Account, folder: str, page_token: str | None
) -> None:
    controller = microsoft_controller
    state = controller.thread_repo.get_state.return_value
    state.phase = MailboxThreadIndexPhase.backfill
    state.provider_state = None
    controller.thread_repo.has_two_month_history.return_value = True
    controller.thread_repo.page.return_value = [reference("old-thread", 100)]
    expected = ListThreadsResult([], "provider-next")
    provider = AsyncMock(list_threads=AsyncMock(return_value=expected))
    controller.registry.get_client.return_value = provider
    params = ListThreadsParams(in_=folder, page_token=page_token)

    assert await controller.subject.list_threads(account, params) is expected

    provider.list_threads.assert_awaited_once_with(
        account, replace(params, page_token=None) if page_token == "index:previous" else params
    )
    controller.thread_repo.page.assert_not_awaited()
    controller.thread_repo.has_two_month_history.assert_not_awaited()
    controller.job_repo.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_microsoft_custom_folders_keep_provider_pagination(
    microsoft_controller: ControllerFixture, account: Account
) -> None:
    controller = microsoft_controller
    provider = AsyncMock()
    provider.list_threads.return_value = ListThreadsResult([])
    controller.registry.get_client.return_value = provider
    params = ListThreadsParams(in_="custom-folder")
    await controller.subject.list_threads(account, params)
    provider.list_threads.assert_awaited_once_with(account, params)
    controller.thread_repo.page.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [AccountProvider.google, AccountProvider.microsoft])
async def test_cache_uses_latest_sent_reply_and_provider_read_state_without_storing_bodies(
    provider: AccountProvider,
) -> None:
    repo = MailboxThreadRepo()
    session = AsyncMock()
    received = message("thread", 100).model_copy(update={"id": "received", "folders": ["inbox-id"], "unread": True})
    sent = message("thread", 300).model_copy(
        update={
            "id": "sent",
            "folders": ["sent-id"],
            "unread": False,
            "headers": [MessageHeader(name="Message-Id", value="<example@example.com>")],
            "from_": [EmailAddress(name="Mailbox Owner", email="owner@example.com")],
            "to": [EmailAddress(name="Contact", email="contact@example.com")],
        }
    )
    with patch.object(repo, "_db", Mock(session=session)):
        await repo.upsert(1, "thread", [received, sent], provider=provider)
    values = session.execute.await_args.args[0].compile().params
    assert values["last_message_id"] == "sent"
    assert values["last_message_date"] == datetime.fromtimestamp(300, UTC)
    assert set(values["folders"]) == {"inbox-id", "sent-id"}
    assert values["message_ids"] == ["sent", "received"]
    assert values["unread"] is (provider == AccountProvider.google)
    cached = values["cached_thread"]
    assert cached["latest_draft_or_message"]["body"] == ""
    assert cached["latest_draft_or_message"]["headers"] is None
    assert cached["latest_draft_or_message"]["from"][0]["name"] == "Mailbox Owner"
    assert cached["latest_draft_or_message"]["to"][0]["name"] == "Contact"
    assert cached["message_ids"] == ["sent", "received"]
    assert cached["snippet"] == "Preview"
    assert cached["earliest_message_date"] == 100
    assert sent.body == "Body"
    assert sent.headers is not None


@pytest.mark.asyncio
async def test_microsoft_backfills_beyond_5000_messages_without_hydrating_old_only_pages(
    microsoft_controller: ControllerFixture, account: Account
) -> None:
    controller = microsoft_controller
    state = controller.thread_repo.get_state.return_value
    state.phase = MailboxThreadIndexPhase.backfill
    now = int(controller.job_repo.db_now.return_value.timestamp())
    entries = [
        {"id": f"m-{index}", "conversationId": f"t-{index}", "receivedDateTime": "2026-10-29T00:00:00Z"}
        for index in range(5000)
    ]
    entries.extend(
        {"id": f"old-{index}", "conversationId": f"old-t-{index}", "receivedDateTime": "2020-01-01T00:00:00Z"}
        for index in range(50)
    )
    entries.append({"id": "m-5000", "conversationId": "t-5000", "receivedDateTime": "2026-10-29T00:00:00Z"})
    pages = [
        {
            "value": entries[start : start + 50],
            (
                "@odata.nextLink" if start + 50 < len(entries) else "@odata.deltaLink"
            ): f"https://graph.microsoft.com/v1.0/page-{start + 50}",
        }
        for start in range(0, len(entries), 50)
    ]
    http = AsyncMock()
    http.request.side_effect = pages
    graph = GraphClient(http)
    controller.registry.get_graph.return_value = graph
    stored: set[str] = set()

    async def store(account_id: int, thread_id: str, messages: list[Message], *, provider: AccountProvider) -> None:
        stored.add(thread_id)

    controller.thread_repo.upsert.side_effect = store
    job = MailboxThreadIndexJobPayload(account_id=account.id)
    with patch.object(
        graph, "get_thread_messages", AsyncMock(side_effect=lambda _, thread_id: [message(thread_id, now)])
    ) as fetch:
        for page_number, _ in enumerate(pages):
            await controller.subject.process_index_job(job)
            assert http.request.await_count == page_number + 1
            if page_number == 100:
                assert fetch.await_count == 5000
                assert controller.job_repo.enqueue.await_args.kwargs["available_at"].timestamp() == now
        assert fetch.await_count == 5001

    assert stored == {f"t-{index}" for index in range(5001)}
    assert "$filter" not in http.request.await_args_list[0].kwargs["params"]
    assert all(call.kwargs["params"] is None for call in http.request.await_args_list[1:])
    assert controller.job_repo.enqueue.await_count == len(pages)
    progress = MicrosoftThreadIndexProgress.model_validate(state.provider_state)
    assert progress.folder_index == 1 and "inbox-id" in progress.delta_links
    assert state.phase == MailboxThreadIndexPhase.backfill and state.page_token is None


@pytest.mark.asyncio
async def test_microsoft_backfill_resumes_catches_up_and_schedules_incremental_sync(
    microsoft_controller: ControllerFixture, account: Account
) -> None:
    controller = microsoft_controller
    state = MailboxThreadIndexState(account_id=1, phase=MailboxThreadIndexPhase.backfill)
    controller.thread_repo.get_state.return_value = state
    graph = controller.registry.get_graph()
    graph.get_folder.side_effect = [FolderData(id="inbox-id", name="Inbox"), FolderData(id="sent-id", name="Sent")]
    graph.list_thread_changes.side_effect = [
        {"value": [{"id": "m1", "conversationId": "a"}], "@odata.nextLink": "inbox-page-2"},
        {"value": [{"id": "m2", "conversationId": "a"}], "@odata.deltaLink": "inbox-delta"},
        {"value": [{"id": "m3", "conversationId": "b"}], "@odata.deltaLink": "sent-delta"},
        {"value": [{"id": "m1", "@removed": {"reason": "deleted"}}], "@odata.deltaLink": "inbox-delta-2"},
        {"value": [], "@odata.deltaLink": "sent-delta-2"},
    ]
    graph.get_thread_messages.return_value = [message("a", 100)]
    job = MailboxThreadIndexJobPayload(account_id=account.id)
    await controller.subject.process_index_job(job)
    assert (state.phase, state.page_token) == (MailboxThreadIndexPhase.backfill, "inbox-page-2")
    cutoff = int(datetime(2024, 10, 29, tzinfo=UTC).timestamp())
    assert graph.list_thread_changes.await_args.kwargs == {"received_after": cutoff}
    await controller.subject.process_index_job(job)
    assert graph.list_thread_changes.await_args.args == (account, "inbox-id", "inbox-page-2")
    await controller.subject.process_index_job(job)
    assert (state.phase, state.page_token) == (MailboxThreadIndexPhase.catchup, None)
    controller.thread_repo.thread_ids_for_messages.return_value = ["a"]
    graph.get_thread_messages.return_value = []
    await controller.subject.process_index_job(job)
    assert graph.list_thread_changes.await_args.args == (account, "inbox-id", "inbox-delta")
    controller.thread_repo.upsert.assert_awaited_with(account.id, "a", [], provider=AccountProvider.microsoft)
    controller.thread_repo.thread_ids_for_messages.return_value = []
    await controller.subject.process_index_job(job)
    assert state.phase == MailboxThreadIndexPhase.ready
    progress = MicrosoftThreadIndexProgress.model_validate(state.provider_state)
    assert progress.delta_links == {"inbox-id": "inbox-delta-2", "sent-id": "sent-delta-2"}
    assert progress.received_after == cutoff
    assert progress.next_sync_at == int(controller.job_repo.db_now.return_value.timestamp()) + 300
    assert graph.get_folder.await_count == 2
    assert controller.job_repo.enqueue.await_args.kwargs["available_at"].timestamp() == progress.next_sync_at
    # An old duplicate job must not create another polling chain before the scheduled time.
    await controller.subject.process_index_job(job)
    assert graph.list_thread_changes.await_count == 5
    assert controller.job_repo.enqueue.await_count == 5
    controller.job_repo.db_now.return_value = datetime.fromtimestamp(progress.next_sync_at, UTC)
    graph.list_thread_changes.side_effect = None
    graph.list_thread_changes.return_value = {"value": [], "@odata.deltaLink": "inbox-delta-3"}
    await controller.subject.process_index_job(job)
    assert state.phase == MailboxThreadIndexPhase.ready
    assert graph.list_thread_changes.await_args.args == (account, "inbox-id", "inbox-delta-2")


@pytest.mark.parametrize(
    "now,expected",
    [
        (datetime(2026, 10, 29, 12, 30, tzinfo=UTC), datetime(2024, 10, 29, 12, 30, tzinfo=UTC)),
        (datetime(2024, 2, 29, 12, 30, tzinfo=UTC), datetime(2022, 2, 28, 12, 30, tzinfo=UTC)),
    ],
)
def test_backfill_cutoff_is_two_calendar_years_including_leap_day(now: datetime, expected: datetime) -> None:
    assert _two_years_ago(now) == int(expected.timestamp())


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [AccountProvider.google, AccountProvider.microsoft])
async def test_index_excludes_old_messages_inside_a_recent_thread(
    controller: ControllerFixture, account: Account, provider: AccountProvider
) -> None:
    account.provider = provider
    state = controller.thread_repo.get_state.return_value
    cutoff = int(datetime(2024, 10, 29, tzinfo=UTC).timestamp())
    state.provider_state = {"received_after": cutoff}
    client = AsyncMock()
    controller.registry.get_gmail.return_value = client
    controller.registry.get_graph.return_value = client
    old = message("thread", cutoff - 1)
    recent = message("thread", cutoff).model_copy(update={"id": "recent"})
    client.get_thread_messages.return_value = [old, recent]
    await controller.subject.refresh_threads(account, ["thread"])
    assert controller.thread_repo.upsert.await_args.args == (account.id, "thread", [recent])
    # Old-only conversations are removed rather than becoming cached out-of-window threads.
    client.get_thread_messages.return_value = [old]
    await controller.subject.refresh_threads(account, ["thread"])
    assert controller.thread_repo.upsert.await_args.args == (account.id, "thread", [])


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 410])
async def test_microsoft_expired_delta_rebuilds_without_publishing_partial_data(
    microsoft_controller: ControllerFixture, account: Account, status: int
) -> None:
    controller = microsoft_controller
    controller.registry.get_graph().list_thread_changes.side_effect = ProviderError("Expired sync", status_code=status)
    await controller.subject.process_index_job(MailboxThreadIndexJobPayload(account_id=account.id))
    controller.thread_repo.reset.assert_awaited_once_with(controller.thread_repo.get_state.return_value)
    controller.job_repo.enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_microsoft_failed_refresh_keeps_delta_cursor_for_retry(
    microsoft_controller: ControllerFixture, account: Account
) -> None:
    controller = microsoft_controller
    state = controller.thread_repo.get_state.return_value
    state.phase, state.page_token = MailboxThreadIndexPhase.backfill, "retry-this"
    before = dict(state.provider_state)
    graph = controller.registry.get_graph()
    graph.list_thread_changes.return_value = {
        "value": [{"id": "message", "conversationId": "thread"}],
        "@odata.nextLink": "next",
    }
    graph.get_thread_messages.side_effect = ProviderError("Temporary failure")
    with pytest.raises(ProviderError):
        await controller.subject.process_index_job(MailboxThreadIndexJobPayload(account_id=account.id))
    assert state.page_token == "retry-this" and state.provider_state == before
    controller.job_repo.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_microsoft_deleted_message_refreshes_its_original_thread(
    microsoft_controller: ControllerFixture, account: Account
) -> None:
    controller = microsoft_controller
    controller.thread_repo.thread_ids_for_messages.return_value = ["thread"]
    graph = controller.registry.get_graph()
    graph.get_thread_messages.return_value = []
    await controller.subject.refresh_microsoft_message(account, "deleted-message")
    controller.thread_repo.thread_ids_for_messages.assert_awaited_once_with(account.id, ["deleted-message"])
    controller.thread_repo.upsert.assert_awaited_once_with(account.id, "thread", [], provider=AccountProvider.microsoft)
