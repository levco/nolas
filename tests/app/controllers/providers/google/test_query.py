from app.controllers.providers.base import ListMessagesParams, ListThreadsParams
from app.controllers.providers.google.query import build_gmail_query, build_gmail_thread_query


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


class TestBuildGmailThreadQuery:
    def test_excludes_drafts_always(self) -> None:
        assert "-in:draft" in build_gmail_thread_query(ListThreadsParams())

    def test_combines_common_filters(self) -> None:
        query = build_gmail_thread_query(
            ListThreadsParams(
                from_="from@example.com",
                to="to@example.com",
                cc="cc@example.com",
                bcc="bcc@example.com",
                in_="inbox",
                subject='Deal "A"',
                latest_message_after=1700000000,
                latest_message_before=1710000000,
                unread=True,
                starred=False,
                has_attachment=True,
            )
        )
        assert "from:from@example.com" in query
        assert "to:to@example.com" in query
        assert "cc:cc@example.com" in query
        assert "bcc:bcc@example.com" in query
        assert "in:inbox" in query
        assert 'subject:"Deal \\"A\\""' in query
        assert "after:1700000000" in query
        assert "before:1710000000" in query
        assert "is:unread" in query
        assert "-is:starred" in query
        assert "has:attachment" in query

    def test_any_email_builds_or_group(self) -> None:
        query = build_gmail_thread_query(ListThreadsParams(any_email=["a@x.com", "b@y.com"]))
        assert query.count("{") == 1
        for email in ("a@x.com", "b@y.com"):
            for field in ("from", "to", "cc", "bcc"):
                assert f"{field}:{email}" in query
