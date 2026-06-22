import base64
import uuid

from app.controllers.providers.google.mapper import (
    extract_gmail_attachments,
    gmail_label_name,
    map_gmail_message,
)

GRANT_ID = uuid.uuid4()


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _gmail_message() -> dict:
    return {
        "id": "18d5a4b2c3e4f567",
        "threadId": "18d5a4b2c3e4f000",
        "labelIds": ["INBOX", "UNREAD", "IMPORTANT"],
        "snippet": "Hello there",
        "internalDate": "1717920000000",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "Subject", "value": "Deal update"},
                {"name": "From", "value": "Jane Doe <jane@example.com>"},
                {"name": "To", "value": "John <john@lev.co>, other@lev.co"},
                {"name": "Cc", "value": "cc@lev.co"},
                {"name": "Message-ID", "value": "<abc@mail.gmail.com>"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": _b64("plain body")},
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": _b64("<p>html body</p>")},
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "term-sheet.pdf",
                    "headers": [{"name": "Content-Disposition", "value": "attachment; filename=term-sheet.pdf"}],
                    "body": {"attachmentId": "att-123", "size": 2048},
                },
            ],
        },
    }


class TestMapGmailMessage:
    def test_basic_fields(self) -> None:
        message = map_gmail_message(_gmail_message(), GRANT_ID)
        assert message.id == "18d5a4b2c3e4f567"
        assert message.thread_id == "18d5a4b2c3e4f000"
        assert message.grant_id == str(GRANT_ID)
        assert message.subject == "Deal update"
        assert message.body == "<p>html body</p>"
        assert message.date == 1717920000
        assert message.unread is True
        assert message.starred is False
        assert message.object == "message"

    def test_addresses(self) -> None:
        message = map_gmail_message(_gmail_message(), GRANT_ID)
        assert message.from_[0].email == "jane@example.com"
        assert message.from_[0].name == "Jane Doe"
        assert [a.email for a in message.to] == ["john@lev.co", "other@lev.co"]
        assert message.cc[0].email == "cc@lev.co"

    def test_folders_exclude_flag_labels(self) -> None:
        message = map_gmail_message(_gmail_message(), GRANT_ID)
        assert message.folders == ["INBOX"]

    def test_attachments(self) -> None:
        attachments = extract_gmail_attachments(_gmail_message()["payload"])
        assert len(attachments) == 1
        assert attachments[0].id == "att-123"
        assert attachments[0].filename == "term-sheet.pdf"
        assert attachments[0].size == 2048
        assert attachments[0].is_inline is False

    def test_headers_included_when_requested(self) -> None:
        message = map_gmail_message(_gmail_message(), GRANT_ID, include_headers=True)
        assert message.headers is not None
        header_map = {h.name: h.value for h in message.headers}
        assert header_map["Message-ID"] == "<abc@mail.gmail.com>"

    def test_headers_omitted_by_default(self) -> None:
        message = map_gmail_message(_gmail_message(), GRANT_ID)
        assert message.headers is None

    def test_inline_image(self) -> None:
        raw = _gmail_message()
        raw["payload"]["parts"].append(
            {
                "mimeType": "image/png",
                "filename": "logo.png",
                "headers": [
                    {"name": "Content-ID", "value": "<logo@cid>"},
                    {"name": "Content-Disposition", "value": "inline; filename=logo.png"},
                ],
                "body": {"attachmentId": "att-inline", "size": 100},
            }
        )
        attachments = extract_gmail_attachments(raw["payload"])
        inline = [a for a in attachments if a.id == "att-inline"][0]
        assert inline.is_inline is True
        assert inline.content_id == "logo@cid"


class TestGmailLabelName:
    def test_system_labels(self) -> None:
        assert gmail_label_name("SENT") == "Sent"
        assert gmail_label_name("DRAFT") == "Drafts"
        assert gmail_label_name("SPAM") == "Junk"
        assert gmail_label_name("CATEGORY_PROMOTIONS") == "Promotion"

    def test_custom_label_uses_raw_name(self) -> None:
        assert gmail_label_name("Label_123", "Deals") == "Deals"
