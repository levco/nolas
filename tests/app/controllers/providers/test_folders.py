import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from app.controllers.providers.base import ListFoldersParams
from app.controllers.providers.exceptions import ProviderError, ProviderNotFoundError
from app.controllers.providers.google.gmail_client import GMAIL_API_BASE, GmailClient
from app.controllers.providers.imap_adapter import ImapProviderAdapter
from app.controllers.providers.microsoft.graph_client import GRAPH_API_BASE, IMMUTABLE_ID_HEADER, GraphClient
from app.models.account import Account


@pytest.fixture
def account() -> Account:
    return Account(uuid=uuid.uuid4(), email="owner@example.com")


@pytest.mark.asyncio
async def test_gmail_folders_include_system_labels_counts_and_colors(account: Account) -> None:
    labels: dict[str, dict[str, Any]] = {
        "INBOX": {"id": "INBOX", "name": "INBOX", "type": "system", "messagesTotal": 12, "messagesUnread": 3},
        "Label_1": {
            "id": "Label_1",
            "name": "Sent",
            "type": "user",
            "color": {
                "backgroundColor": "#fb4c2f",
                "textColor": "#ffffff",
            },
        },
        "UNREAD": {"id": "UNREAD", "name": "UNREAD", "type": "system"},
    }

    async def request(_: Account, method: str, url: str) -> dict[str, Any]:
        assert method == "GET"
        return {"labels": list(labels.values())} if url == f"{GMAIL_API_BASE}/labels" else labels[url.rsplit("/", 1)[1]]

    http = Mock(request=AsyncMock(side_effect=request))
    folders = await GmailClient(http).list_folders(account, ListFoldersParams())
    assert [folder.id for folder in folders] == ["INBOX", "Label_1", "UNREAD"]
    assert folders[0].name == "Inbox" and folders[0].system_folder
    assert folders[0].attributes == [r"\Inbox"]
    assert (folders[0].total_count, folders[0].unread_count) == (12, 3)
    assert folders[1].attributes == [] and not folders[1].system_folder
    assert folders[1].background_color == "#fb4c2f" and folders[1].text_color == "#ffffff"
    assert all(folder.grant_id == str(account.uuid) for folder in folders)


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [404, 403, 429])
async def test_gmail_skips_deleted_labels_but_propagates_other_errors(account: Account, status_code: int) -> None:
    error = (
        ProviderNotFoundError() if status_code == 404 else ProviderError("Provider failure", status_code=status_code)
    )
    http = Mock(request=AsyncMock(side_effect=[{"labels": [{"id": "Label_1"}]}, error]))
    client = GmailClient(http)
    if status_code == 404:
        assert await client.list_folders(account, ListFoldersParams()) == []
    else:
        with pytest.raises(ProviderError) as exc:
            await client.list_folders(account, ListFoldersParams())
        assert exc.value is error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params", [ListFoldersParams(), ListFoldersParams(single_level=True), ListFoldersParams(parent_id="p/1")]
)
async def test_graph_lists_native_pages_and_flattens_hierarchy(account: Account, params: ListFoldersParams) -> None:
    base = f"{GRAPH_API_BASE}/me/mailFolders"
    initial = f"{base}/p%2F1/childFolders" if params.parent_id else base
    next_link = f"{initial}?$skiptoken=next"
    pages: dict[str, dict[str, Any]] = {
        initial: {
            "value": [
                {
                    "id": "localized",
                    "displayName": "Enviados",
                    "childFolderCount": 1,
                    "totalItemCount": 12,
                    "unreadItemCount": 3,
                }
            ],
            "@odata.nextLink": next_link,
        },
        next_link: {"value": [{"id": "custom", "displayName": "Inbox", "childFolderCount": 0}]},
        f"{base}/localized/childFolders": {
            "value": [{"id": "nested", "displayName": "Deals", "parentFolderId": "localized"}]
        },
    }

    async def request(_: Account, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        assert method == "GET" and kwargs["headers"] == IMMUTABLE_ID_HEADER
        if url == f"{base}/sentitems":
            return {"id": "localized"}
        if url in pages:
            return pages[url]
        raise ProviderNotFoundError()

    http = Mock(request=AsyncMock(side_effect=request))
    folders = await GraphClient(http).list_folders(account, params)
    recursive = not params.single_level and not params.parent_id
    assert [folder.id for folder in folders] == (
        ["localized", "custom", "nested"] if recursive else ["localized", "custom"]
    )
    assert folders[0].attributes == [r"\Sent"]
    assert folders[0].total_count == 12 and folders[0].unread_count == 3 and folders[0].child_count == 1
    assert folders[1].attributes == [] and not folders[1].system_folder
    if recursive:
        assert folders[2].parent_id == "localized"
    assert all(folder.grant_id == str(account.uuid) for folder in folders)


@pytest.mark.asyncio
async def test_graph_escapes_shared_mailbox_and_parent_and_includes_hidden_folders(account: Account) -> None:
    http = Mock(request=AsyncMock(return_value={"value": []}))
    await GraphClient(http).list_folders(
        account,
        ListFoldersParams(
            shared_from="shared+box@example.com",
            parent_id="a/b?#",
            include_hidden_folders=True,
        ),
    )
    call = http.request.await_args
    assert call.args[2] == f"{GRAPH_API_BASE}/users/shared%2Bbox%40example.com/mailFolders/a%2Fb%3F%23/childFolders"
    assert call.kwargs["params"]["includeHiddenFolders"] == "true"
    assert call.kwargs["params"]["$top"] == "200"


@pytest.mark.asyncio
async def test_graph_rejects_pagination_links_to_another_server(account: Account) -> None:
    http = Mock(request=AsyncMock(return_value={"value": [], "@odata.nextLink": "https://attacker.example/folders"}))
    with pytest.raises(ProviderError, match="pagination URL"):
        await GraphClient(http).list_folders(account, ListFoldersParams())
    assert http.request.await_count == 1


@pytest.mark.asyncio
async def test_graph_resolves_system_folders_concurrently(account: Account) -> None:
    base = f"{GRAPH_API_BASE}/me/mailFolders"
    started: set[str] = set()
    all_started = asyncio.Event()

    async def request(_: Account, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        if url == base:
            return {"value": [{"id": "sent-id", "displayName": "Enviados"}]}
        well_known = url.rsplit("/", 1)[1]
        started.add(well_known)
        if len(started) == 6:
            all_started.set()
        await all_started.wait()
        return {"id": "sent-id" if well_known == "sentitems" else well_known}

    client = GraphClient(Mock(request=AsyncMock(side_effect=request)))
    folders = await asyncio.wait_for(client.list_folders(account, ListFoldersParams()), timeout=1)
    assert folders[0].attributes == [r"\Sent"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [403, 429, 503])
async def test_graph_does_not_return_incomplete_system_folder_attributes(account: Account, status_code: int) -> None:
    base = f"{GRAPH_API_BASE}/me/mailFolders"
    error = ProviderError("System folder lookup failed", status_code=status_code)

    async def request(_: Account, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        if url == base:
            return {"value": [{"id": "sent-id", "displayName": "Enviados"}]}
        if url == f"{base}/sentitems":
            raise error
        raise ProviderNotFoundError()

    with pytest.raises(ProviderError) as exc:
        await GraphClient(Mock(request=AsyncMock(side_effect=request))).list_folders(account, ListFoldersParams())
    assert exc.value is error


@pytest.mark.asyncio
async def test_imap_lists_every_folder_and_decodes_names(account: Account) -> None:
    lines = [
        b'(\\HasChildren) "/" INBOX',
        b'(\\Sent) "/" "Enviados"',
        b'(\\Trash) "/" Trash',
        b'() NIL "&ZeVnLIqe-"',
        b'() "/" "Deals/2026"',
        b'() "/" "Escaped \\"quote\\""',
        *[f'() "/" "Folder {index}"'.encode() for index in range(20)],
        b"LIST completed",
    ]
    connection = Mock(list=AsyncMock(return_value=Mock(result="OK", lines=lines)))
    manager = Mock(get_connection_or_fail=AsyncMock(return_value=connection), close_connection=AsyncMock())
    folders = await ImapProviderAdapter(Mock(), manager).list_folders(account, ListFoldersParams())
    assert len(folders) == 26
    assert [folder.id for folder in folders[:6]] == [
        "INBOX",
        "Enviados",
        "Trash",
        "&ZeVnLIqe-",
        "Deals/2026",
        'Escaped "quote"',
    ]
    assert folders[0].attributes == [r"\Inbox"]
    assert folders[1].attributes == [r"\Sent"]
    assert folders[2].attributes == [r"\Trash"]
    assert folders[3].attributes == [] and not folders[3].system_folder
    assert folders[3].name == "日本語"
    manager.close_connection.assert_awaited_once_with(connection, account)


@pytest.mark.asyncio
@pytest.mark.parametrize("mailbox,expected", [(b"123", "123"), (b"00123", "00123"), (b'"123"', "123")])
async def test_imap_numeric_mailboxes_keep_string_ids(account: Account, mailbox: bytes, expected: str) -> None:
    connection = Mock(list=AsyncMock(return_value=Mock(result="OK", lines=[b'() "/" ' + mailbox])))
    manager = Mock(get_connection_or_fail=AsyncMock(return_value=connection), close_connection=AsyncMock())
    folders = await ImapProviderAdapter(Mock(), manager).list_folders(account, ListFoldersParams())
    assert folders[0].id == expected
    assert folders[0].name == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("result,lines", [("NO", []), ("OK", [b"(invalid)"])])
async def test_imap_failures_close_connection_and_do_not_return_fallback_folders(
    account: Account, result: str, lines: list[bytes]
) -> None:
    connection = Mock(list=AsyncMock(return_value=Mock(result=result, lines=lines)))
    manager = Mock(get_connection_or_fail=AsyncMock(return_value=connection), close_connection=AsyncMock())
    with pytest.raises((ProviderError, ValueError)):
        await ImapProviderAdapter(Mock(), manager).list_folders(account, ListFoldersParams())
    manager.close_connection.assert_awaited_once_with(connection, account)
