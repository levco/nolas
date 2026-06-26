from app.controllers.providers.base import ListMessagesParams
from app.controllers.providers.google.query import build_gmail_query


class TestBuildGmailQuery:
    def test_excludes_drafts_always(self) -> None:
        assert "-in:draft" in build_gmail_query(ListMessagesParams())

    def test_from(self) -> None:
        query = build_gmail_query(ListMessagesParams(from_="jane@example.com"))
        assert "from:jane@example.com" in query

    def test_in_folder(self) -> None:
        query = build_gmail_query(ListMessagesParams(in_="SENT"))
        assert "in:SENT" in query

    def test_any_email_builds_or_group(self) -> None:
        query = build_gmail_query(ListMessagesParams(any_email=["a@x.com", "b@y.com"]))
        assert query.count("{") == 1
        for email in ("a@x.com", "b@y.com"):
            for field in ("from", "to", "cc", "bcc"):
                assert f"{field}:{email}" in query

    def test_date_range(self) -> None:
        query = build_gmail_query(ListMessagesParams(received_after=1700000000, received_before=1710000000))
        assert "after:1700000000" in query
        assert "before:1710000000" in query

    def test_subject_quoted(self) -> None:
        query = build_gmail_query(ListMessagesParams(subject='Deal "A"'))
        assert 'subject:"Deal \\"A\\""' in query

    def test_native_query_passthrough(self) -> None:
        query = build_gmail_query(ListMessagesParams(search_query_native="has:attachment"))
        assert "has:attachment" in query
