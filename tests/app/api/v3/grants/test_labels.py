import uuid
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.types import Message

from app.api.middlewares.authentication import get_current_app
from app.api.payloads.labels import (
    CreateLabelRequest,
    Label,
    LabelListResponse,
    LabelResponse,
    UpdateLabelRequest,
)
from app.api.payloads.messages import MessageResponse, UpdateMessageRequest
from app.api.v3.grants import labels as labels_api
from app.api.v3.grants import messages as messages_api
from app.controllers.providers.exceptions import ProviderError
from app.controllers.providers.microsoft.mapper import map_graph_message
from app.models.account import Account
from app.models.app import App


@pytest.fixture
def account() -> Account:
    return Account(uuid=uuid.uuid4(), email="owner@example.com")


@pytest.mark.asyncio
async def test_catalog_endpoints_delegate_with_authorized_account(
    monkeypatch: pytest.MonkeyPatch, account: Account
) -> None:
    grant_id = str(account.uuid)
    label = Label(id="Deals/2026", name="Deals/2026", grant_id=grant_id, color="preset0")
    provider = AsyncMock()
    provider.list_labels.return_value = [label]
    provider.get_label.return_value = label
    provider.create_label.return_value = label
    provider.update_label.return_value = label
    registry = Mock(get_client=Mock(return_value=provider))
    validate = AsyncMock(return_value=(account, None))
    monkeypatch.setattr(labels_api, "validate_grant_access", validate)
    app = App(id=1)

    listing = await labels_api.list_labels(grant_id, app=app, registry=registry)
    fetched = await labels_api.get_label(grant_id, label.id, app=app, registry=registry)
    created = await labels_api.create_label(grant_id, CreateLabelRequest(name=label.name), app=app, registry=registry)
    updated = await labels_api.update_label(
        grant_id, label.id, UpdateLabelRequest(color="preset0"), app=app, registry=registry
    )
    deleted = await labels_api.delete_label(grant_id, label.id, app=app, registry=registry)

    assert isinstance(listing, LabelListResponse) and listing.data == [label]
    assert all(isinstance(response, LabelResponse) for response in [fetched, created, updated])
    assert deleted.status_code == 204
    assert validate.await_count == 5
    assert all(call.args == (app.id, grant_id) for call in validate.await_args_list)
    provider.get_label.assert_awaited_once_with(account, label.id)
    provider.delete_label.assert_awaited_once_with(account, label.id)


@pytest.mark.asyncio
async def test_inaccessible_grant_never_reaches_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    denied = JSONResponse(status_code=404, content={"error": "grant.not_found"})
    monkeypatch.setattr(labels_api, "validate_grant_access", AsyncMock(return_value=(None, denied)))
    monkeypatch.setattr(messages_api, "validate_grant_access", AsyncMock(return_value=(None, denied)))
    registry = Mock()
    app = App(id=1)
    responses = [
        await labels_api.list_labels("other-grant", app=app, registry=registry),
        await labels_api.get_label("other-grant", "Deals", app=app, registry=registry),
        await labels_api.create_label("other-grant", CreateLabelRequest(name="Deals"), app=app, registry=registry),
        await labels_api.update_label(
            "other-grant", "Deals", UpdateLabelRequest(color="preset0"), app=app, registry=registry
        ),
        await labels_api.delete_label("other-grant", "Deals", app=app, registry=registry),
        await messages_api.update_message(
            UpdateMessageRequest(labels=["Deals"]), "other-grant", "m1", app=app, registry=registry
        ),
    ]
    assert all(response is denied for response in responses)
    registry.get_client.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 403, 404, 429, 503])
async def test_provider_errors_keep_their_status(
    monkeypatch: pytest.MonkeyPatch, account: Account, status_code: int
) -> None:
    monkeypatch.setattr(labels_api, "validate_grant_access", AsyncMock(return_value=(account, None)))
    provider = AsyncMock()
    provider.list_labels.side_effect = ProviderError("Provider failure", status_code=status_code)
    response = await labels_api.list_labels(
        str(account.uuid), app=App(id=1), registry=Mock(get_client=lambda _: provider)
    )
    assert isinstance(response, JSONResponse)
    assert response.status_code == status_code


@pytest.mark.asyncio
async def test_message_label_changes_return_message_payload(monkeypatch: pytest.MonkeyPatch, account: Account) -> None:
    monkeypatch.setattr(messages_api, "validate_grant_access", AsyncMock(return_value=(account, None)))
    provider = AsyncMock()
    provider.update_message_labels.return_value = map_graph_message(
        {"id": "m1", "parentFolderId": "inbox", "categories": ["Deals"]}, account.uuid
    )
    response = await messages_api.update_message(
        UpdateMessageRequest(labels=["Deals"]),
        str(account.uuid),
        "m1",
        app=App(id=1),
        registry=Mock(get_client=lambda _: provider),
    )
    assert isinstance(response, MessageResponse)
    assert response.data.labels == ["Deals"]
    provider.update_message_labels.assert_awaited_once_with(account, "m1", ["Deals"])
    provider.update_message_unread.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_labels_clear_assignments(monkeypatch: pytest.MonkeyPatch, account: Account) -> None:
    monkeypatch.setattr(messages_api, "validate_grant_access", AsyncMock(return_value=(account, None)))
    provider = AsyncMock()
    provider.update_message_labels.return_value = map_graph_message({"id": "m1"}, account.uuid)
    response = await messages_api.update_message(
        UpdateMessageRequest(labels=[]),
        str(account.uuid),
        "m1",
        app=App(id=1),
        registry=Mock(get_client=lambda _: provider),
    )
    assert isinstance(response, MessageResponse)
    assert response.data.labels == []
    provider.update_message_labels.assert_awaited_once_with(account, "m1", [])
    provider.update_message_unread.assert_not_awaited()


