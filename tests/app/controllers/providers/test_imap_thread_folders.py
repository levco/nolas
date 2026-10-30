from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.controllers.providers.base import ListThreadsParams
from app.controllers.providers.imap_adapter import ImapProviderAdapter
from app.models.account import Account


@pytest.mark.asyncio
async def test_thread_folder_array_only_searches_selected_mailboxes() -> None:
    connection = AsyncMock()
    connection.search.return_value = ("OK", [b""])
    manager = AsyncMock()
    manager.get_connection_or_fail.return_value = connection
    account = Account(id=1, email="owner@example.com")
    adapter = ImapProviderAdapter(Mock(), manager)
    with patch(
        "app.controllers.providers.imap_adapter.FolderUtils.get_account_folders",
        AsyncMock(return_value=["Archive", "INBOX", "Sent"]),
    ):
        await adapter.list_threads(account, ListThreadsParams(in_=["INBOX", "Sent"]))
    assert [call.args[1] for call in manager.get_connection_or_fail.await_args_list] == ["INBOX", "Sent"]
