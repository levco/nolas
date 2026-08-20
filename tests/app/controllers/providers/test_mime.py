import email

from app.api.payloads.messages import AttachmentData, EmailAddress
from app.controllers.providers.mime import build_mime_message


class TestBuildMimeMessage:
    def test_basic_html_message(self) -> None:
        raw, message_id = build_mime_message(
            to=[EmailAddress(name="John", email="john@lev.co")],
            subject="Hello",
            body="<p>Hi John</p>",
            from_=[EmailAddress(name="Jane", email="jane@example.com")],
        )
        parsed = email.message_from_bytes(raw)
        assert parsed["Subject"] == "Hello"
        assert "john@lev.co" in parsed["To"]
        assert "jane@example.com" in parsed["From"]
        assert parsed["Message-ID"] == message_id
        html_parts = [p for p in parsed.walk() if p.get_content_type() == "text/html"]
        assert len(html_parts) == 1
        assert "Hi John" in html_parts[0].get_payload(decode=True).decode()

    def test_reply_headers(self) -> None:
        raw, _ = build_mime_message(
            to=[EmailAddress(name="a", email="a@b.co")],
            subject="Re: Hello",
            body="<p>reply</p>",
            in_reply_to="<parent@mail.com>",
            references="<root@mail.com> <parent@mail.com>",
        )
        parsed = email.message_from_bytes(raw)
        assert parsed["In-Reply-To"] == "<parent@mail.com>"
        assert parsed["References"] == "<root@mail.com> <parent@mail.com>"

    def test_attachments(self) -> None:
        raw, _ = build_mime_message(
            to=[EmailAddress(name="a", email="a@b.co")],
            subject="With attachment",
            body="<p>see attached</p>",
            attachments=[AttachmentData(filename="report.pdf", content_type="application/pdf", data=b"%PDF-1.4")],
        )
        parsed = email.message_from_bytes(raw)
        attachment_parts = [p for p in parsed.walk() if "attachment" in str(p.get("Content-Disposition", ""))]
        assert len(attachment_parts) == 1
        assert attachment_parts[0].get_filename() == "report.pdf"
        assert attachment_parts[0].get_payload(decode=True) == b"%PDF-1.4"
        assert attachment_parts[0]["Content-ID"] is None

    def test_inline_cid_image_attachment(self) -> None:
        raw, _ = build_mime_message(
            to=[EmailAddress(name="a", email="a@b.co")],
            subject="With inline image",
            body='<p>see <img src="cid:logo123"></p>',
            attachments=[
                AttachmentData(
                    filename="logo.png",
                    content_type="image/png",
                    data=b"\x89PNG\r\n",
                    content_id="logo123",
                    is_inline=True,
                )
            ],
        )
        parsed = email.message_from_bytes(raw)
        inline_parts = [p for p in parsed.walk() if p.get_content_type() == "image/png"]
        assert len(inline_parts) == 1
        part = inline_parts[0]
        assert part.get_filename() == "logo.png"
        assert part.get_payload(decode=True) == b"\x89PNG\r\n"
        assert part["Content-ID"] == "<logo123>"
        assert "inline" in str(part["Content-Disposition"])

    def test_inline_content_id_already_bracketed(self) -> None:
        raw, _ = build_mime_message(
            to=[EmailAddress(name="a", email="a@b.co")],
            subject="With inline image",
            body="<p>see attached</p>",
            attachments=[
                AttachmentData(
                    filename="logo.png",
                    content_type="image/png",
                    data=b"\x89PNG\r\n",
                    content_id="<logo123>",
                    is_inline=True,
                )
            ],
        )
        parsed = email.message_from_bytes(raw)
        inline_parts = [p for p in parsed.walk() if p.get_content_type() == "image/png"]
        assert inline_parts[0]["Content-ID"] == "<logo123>"

    def test_non_inline_attachment_with_explicit_disposition(self) -> None:
        raw, _ = build_mime_message(
            to=[EmailAddress(name="a", email="a@b.co")],
            subject="With attachment",
            body="<p>see attached</p>",
            attachments=[
                AttachmentData(
                    filename="report.pdf",
                    content_type="application/pdf",
                    data=b"%PDF-1.4",
                    content_disposition="attachment",
                    is_inline=False,
                )
            ],
        )
        parsed = email.message_from_bytes(raw)
        attachment_parts = [p for p in parsed.walk() if "attachment" in str(p.get("Content-Disposition", ""))]
        assert len(attachment_parts) == 1
        assert attachment_parts[0].get_content_type() == "application/pdf"
