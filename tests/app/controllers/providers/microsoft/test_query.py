from app.controllers.providers.base import ListMessagesParams
from app.controllers.providers.microsoft.query import (
    build_graph_filter,
    build_graph_search,
    decode_cursor,
    encode_cursor,
)


class TestBuildGraphFilter:
    def test_excludes_drafts(self) -> None:
        assert build_graph_filter(ListMessagesParams()) == "isDraft eq false"

    def test_thread_id(self) -> None:
        result = build_graph_filter(ListMessagesParams(thread_id="AAQk123"))
        assert "conversationId eq 'AAQk123'" in result

    def test_from(self) -> None:
        result = build_graph_filter(ListMessagesParams(from_="jane@example.com"))
        assert "from/emailAddress/address eq 'jane@example.com'" in result

    def test_date_range(self) -> None:
        result = build_graph_filter(ListMessagesParams(received_after=1700000000, received_before=1710000000))
        assert "receivedDateTime ge 2023-11-14T22:13:20Z" in result
        assert "receivedDateTime le 2024-03-09T16:00:00Z" in result

    def test_quote_escaping(self) -> None:
        result = build_graph_filter(ListMessagesParams(subject="John's deal"))
        assert "subject eq 'John''s deal'" in result

    def test_unread(self) -> None:
        assert "isRead eq false" in build_graph_filter(ListMessagesParams(unread=True))
        assert "isRead eq true" in build_graph_filter(ListMessagesParams(unread=False))

    def test_native_query_passthrough(self) -> None:
        native = "conversationId eq 'X' and receivedDateTime ge 2024-01-01T00:00:00Z"
        assert build_graph_filter(ListMessagesParams(search_query_native=native)) == native


class TestBuildGraphSearch:
    def test_none_without_any_email(self) -> None:
        assert build_graph_search(ListMessagesParams()) is None

    def test_participants_or(self) -> None:
        result = build_graph_search(ListMessagesParams(any_email=["a@x.com", "b@y.com"]))
        assert result == "participants:a@x.com OR participants:b@y.com"


class TestCursor:
    def test_round_trip(self) -> None:
        link = "https://graph.microsoft.com/v1.0/me/messages?$skip=50&$top=50"
        assert decode_cursor(encode_cursor(link)) == link
