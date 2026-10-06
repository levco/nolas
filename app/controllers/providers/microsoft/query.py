import base64
from datetime import UTC, datetime

from app.controllers.providers.base import ListMessagesParams


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_graph_filter(params: ListMessagesParams) -> str | None:
    """Translate Nylas messages.list params to a Microsoft Graph $filter expression.

    Message crawling passes a Graph $filter, optionally prefixed with $filter=
    as in the Nylas API. Thread text searches use $search separately.
    """
    if params.search_query_native:
        return params.search_query_native.removeprefix("$filter=")

    clauses: list[str] = ["isDraft eq false"]

    if params.thread_id:
        escaped = params.thread_id.replace("'", "''")
        clauses.append(f"conversationId eq '{escaped}'")
    if params.from_:
        escaped = params.from_.replace("'", "''")
        clauses.append(f"from/emailAddress/address eq '{escaped}'")
    if params.subject:
        escaped = params.subject.replace("'", "''")
        clauses.append(f"subject eq '{escaped}'")
    if params.unread is not None:
        clauses.append(f"isRead eq {'false' if params.unread else 'true'}")
    if params.received_after is not None:
        clauses.append(f"receivedDateTime ge {_iso(params.received_after)}")
    if params.received_before is not None:
        clauses.append(f"receivedDateTime le {_iso(params.received_before)}")

    return " and ".join(clauses)


def build_graph_search(params: ListMessagesParams) -> str | None:
    """$search expression for any_email (participants), which $filter cannot express."""
    if not params.any_email:
        return None
    return " OR ".join(f"participants:{email}" for email in params.any_email)


def encode_cursor(next_link: str) -> str:
    return base64.urlsafe_b64encode(next_link.encode()).decode()


def decode_cursor(cursor: str) -> str:
    return base64.urlsafe_b64decode(cursor.encode()).decode()
