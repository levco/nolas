import asyncio
import uuid
from unittest.mock import AsyncMock, Mock

import pytest

from app.api.payloads.labels import CreateLabelRequest, UpdateLabelRequest
from app.api.payloads.messages import EmailAddress
from app.controllers.providers.base import ListMessagesParams, ListThreadsParams
from app.controllers.providers.exceptions import ProviderError, ProviderNotFoundError
from app.controllers.providers.google.gmail_client import GMAIL_API_BASE, GmailClient
from app.controllers.providers.google.mapper import map_gmail_message
from app.controllers.providers.imap_adapter import ImapProviderAdapter
from app.controllers.providers.microsoft.graph_client import GRAPH_API_BASE, IMMUTABLE_ID_HEADER, GraphClient
from app.controllers.providers.microsoft.mapper import map_graph_message
from app.models.account import Account


@pytest.fixture
def account() -> Account:
    return Account(uuid=uuid.uuid4(), email="owner@example.com")


def test_gmail_list_fetches_custom_label_colors(account: Account) -> None:
    http = Mock(
        request=AsyncMock(
            side_effect=[
                {"labels": [{"id": "INBOX", "type": "system"}, {"id": "Label_1", "name": "Deals", "type": "user"}]},
                {
                    "id": "Label_1",
                    "name": "Deals",
                    "type": "user",
                    "color": {"backgroundColor": "#fb4c2f", "textColor": "#ffffff"},
                },
            ]
        )
    )

    labels = asyncio.run(GmailClient(http).list_labels(account))

    assert labels[0].name == "Inbox"
    assert labels[0].system_label is True
    assert labels[1].color == "#fb4c2f"
    assert labels[1].text_color == "#ffffff"
    assert labels[1].grant_id == str(account.uuid)
    assert http.request.await_count == 2


def test_gmail_create_label_maps_colors(account: Account) -> None:
    http = Mock(request=AsyncMock(return_value={"id": "Label_1", "name": "Deals"}))

    label = asyncio.run(
        GmailClient(http).create_label(account, CreateLabelRequest(name="Deals", color="#FB4C2F", text_color="#FFFFFF"))
    )

    assert label.id == "Label_1"
    http.request.assert_awaited_once_with(
        account,
        "POST",
        f"{GMAIL_API_BASE}/labels",
        json_body={"name": "Deals", "color": {"backgroundColor": "#fb4c2f", "textColor": "#ffffff"}},
    )


def test_gmail_color_update_preserves_text_color(account: Account) -> None:
    http = Mock(
        request=AsyncMock(
            side_effect=[
                {"id": "Label_1", "name": "Deals", "color": {"backgroundColor": "#ffffff", "textColor": "#434343"}},
                {"id": "Label_1", "name": "Deals", "color": {"backgroundColor": "#fb4c2f", "textColor": "#434343"}},
            ]
        )
    )

    label = asyncio.run(GmailClient(http).update_label(account, "Label_1", UpdateLabelRequest(color="#fb4c2f")))

    assert label.color == "#fb4c2f"
    assert http.request.await_args.kwargs["json_body"] == {
        "color": {"backgroundColor": "#fb4c2f", "textColor": "#434343"}
    }


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_gmail_system_label_is_protected(account: Account, operation: str) -> None:
    http = Mock(request=AsyncMock(return_value={"id": "INBOX", "name": "INBOX", "type": "system"}))
    client = GmailClient(http)
    with pytest.raises(ProviderError, match="system labels"):
        if operation == "update":
            asyncio.run(client.update_label(account, "INBOX", UpdateLabelRequest(name="Changed")))
        else:
            asyncio.run(client.delete_label(account, "INBOX"))
    assert http.request.await_count == 1


def test_gmail_delete_uses_native_label_id(account: Account) -> None:
    http = Mock(request=AsyncMock(side_effect=[{"id": "Label_1", "name": "Deals"}, None]))
    asyncio.run(GmailClient(http).delete_label(account, "Label_1"))
    assert http.request.await_args.args[1:] == ("DELETE", f"{GMAIL_API_BASE}/labels/Label_1")
    assert http.request.await_args.kwargs["expect_json"] is False


