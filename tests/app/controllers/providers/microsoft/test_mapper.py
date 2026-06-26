import uuid

from app.controllers.providers.microsoft.mapper import graph_folder_name, map_graph_message

GRANT_ID = uuid.uuid4()


def _graph_message() -> dict:
    return {
        "id": "AAMkAGI2TG93AAA=",
        "conversationId": "AAQkAGI2TG93Conversation",
        "subject": "Deal update",
        "bodyPreview": "Hello there",
        "body": {"contentType": "html", "content": "<p>html body</p>"},
        "from": {"emailAddress": {"name": "Jane Doe", "address": "jane@example.com"}},
        "toRecipients": [
            {"emailAddress": {"name": "John", "address": "john@lev.co"}},
            {"emailAddress": {"address": "other@lev.co"}},
        ],
        "ccRecipients": [{"emailAddress": {"name": "CC", "address": "cc@lev.co"}}],
        "receivedDateTime": "2026-06-09T08:00:00Z",
        "isRead": False,
        "isDraft": False,
        "flag": {"flagStatus": "notFlagged"},
        "parentFolderId": "AQMkAGI2InboxFolder",
        "internetMessageId": "<abc@outlook.com>",
        "attachments": [
            {
                "id": "att-graph-1",
                "name": "term-sheet.pdf",
                "contentType": "application/pdf",
                "size": 2048,
                "isInline": False,
            }
        ],
    }


class TestMapGraphMessage:
    def test_basic_fields(self) -> None:
        message = map_graph_message(_graph_message(), GRANT_ID)
        assert message.id == "AAMkAGI2TG93AAA="
        assert message.thread_id == "AAQkAGI2TG93Conversation"
        assert message.subject == "Deal update"
        assert message.body == "<p>html body</p>"
        assert message.snippet == "Hello there"
        assert message.unread is True
        assert message.starred is False
        assert message.folders == ["AQMkAGI2InboxFolder"]
        # 2026-06-09T08:00:00Z
        assert message.date == 1780992000

    def test_addresses(self) -> None:
        message = map_graph_message(_graph_message(), GRANT_ID)
        assert message.from_[0].email == "jane@example.com"
        assert [a.email for a in message.to] == ["john@lev.co", "other@lev.co"]
        assert message.cc[0].email == "cc@lev.co"

    def test_attachments(self) -> None:
        message = map_graph_message(_graph_message(), GRANT_ID)
        assert len(message.attachments) == 1
        assert message.attachments[0].id == "att-graph-1"
        assert message.attachments[0].content_type == "application/pdf"

    def test_headers_synthesized_from_internet_message_id(self) -> None:
        message = map_graph_message(_graph_message(), GRANT_ID, include_headers=True)
        assert message.headers is not None
        header_map = {h.name.lower(): h.value for h in message.headers}
        assert header_map["message-id"] == "<abc@outlook.com>"

    def test_explicit_headers_used_when_present(self) -> None:
        raw = _graph_message()
        raw["internetMessageHeaders"] = [
            {"name": "Message-ID", "value": "<explicit@outlook.com>"},
            {"name": "References", "value": "<parent@outlook.com>"},
        ]
        message = map_graph_message(raw, GRANT_ID, include_headers=True)
        header_map = {h.name.lower(): h.value for h in message.headers or []}
        assert header_map["message-id"] == "<explicit@outlook.com>"
        assert header_map["references"] == "<parent@outlook.com>"


class TestGraphFolderName:
    def test_junk_normalized(self) -> None:
        assert graph_folder_name("Junk Email") == "Junk"

    def test_regular_folder_unchanged(self) -> None:
        assert graph_folder_name("Deleted Items") == "Deleted Items"
        assert graph_folder_name("Inbox") == "Inbox"