@pytest.mark.asyncio
async def test_combined_update_applies_unread_after_labels(monkeypatch: pytest.MonkeyPatch, account: Account) -> None:
    monkeypatch.setattr(messages_api, "validate_grant_access", AsyncMock(return_value=(account, None)))
    provider = AsyncMock()
    provider.update_message_labels.return_value = map_graph_message({"id": "m1", "categories": ["Deals"]}, account.uuid)
    provider.update_message_unread.return_value = map_graph_message(
        {"id": "m1", "categories": ["Deals"], "isRead": True}, account.uuid
    )
    response = await messages_api.update_message(
        UpdateMessageRequest(labels=["Deals"], unread=False),
        str(account.uuid),
        "m1",
        app=App(id=1),
        registry=Mock(get_client=lambda _: provider),
    )
    assert isinstance(response, MessageResponse)
    assert response.data.unread is False
    assert [call[0] for call in provider.mock_calls] == ["update_message_labels", "update_message_unread"]
    provider.update_message_unread.assert_awaited_once_with(account, "m1", False)


@pytest.mark.asyncio
async def test_unread_only_update_does_not_replace_labels(monkeypatch: pytest.MonkeyPatch, account: Account) -> None:
    monkeypatch.setattr(messages_api, "validate_grant_access", AsyncMock(return_value=(account, None)))
    provider = AsyncMock()
    provider.update_message_unread.return_value = map_graph_message({"id": "m1", "categories": ["Deals"]}, account.uuid)
    response = await messages_api.update_message(
        UpdateMessageRequest(unread=True),
        str(account.uuid),
        "m1",
        app=App(id=1),
        registry=Mock(get_client=lambda _: provider),
    )
    assert isinstance(response, MessageResponse)
    provider.update_message_labels.assert_not_awaited()
    provider.update_message_unread.assert_awaited_once_with(account, "m1", True)


@pytest.mark.parametrize(
    "payload", [{}, {"labels": [""]}, {"labels": [" \t\n"]}, {"labels": None}, {"labels": [], "unread": None}]
)
def test_invalid_message_label_changes_are_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        UpdateMessageRequest.model_validate(payload)


@pytest.mark.parametrize("payload", [{}, {"name": ""}, {"name": "  "}, {"name": None}, {"unknown": "value"}])
def test_invalid_label_updates_are_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        UpdateLabelRequest.model_validate(payload)


@pytest.mark.parametrize("name", ["", "  "])
def test_blank_label_names_are_rejected(name: str) -> None:
    with pytest.raises(ValidationError):
        CreateLabelRequest(name=name)


def test_label_names_are_trimmed_without_changing_internal_spaces() -> None:
    assert CreateLabelRequest(name=" \tDeals  2026\n").name == "Deals  2026"
    assert UpdateLabelRequest(name=" \tDeals  2026\n").name == "Deals  2026"


def test_message_label_ids_are_preserved_exactly() -> None:
    assert UpdateMessageRequest(labels=[" Deals/2026 "]).labels == [" Deals/2026 "]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
@pytest.mark.parametrize("label_id", ["", "   ", "\t"])
async def test_blank_label_path_never_reaches_provider(
    monkeypatch: pytest.MonkeyPatch, method: str, label_id: str
) -> None:
    app = FastAPI()
    app.include_router(labels_api.router, prefix="/labels")
    app.dependency_overrides[get_current_app] = lambda: App(id=1)
    validate = AsyncMock()
    monkeypatch.setattr(labels_api, "validate_grant_access", validate)
    responses: list[Message] = []

    async def send(message: Message) -> None:
        responses.append(message)

    await app(
        {
            "type": "http",
            "method": method,
            "path": f"/labels/{label_id}",
            "query_string": b"grant_id=grant-1",
            "headers": [(b"content-type", b"application/json")],
        },
        AsyncMock(return_value={"type": "http.request", "body": b'{"color": "preset0"}'}),
        send,
    )

    assert responses[0]["status"] == 422
    validate.assert_not_awaited()


def test_routes_are_registered_with_documented_schemas() -> None:
    from app.api.v3.grants import router

    app = FastAPI()
    app.include_router(router, prefix="/v3/grants")
    paths = app.openapi()["paths"]
    assert set(paths["/v3/grants/{grant_id}/labels"]) == {"get", "post"}
    assert set(paths["/v3/grants/{grant_id}/labels/{label_id}"]) == {"get", "put", "delete"}
    assert "put" in paths["/v3/grants/{grant_id}/messages/{message_id}"]
    assert "/v3/grants/{grant_id}/messages/{message_id}/labels" not in paths
