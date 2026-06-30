from app.controllers.providers.base import ListMessagesParams, ListThreadsParams


def build_gmail_query(params: ListMessagesParams) -> str:
    """Translate Nylas messages.list params to a Gmail search query (q=).

    thread_id is handled separately (threads.get), not via q.
    """
    clauses: list[str] = []

    if params.search_query_native:
        clauses.append(params.search_query_native)

    if params.from_:
        clauses.append(f"from:{params.from_}")

    if params.in_:
        clauses.append(f"in:{params.in_}")

    if params.any_email:
        ors = " ".join(f"from:{email} to:{email} cc:{email} bcc:{email}" for email in params.any_email)
        clauses.append("{" + ors + "}")

    if params.subject:
        escaped = params.subject.replace('"', '\\"')
        clauses.append(f'subject:"{escaped}"')

    # Gmail accepts epoch seconds for after:/before:.
    if params.received_after is not None:
        clauses.append(f"after:{params.received_after}")
    if params.received_before is not None:
        clauses.append(f"before:{params.received_before}")

    # Nylas does not return drafts from messages.list; neither do we.
    clauses.append("-in:draft")

    return " ".join(clauses)


def build_gmail_thread_query(params: ListThreadsParams) -> str:
    """Translate Nylas threads.list params to a Gmail search query (q=)."""
    clauses: list[str] = []

    if params.search_query_native:
        clauses.append(params.search_query_native)

    if params.from_:
        clauses.append(f"from:{params.from_}")
    if params.to:
        clauses.append(f"to:{params.to}")
    if params.cc:
        clauses.append(f"cc:{params.cc}")
    if params.bcc:
        clauses.append(f"bcc:{params.bcc}")
    if params.in_:
        clauses.append(f"in:{params.in_}")

    if params.any_email:
        ors = " ".join(f"from:{email} to:{email} cc:{email} bcc:{email}" for email in params.any_email)
        clauses.append("{" + ors + "}")

    if params.subject:
        escaped = params.subject.replace('"', '\\"')
        clauses.append(f'subject:"{escaped}"')
    if params.latest_message_after is not None:
        clauses.append(f"after:{params.latest_message_after}")
    if params.latest_message_before is not None:
        clauses.append(f"before:{params.latest_message_before}")
    if params.unread is not None:
        clauses.append("is:unread" if params.unread else "-is:unread")
    if params.starred is not None:
        clauses.append("is:starred" if params.starred else "-is:starred")
    if params.has_attachment is not None:
        clauses.append("has:attachment" if params.has_attachment else "-has:attachment")

    clauses.append("-in:draft")
    return " ".join(clauses)
