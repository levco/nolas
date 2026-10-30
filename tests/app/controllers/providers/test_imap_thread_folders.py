import uuid
from email.message import EmailMessage
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.controllers.providers.base import ListThreadsParams
from app.controllers.providers.imap_adapter import ImapProviderAdapter
from app.models.account import Account


@pytest.mark.asyncio
@pytest.mark.parametrize("folders", ["Sent", ["Sent"], ["INBOX", "Sent"], ["INBOX", "Trash"], ["Custom"]])
async def test_explicit_thread_folders_bypass_default_discovery(folders: str | list[str]) -> None:
    connection = AsyncMock()
    connection.search.return_value = ("OK", [b""])
    manager = AsyncMock()
    manager.get_connection_or_fail.return_value = connection
    account = Account(id=1, email="owner@example.com")
    adapter = ImapProviderAdapter(Mock(), manager)
    with patch(
        "app.controllers.providers.imap_adapter.FolderUtils.get_account_folders",
        AsyncMock(return_value=["Archive", "INBOX", "Sent"]),
    ) as discover:
        await adapter.list_threads(account, ListThreadsParams(in_=folders))
    expected = [folders] if isinstance(folders, str) else folders
    assert [call.args[1] for call in manager.get_connection_or_fail.await_args_list] == expected
    discover.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_inbox_does_not_hide_newer_sent_thread() -> None:
    def raw_message(identifier: str, date: str) -> bytes:
        message = EmailMessage()
        message["Message-ID"] = f"<{identifier}@example.com>"
        message["From"] = "owner@example.com"
        message["To"] = "contact@example.com"
        message["Subject"] = identifier
        message["Date"] = date
        message.set_content("Hello")
        return message.as_bytes()

    inbox = AsyncMock()
    inbox.search.return_value = ("OK", [b"1 2 3 4 5 6"])
    inbox.fetch.side_effect = [
        ("OK", [raw_message(f"inbox-{uid}", "Mon, 26 Oct 2026 10:00:00 +0000")]) for uid in range(6, 0, -1)
    ]
    sent = AsyncMock()
    sent.search.return_value = ("OK", [b"1"])
    sent.fetch.return_value = ("OK", [raw_message("sent-newest", "Tue, 27 Oct 2026 10:00:00 +0000")])
    manager = AsyncMock()
    manager.get_connection_or_fail.side_effect = [inbox, sent]
    adapter = ImapProviderAdapter(Mock(), manager)
    account = Account(id=1, uuid=uuid.uuid4(), email="owner@example.com")

    result = await adapter.list_threads(account, ListThreadsParams(in_=["INBOX", "Sent"], limit=1))

    assert [thread.subject for thread in result.threads] == ["sent-newest"]
    assert inbox.fetch.await_count == 5
    sent.search.assert_awaited_once()
    assert manager.close_connection.await_count == 2
