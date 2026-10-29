"""Opt-in checks against a local PostgreSQL database; every change is rolled back."""

from collections.abc import Iterator
from datetime import UTC, datetime
import importlib.util
import os
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.controllers.providers.base import ListThreadsParams
from app.models.mailbox_thread import MailboxThread, MailboxThreadIndexPhase, MailboxThreadIndexState
from app.repos.mailbox_thread import MailboxThreadRepo


@pytest.fixture
def session() -> Iterator[Session]:
    raw_url = os.getenv("NOLAS_TEST_DATABASE_URL")
    if not raw_url:
        pytest.skip("Set NOLAS_TEST_DATABASE_URL to run isolated local PostgreSQL checks")
    url = make_url(raw_url)
    assert url.host in {"localhost", "127.0.0.1"}, "This test only writes to a local database"
    engine = sa.create_engine(url)
    try:
        with engine.connect() as connection, connection.begin():
            schema = f"codex_thread_test_{uuid4().hex}"
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
            connection.exec_driver_sql("CREATE TABLE accounts (id BIGINT PRIMARY KEY)")
            connection.exec_driver_sql("INSERT INTO accounts (id) VALUES (1), (2)")
            migration_path = (
                Path(__file__).resolve().parents[2]
                / "migrations/versions/20261029_120000_d1e2f3a4b5c6_add_mailbox_thread_index.py"
            )
            spec = importlib.util.spec_from_file_location("thread_index_migration", migration_path)
            assert spec is not None and spec.loader is not None
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            connection.info["index_migration"] = migration
            # The savepoint lets Session close without committing the outer DDL transaction.
            with Session(bind=connection, join_transaction_mode="create_savepoint") as current_session:
                yield current_session
            connection.rollback()
    finally:
        engine.dispose()


def row(thread_id: str, date: int, account_id: int = 1) -> dict[str, object]:
    return {
        "account_id": account_id,
        "thread_id": thread_id,
        "last_message_id": f"message-{thread_id}",
        "last_message_date": datetime.fromtimestamp(date, UTC),
        "subject": "Subject 100%",
        "folders": ["INBOX", "SENT"],
        "participants": ["contact@example.com"],
        "senders": ["sender@example.com"],
        "recipients": [],
        "cc": [],
        "bcc": [],
        "unread": False,
        "starred": False,
        "has_attachment": False,
        "message_ids": [f"message-{thread_id}"],
    }


@pytest.mark.parametrize("folder", ["INBOX", "SENT", "microsoft-inbox-id", "microsoft-sent-id"])
def test_cross_page_order_ties_and_account_isolation(session: Session, folder: str) -> None:
    # Deliberately insert in provider order, with the newest thread last and another account first.
    session.execute(
        sa.insert(MailboxThread),
        [
            {**row("foreign-account", 999, 2), "folders": [folder]},
            {**row("oldest", 100), "folders": [folder]},
            {**row("b", 200), "folders": [folder]},
            {**row("a", 200), "folders": [folder]},
            {**row("newest", 300), "folders": [folder]},
        ],
    )
    params = ListThreadsParams(in_=folder, limit=2, unread=False, has_attachment=False)
    first = list(session.scalars(MailboxThreadRepo.page_query(1, params)))
    assert [thread.thread_id for thread in first] == ["newest", "b", "a"]
    second = list(session.scalars(MailboxThreadRepo.page_query(1, params, (200, "b"))))
    assert [thread.thread_id for thread in first[:2] + second] == ["newest", "b", "a", "oldest"]


def test_message_tombstone_lookup_is_mailbox_scoped(session: Session) -> None:
    session.execute(sa.insert(MailboxThread), [row("thread", 100), row("foreign", 200, 2)])
    matching = sa.select(MailboxThread.thread_id).where(
        MailboxThread.account_id == 1, MailboxThread.message_ids.overlap(["message-thread", "message-foreign"])
    )
    assert list(session.scalars(matching)) == ["thread"]


def test_literal_subject_and_participant_filters(session: Session) -> None:
    matching = row("matching", 100)
    not_matching = {**row("not-matching", 200), "subject": "Subject 1000"}
    session.execute(sa.insert(MailboxThread), [matching, not_matching])
    params = ListThreadsParams(subject="100%", any_email=["CONTACT@EXAMPLE.COM"], from_="SENDER@EXAMPLE.COM")
    assert [thread.thread_id for thread in session.scalars(MailboxThreadRepo.page_query(1, params))] == ["matching"]