def test_gmail_replaces_labels_using_native_differences(account: Account) -> None:
    http = Mock(
        request=AsyncMock(
            side_effect=[
                {"id": "m1", "threadId": "t1", "labelIds": ["INBOX", "UNREAD", "Label_1"], "payload": {}},
                {},
                {"id": "m1", "threadId": "t1", "labelIds": ["INBOX", "UNREAD", "Label_2"], "payload": {}},
            ]
        )
    )

    message = asyncio.run(
        GmailClient(http).update_message_labels(account, "m1", ["INBOX", "UNREAD", "Label_2", "Label_2"])
    )

    assert http.request.await_args_list[1].kwargs["json_body"] == {
        "addLabelIds": ["Label_2"],
        "removeLabelIds": ["Label_1"],
    }
    assert message.labels == ["INBOX", "UNREAD", "Label_2"]
    assert message.folders == ["INBOX", "Label_2"]
    assert message.unread is True


@pytest.mark.parametrize("unread", [True, False])
def test_gmail_read_update_keeps_label_field_in_sync(account: Account, unread: bool) -> None:
    http = Mock(
        request=AsyncMock(
            side_effect=[{"id": "m1", "threadId": "t1", "labelIds": ["INBOX", "UNREAD", "Label_1"], "payload": {}}, {}]
        )
    )
    message = asyncio.run(GmailClient(http).update_message_unread(account, "m1", unread))
    assert message is not None
    assert ("UNREAD" in message.labels) is unread
    assert "Label_1" in message.labels


def test_graph_list_pages_categories_and_uses_names_as_ids(account: Account) -> None:
    http = Mock(
        request=AsyncMock(
            side_effect=[
                {
                    "value": [{"id": "native-1", "displayName": "Deals/2026", "color": "preset0"}],
                    "@odata.nextLink": f"{GRAPH_API_BASE}/me/outlook/masterCategories?$skip=1",
                },
                {"value": [{"id": "native-2", "displayName": "Follow up", "color": "none"}]},
            ]
        )
    )
    labels = asyncio.run(GraphClient(http).list_labels(account))
    assert [label.id for label in labels] == ["Deals/2026", "Follow up"]
    assert labels[0].color == "preset0"
    assert labels[0].system_label is False
    assert http.request.await_count == 2


def test_graph_create_label_maps_category(account: Account) -> None:
    http = Mock(request=AsyncMock(return_value={"id": "native-1", "displayName": "Deals", "color": "preset0"}))
    label = asyncio.run(GraphClient(http).create_label(account, CreateLabelRequest(name="Deals", color="Preset0")))
    assert label.id == "Deals"
    assert http.request.await_args.kwargs["json_body"] == {"displayName": "Deals", "color": "preset0"}


def test_graph_color_update_resolves_category_id_and_can_clear_color(account: Account) -> None:
    http = Mock(
        request=AsyncMock(
            side_effect=[
                {"value": [{"id": "native-1", "displayName": "Deals", "color": "preset0"}]},
                {"id": "native-1", "displayName": "Deals", "color": "none"},
            ]
        )
    )
    label = asyncio.run(GraphClient(http).update_label(account, "Deals", UpdateLabelRequest(color=None)))
    assert label.color == "none"
    assert http.request.await_args.args[1:] == ("PATCH", f"{GRAPH_API_BASE}/me/outlook/masterCategories/native-1")
    assert http.request.await_args.kwargs["json_body"] == {"color": "none"}


def test_graph_delete_resolves_native_category_id(account: Account) -> None:
    http = Mock(
        request=AsyncMock(
            side_effect=[
                {"value": [{"id": "native-1", "displayName": "Deals"}]},
                None,
            ]
        )
    )
    asyncio.run(GraphClient(http).delete_label(account, "Deals"))
    assert http.request.await_args.args[1:] == ("DELETE", f"{GRAPH_API_BASE}/me/outlook/masterCategories/native-1")


@pytest.mark.parametrize("client_type", [GmailClient, GraphClient])
def test_missing_label_raises_not_found(account: Account, client_type: type[GmailClient] | type[GraphClient]) -> None:
    http = Mock(request=AsyncMock(side_effect=ProviderNotFoundError()))
    with pytest.raises(ProviderNotFoundError):
        asyncio.run(client_type(http).get_label(account, "missing"))


def test_graph_rejects_rename_without_writing(account: Account) -> None:
    http = Mock(request=AsyncMock())
    with pytest.raises(ProviderError, match="names cannot be changed"):
        asyncio.run(GraphClient(http).update_label(account, "Deals", UpdateLabelRequest(name="Renamed")))
    http.request.assert_not_awaited()


