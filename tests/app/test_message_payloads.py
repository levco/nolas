import base64

import pytest

from app.api.payloads.messages import SendMessageAttachment, SendMessageRequest, UpdateMessageRequest


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


def test_send_message_request_parses_json_body_attachments() -> None:
    content = base64.b64encode(b"%PDF-1.4").decode()
    request = SendMessageRequest.model_validate(
        {
            "to": [{"email": "to@example.com"}],
            "subject": "Subject",
            "body": "Body",
            "attachments": [
                {
                    "filename": "report.pdf",
                    "content_type": "application/pdf",
                    "content": content,
                    "size": 8,
                }
            ],
        }
    )

    assert request.attachments is not None
    assert len(request.attachments) == 1
    attachment_data = request.attachments[0].to_attachment_data()
    assert attachment_data.filename == "report.pdf"
    assert attachment_data.content_type == "application/pdf"
    assert attachment_data.data == b"%PDF-1.4"
    assert attachment_data.is_inline is False
    assert attachment_data.content_id is None


def test_send_message_json_attachment_decodes_inline_cid_image() -> None:
    content = base64.b64encode(b"\x89PNG\r\n").decode()
    attachment = SendMessageAttachment(
        filename="logo.png",
        content_type="image/png",
        content=content,
        content_id="logo123",
        content_disposition="inline",
        is_inline=True,
    )

    attachment_data = attachment.to_attachment_data()

    assert attachment_data.data == b"\x89PNG\r\n"
    assert attachment_data.content_id == "logo123"
    assert attachment_data.content_disposition == "inline"
    assert attachment_data.is_inline is True


def test_send_message_request_defaults_attachments_to_none() -> None:
    request = SendMessageRequest.model_validate(
        {
            "to": [{"email": "to@example.com"}],
            "subject": "Subject",
            "body": "Body",
        }
    )

    assert request.attachments is None
