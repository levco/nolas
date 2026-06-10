from app.controllers.providers.base import ListMessagesParams


def build_gmail_query(params: ListMessagesParams) -> str:
    """Translate Nylas messages.list params to a Gmail search query (q=).

    thread_id is handled separately (threads.get), not via q.
    """
    clauses: list[str] = []

    if params.search_query_native:
        clauses.append(params.search_query_native)

    if params.from_:
        clauses.append(f"from:{params.from_}")

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
