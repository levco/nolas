import pytest

from app.api.payloads.messages import SendMessageRequest, UpdateMessageRequest


@pytest.mark.parametrize("name", [None, "", "   "])
def test_send_message_defaults_empty_recipient_name_to_email(name: str | None) -> None:
    request = SendMessageRequest.model_validate(
        {
            "to": [{"name": "Recipient", "email": "to@example.com"}],
            "cc": [{"name": name, "email": "cc@example.com"}],
            "subject": "Subject",
            "body": "Body",
        }
    )

    assert request.cc is not None
    assert request.cc[0].name == "cc@example.com"


def test_send_message_defaults_missing_recipient_name_to_email() -> None:
    request = SendMessageRequest.model_validate(
        {
            "to": [{"email": "to@example.com"}],
            "subject": "Subject",
            "body": "Body",
        }
    )

    assert request.to[0].name == "to@example.com"


@pytest.mark.parametrize(("unread", "expected"), [(True, True), (False, False)])
def test_update_message_accepts_unread(unread: bool, expected: bool) -> None:
    assert UpdateMessageRequest(unread=unread).unread is expected
