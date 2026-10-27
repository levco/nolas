import base64
import uuid
from typing import Any

import pytest

from app.controllers.providers.google.mapper import (
    extract_gmail_attachments,
    gmail_label_name,
    map_gmail_message,
)

GRANT_ID = uuid.uuid4()


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _gmail_message() -> dict[str, Any]:
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

    @pytest.mark.parametrize("mime_type", ["text/html", "text/plain"])
    @pytest.mark.parametrize(
        "attached_message",
        [
            {"mimeType": "message/rfc822"},
            {"mimeType": "message/rfc822", "filename": "original.eml"},
            {"mimeType": "message/global"},
            {"mimeType": "multipart/mixed", "filename": "original.eml"},
            {
                "mimeType": "multipart/mixed",
                "headers": [{"name": "content-disposition", "value": "Attachment; filename=original.eml"}],
            },
        ],
    )
    def test_bounce_body_ignores_attached_original(self, mime_type: str, attached_message: dict[str, Any]) -> None:
        raw = _gmail_message()
        body = "<p>550 5.1.1 User unknown</p>" if mime_type == "text/html" else "550 5.1.1 User unknown"
        raw["payload"]["mimeType"] = "multipart/report"
        raw["payload"]["parts"] = [
            {"mimeType": mime_type, "body": {"data": _b64(body)}},
            {"mimeType": "message/delivery-status"},
            {
                **attached_message,
                "parts": [{"mimeType": "text/html", "body": {"data": _b64("<p>Original outreach</p>")}}],
            },
        ]

        assert map_gmail_message(raw, GRANT_ID).body == body

    def test_body_preserves_inline_quoted_history(self) -> None:
        raw = _gmail_message()
        body = '<p>Delivery failed</p><div class="gmail_quote"><blockquote>Original outreach</blockquote></div>'
        raw["payload"]["parts"][1]["body"]["data"] = _b64(body)

        assert map_gmail_message(raw, GRANT_ID).body == body

    def test_body_uses_first_html_part_in_mime_order(self) -> None:
        raw = _gmail_message()
        raw["payload"]["parts"].append({"mimeType": "text/html", "body": {"data": _b64("<p>Later HTML part</p>")}})

        assert map_gmail_message(raw, GRANT_ID).body == "<p>html body</p>"

    def test_body_does_not_fall_back_to_attached_message(self) -> None:
        raw = _gmail_message()
        raw["payload"]["parts"] = [
            {
                "mimeType": "message/rfc822",
                "parts": [{"mimeType": "text/html", "body": {"data": _b64("<p>Original outreach</p>")}}],
            }
        ]

        assert map_gmail_message(raw, GRANT_ID).body == ""

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
        assert attachments[0].id == "term-sheet.pdf"
        assert attachments[0].provider_id == "att-123"
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
        inline = [a for a in attachments if a.id == "logo@cid"][0]
        assert inline.provider_id == "att-inline"
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
