"""
Tests for the send-message multipart parser, covering compatibility with the official
Nylas Python SDK's `messages.send()` request shape as well as the legacy hand-rolled caller format.
"""

import json

import pytest
from starlette.requests import Request

from app.api.v3.grants.messages import _parse_multipart_request


def _build_multipart_request(
    boundary: str,
    fields: list[tuple[str, str]],
    files: list[tuple[str, str, str, bytes]],
) -> Request:
    """Build a Starlette Request wrapping a hand-crafted multipart/form-data body.

    `fields` is a list of (name, value) plain form fields.
    `files` is a list of (name, filename, content_type, content) file parts.
    """
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n'
            f"Content-Type: application/json\r\n\r\n{value}\r\n".encode()
        )
    for name, filename, content_type, content in files:
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n".encode() + content + b"\r\n"
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    body = b"".join(chunks)

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "headers": [(b"content-type", f"multipart/form-data; boundary={boundary}".encode())],
    }
    return Request(scope, receive)


def _message_json(**overrides: object) -> str:
    payload: dict[str, object] = {
        "to": [{"email": "to@example.com"}],
        "subject": "Subject",
        "body": "Body",
    }
    payload.update(overrides)
    return json.dumps(payload)


class TestParseMultipartRequestSdkFormat:
    """The official Nylas SDK's multipart format: lowercase "message" envelope, and per-attachment
    field names of `content_id` (inline) or `file{N}` (non-inline)."""

    @pytest.mark.asyncio
    async def test_lowercase_message_field_and_file_n_attachments(self) -> None:
        request = _build_multipart_request(
            "b1",
            fields=[("message", _message_json())],
            files=[("file0", "report.pdf", "application/pdf", b"%PDF-1.4")],
        )

        message_data, attachments = await _parse_multipart_request(request)

        assert message_data.subject == "Subject"
        assert len(attachments) == 1
        attachment = attachments[0]
        assert attachment.filename == "report.pdf"
        assert attachment.content_type == "application/pdf"
        assert attachment.data == b"%PDF-1.4"
        assert attachment.is_inline is False
        assert attachment.content_id is None
        assert attachment.content_disposition is None

    @pytest.mark.asyncio
    async def test_content_id_named_field_is_treated_as_inline(self) -> None:
        request = _build_multipart_request(
            "b2",
            fields=[("message", _message_json())],
            files=[("logo123", "logo.png", "image/png", b"\x89PNG\r\n")],
        )

        _, attachments = await _parse_multipart_request(request)

        assert len(attachments) == 1
        attachment = attachments[0]
        assert attachment.content_id == "logo123"
        assert attachment.is_inline is True
        assert attachment.content_disposition == "inline"
        assert attachment.content_type == "image/png"

    @pytest.mark.asyncio
    async def test_content_type_read_from_part_header_not_guessed_from_filename(self) -> None:
        # Filename extension (.bin) would guess to application/octet-stream via mimetypes;
        # the actual declared part content-type must win instead.
        request = _build_multipart_request(
            "b3",
            fields=[("message", _message_json())],
            files=[("file0", "data.bin", "application/pdf", b"%PDF-1.4")],
        )

        _, attachments = await _parse_multipart_request(request)

        assert attachments[0].content_type == "application/pdf"

    @pytest.mark.asyncio
    async def test_multiple_attachments_mixed_inline_and_regular(self) -> None:
        request = _build_multipart_request(
            "b4",
            fields=[("message", _message_json())],
            files=[
                ("file0", "report.pdf", "application/pdf", b"%PDF-1.4"),
                ("cid-logo", "logo.png", "image/png", b"\x89PNG\r\n"),
            ],
        )

        _, attachments = await _parse_multipart_request(request)

        by_filename = {a.filename: a for a in attachments}
        assert by_filename["report.pdf"].is_inline is False
        assert by_filename["logo.png"].is_inline is True
        assert by_filename["logo.png"].content_id == "cid-logo"


class TestParseMultipartRequestLegacyFormat:
    """Backward compatibility with the old hand-rolled caller: capital "Message" envelope field
    and repeated "Attachment" fields for files."""

    @pytest.mark.asyncio
    async def test_capital_message_field_and_attachment_fields(self) -> None:
        request = _build_multipart_request(
            "b5",
            fields=[("Message", _message_json())],
            files=[("Attachment", "report.pdf", "application/pdf", b"%PDF-1.4")],
        )

        message_data, attachments = await _parse_multipart_request(request)

        assert message_data.subject == "Subject"
        assert len(attachments) == 1
        assert attachments[0].filename == "report.pdf"
        assert attachments[0].is_inline is False
        assert attachments[0].content_id is None

    @pytest.mark.asyncio
    async def test_repeated_attachment_field_name(self) -> None:
        request = _build_multipart_request(
            "b6",
            fields=[("Message", _message_json())],
            files=[
                ("Attachment", "a.pdf", "application/pdf", b"AAAA"),
                ("Attachment", "b.pdf", "application/pdf", b"BBBB"),
            ],
        )

        _, attachments = await _parse_multipart_request(request)

        assert {a.filename for a in attachments} == {"a.pdf", "b.pdf"}
        assert all(a.is_inline is False for a in attachments)


class TestParseMultipartRequestErrors:
    @pytest.mark.asyncio
    async def test_missing_message_field_raises(self) -> None:
        request = _build_multipart_request(
            "b7",
            fields=[],
            files=[("file0", "report.pdf", "application/pdf", b"%PDF-1.4")],
        )

        with pytest.raises(ValueError, match="message"):
            await _parse_multipart_request(request)