def test_indexed_page_stays_bounded_for_a_large_mailbox(session: Session) -> None:
    session.execute(
        sa.text(
            """
        INSERT INTO mailbox_threads
            (account_id, thread_id, last_message_id, last_message_date, subject,
             folders, participants, senders, recipients, cc, bcc, unread, starred, has_attachment)
        SELECT 1, 'thread-' || n, 'message-' || n, to_timestamp(n), 'Subject',
            ARRAY['INBOX'], ARRAY[]::varchar[], ARRAY[]::varchar[], ARRAY[]::varchar[],
            ARRAY[]::varchar[], ARRAY[]::varchar[], false, false, false
        FROM generate_series(1, 100000) n
    """
        )
    )
    session.execute(sa.text("ANALYZE mailbox_threads"))
    query = MailboxThreadRepo.page_query(1, ListThreadsParams(in_="INBOX", limit=20), (50000, "thread-50000"))
    threads = list(session.scalars(query))
    assert len(threads) == 21 and threads[0].last_message_date == datetime.fromtimestamp(49999, UTC)
    compiled = query.compile(dialect=session.get_bind().dialect)
    plan = (
        session.connection()
        .exec_driver_sql("EXPLAIN (ANALYZE, FORMAT JSON) " + str(compiled), compiled.params)
        .scalar_one()[0]["Plan"]
    )
    assert "ix_mailbox_threads_page" in str(plan)
    assert plan["Actual Rows"] == 21
    assert plan["Plans"][0].get("Rows Removed by Filter", 0) <= 21


def test_timestamp_storage_preserves_time_of_day_across_time_zones(session: Session) -> None:
    epoch = 1730205037
    session.execute(sa.insert(MailboxThread), [row("existing", epoch)])
    session.execute(sa.text("SET LOCAL TIME ZONE 'America/New_York'"))
    restored = session.scalars(sa.select(MailboxThread)).one()
    assert restored.last_message_date == datetime.fromtimestamp(epoch, UTC)
    assert restored.last_message_date.tzinfo is not None
    assert restored.subject == "Subject 100%"


def test_consolidated_migration_downgrade_and_upgrade(session: Session) -> None:
    connection = session.connection()
    migration = connection.info["index_migration"]
    with Operations.context(MigrationContext.configure(connection)):
        migration.downgrade()
        assert session.execute(sa.text("SELECT to_regclass('mailbox_threads')")).scalar_one() is None
        assert session.execute(sa.text("SELECT to_regclass('mailbox_thread_index_states')")).scalar_one() is None
        migration.upgrade()
    columns = sa.inspect(connection).get_columns("mailbox_threads")
    date_type = next(column["type"] for column in columns if column["name"] == "last_message_date")
    assert isinstance(date_type, sa.DateTime) and date_type.timezone is True


def test_timestamp_filters_accept_epoch_query_parameters(session: Session) -> None:
    session.execute(sa.insert(MailboxThread), [row("before", 100), row("matching", 200), row("after", 300)])
    params = ListThreadsParams(latest_message_after=150, latest_message_before=250)
    assert [thread.thread_id for thread in session.scalars(MailboxThreadRepo.page_query(1, params))] == ["matching"]


@pytest.mark.parametrize("phase", list(MailboxThreadIndexPhase))
def test_phase_enum_reads_existing_strings_and_writes_to_the_original_schema(
    session: Session, phase: MailboxThreadIndexPhase
) -> None:
    session.execute(
        sa.text("INSERT INTO mailbox_thread_index_states (account_id, phase) VALUES (1, :phase)"),
        {"phase": phase.value},
    )
    state = session.scalars(sa.select(MailboxThreadIndexState)).one()
    assert state.phase is phase
    updated = (
        MailboxThreadIndexPhase.ready if phase != MailboxThreadIndexPhase.ready else MailboxThreadIndexPhase.backfill
    )
    state.phase = updated
    session.flush()
    session.expire_all()
    assert state.phase is updated
    assert session.execute(sa.text("SELECT phase FROM mailbox_thread_index_states")).scalar_one() == updated.value
    columns = sa.inspect(session.connection()).get_columns("mailbox_thread_index_states")
    phase_type = next(column["type"] for column in columns if column["name"] == "phase")
    assert isinstance(phase_type, sa.String) and phase_type.length == 16