@pytest.mark.parametrize("color", ["#ff0000", "preset25", "preset-1"])
def test_graph_rejects_invalid_colors(account: Account, color: str) -> None:
    http = Mock(request=AsyncMock())
    with pytest.raises(ProviderError, match="category color"):
        asyncio.run(GraphClient(http).create_label(account, CreateLabelRequest(name="Deals", color=color)))
    http.request.assert_not_awaited()


def test_graph_replaces_categories_without_moving_message(account: Account) -> None:
    http = Mock(
        request=AsyncMock(
            side_effect=[
                {
                    "id": "m1",
                    "conversationId": "t1",
                    "parentFolderId": "inbox",
                    "categories": ["Keep", "Remove"],
                    "@odata.etag": 'W/"version1"',
                },
                None,
            ]
        )
    )
    message = asyncio.run(GraphClient(http).update_message_labels(account, "m1", ["Deals", "Deals"]))
    assert message.labels == ["Deals"]
    assert message.folders == ["inbox"]
    assert http.request.await_args.kwargs["json_body"] == {"categories": ["Deals"]}
    assert http.request.await_args.kwargs["headers"] == {**IMMUTABLE_ID_HEADER, "If-Match": 'W/"version1"'}
    assert "categories" in http.request.await_args_list[0].kwargs["params"]["$select"]


def test_graph_category_conflict_does_not_retry_stale_write(account: Account) -> None:
    http = Mock(
        request=AsyncMock(
            side_effect=[
                {"id": "m1", "categories": ["Keep"], "@odata.etag": 'W/"version1"'},
                ProviderError("Precondition failed", status_code=412),
            ]
        )
    )
    with pytest.raises(ProviderError) as exc:
        asyncio.run(GraphClient(http).update_message_labels(account, "m1", ["Deals"]))
    assert exc.value.status_code == 412
    assert http.request.await_count == 2


def test_graph_missing_version_prevents_category_write(account: Account) -> None:
    http = Mock(request=AsyncMock(return_value={"id": "m1", "categories": ["Keep"]}))
    with pytest.raises(ProviderError, match="message version"):
        asyncio.run(GraphClient(http).update_message_labels(account, "m1", ["Deals"]))
    assert http.request.await_count == 1


@pytest.mark.parametrize("color", ["", "preset0", "#fff", "not-a-color"])
def test_gmail_rejects_invalid_color_format(account: Account, color: str) -> None:
    http = Mock(request=AsyncMock())
    with pytest.raises(ProviderError, match="#RRGGBB"):
        asyncio.run(GmailClient(http).create_label(account, CreateLabelRequest(name="Deals", color=color)))
    http.request.assert_not_awaited()


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("color_field", ["color", "text_color"])
def test_gmail_rejects_non_palette_colors_before_writing(account: Account, operation: str, color_field: str) -> None:
    http = Mock(request=AsyncMock(return_value={"id": "Label_1", "name": "Deals", "type": "user"}))
    client = GmailClient(http)
    changes = {color_field: "#123456"}
    with pytest.raises(ProviderError, match="palette") as exc:
        if operation == "create":
            asyncio.run(client.create_label(account, CreateLabelRequest(name="Deals", **changes)))
        else:
            asyncio.run(client.update_label(account, "Label_1", UpdateLabelRequest(**changes)))

    assert exc.value.status_code == 400
    assert all(call.args[1] == "GET" for call in http.request.await_args_list)


@pytest.mark.parametrize("client_type", [GmailClient, GraphClient])
def test_missing_message_cannot_be_labeled(
    account: Account, client_type: type[GmailClient] | type[GraphClient]
) -> None:
    http = Mock(request=AsyncMock(side_effect=ProviderNotFoundError()))
    with pytest.raises(ProviderNotFoundError):
        asyncio.run(client_type(http).update_message_labels(account, "missing", ["Deals"]))


def test_graph_list_and_thread_requests_select_categories(account: Account) -> None:
    http = Mock(request=AsyncMock(return_value={"value": []}))
    client = GraphClient(http)
    asyncio.run(client.list_messages(account, ListMessagesParams()))
    asyncio.run(client.list_threads(account, ListThreadsParams()))
    assert all("categories" in call.kwargs["params"]["$select"] for call in http.request.await_args_list)


def test_message_payloads_include_labels_without_changing_folders(account: Account) -> None:
    gmail = map_gmail_message({"id": "m1", "labelIds": ["INBOX", "UNREAD", "Label_1"], "payload": {}}, account.uuid)
    graph = map_graph_message({"id": "m2", "parentFolderId": "inbox", "categories": ["Deals"]}, account.uuid)
    assert gmail.model_dump(by_alias=True)["labels"] == ["INBOX", "UNREAD", "Label_1"]
    assert gmail.folders == ["INBOX", "Label_1"]
    assert graph.model_dump(by_alias=True)["labels"] == ["Deals"]
    assert graph.folders == ["inbox"]


def test_imap_labels_are_explicitly_unsupported(account: Account) -> None:
    with pytest.raises(ProviderError, match="not supported") as exc:
        asyncio.run(ImapProviderAdapter(Mock(), Mock()).list_labels(account))
    assert exc.value.status_code == 400


def test_gmail_labels_sent_message_without_reading_or_removing_labels(account: Account) -> None:
    http = Mock(
        request=AsyncMock(
            side_effect=[
                {"id": "sent-message", "threadId": "thread-1", "labelIds": ["SENT"]},
                {"labelIds": ["SENT", "Label_1"]},
            ]
        )
    )
    result = asyncio.run(
        GmailClient(http).send_message(
            account,
            [EmailAddress(name="Recipient", email="to@example.com")],
            "Subject",
            "Body",
            labels=["Label_1", "Label_1"],
        )
    )

    assert result.message_id == "sent-message"
    assert result.labels == ["SENT", "Label_1"]
    assert http.request.await_count == 2
    http.request.assert_awaited_with(
        account, "POST", f"{GMAIL_API_BASE}/messages/sent-message/modify", json_body={"addLabelIds": ["Label_1"]}
    )


@pytest.mark.parametrize("failure", [ProviderError("Label deleted"), TimeoutError("Provider unavailable")])
def test_gmail_label_failure_after_send_returns_sent_message(account: Account, failure: Exception) -> None:
    http = Mock(
        request=AsyncMock(side_effect=[{"id": "sent-message", "threadId": "thread-1", "labelIds": ["SENT"]}, failure])
    )

    result = asyncio.run(GmailClient(http).send_message(account, [], "Subject", "Body", labels=["Label_1"]))

    assert result.message_id == "sent-message"
    assert result.labels == ["SENT"]
    assert http.request.await_count == 2


def test_gmail_send_without_labels_does_not_modify_message(account: Account) -> None:
    http = Mock(request=AsyncMock(return_value={"id": "sent-message", "labelIds": ["SENT"]}))
    result = asyncio.run(GmailClient(http).send_message(account, [], "Subject", "Body"))
    assert result.labels == ["SENT"]
    assert http.request.await_count == 1


@pytest.mark.parametrize("reply_id", [None, "original-message"])
def test_graph_send_sets_categories_on_draft_before_sending(account: Account, reply_id: str | None) -> None:
    draft = {"id": "draft-1", "conversationId": "thread-1", "categories": ["Deals"]}
    http = Mock(request=AsyncMock(side_effect=[draft, draft, None] if reply_id else [draft, None]))

    result = asyncio.run(
        GraphClient(http).send_message(
            account, [], "Subject", "Body", reply_to_message_id=reply_id, labels=["Deals", "Deals"]
        )
    )

    assert result.labels == ["Deals"]
    calls = http.request.await_args_list
    assert len(calls) == (3 if reply_id else 2)
    assert calls[-2].kwargs["json_body"]["categories"] == ["Deals"]
    assert calls[-1].args[1:] == ("POST", f"{GRAPH_API_BASE}/me/messages/draft-1/send")
    assert all(call.kwargs["headers"] == IMMUTABLE_ID_HEADER for call in calls)


def test_imap_rejects_labels_before_sending(account: Account) -> None:
    email_controller = Mock(send_email=AsyncMock())
    with pytest.raises(ProviderError, match="not supported"):
        asyncio.run(
            ImapProviderAdapter(email_controller, Mock()).send_message(account, [], "Subject", "Body", labels=["x"])
        )
    email_controller.send_email.assert_not_awaited()
