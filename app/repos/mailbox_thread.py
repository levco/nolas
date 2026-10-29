from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from app.api.payloads.messages import Message
from app.controllers.providers.base import ListThreadsParams
from app.controllers.providers.exceptions import ProviderError
from app.controllers.providers.threads import build_threads_from_messages
from app.models.account import AccountProvider
from app.models.mailbox_thread import MailboxThread, MailboxThreadIndexPhase, MailboxThreadIndexState
from app.repos.base import BaseRepo


def _email_datetime(epoch: int) -> datetime:
    try:
        return datetime.fromtimestamp(epoch, UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise ProviderError("Invalid email timestamp.", status_code=400) from exc


class MailboxThreadRepo(BaseRepo[MailboxThread]):
    def __init__(self) -> None:
        super().__init__(MailboxThread)

    async def get_state(self, account_id: int) -> MailboxThreadIndexState | None:
        return cast(
            MailboxThreadIndexState | None,
            await self._db.session.scalar(
                sa.select(MailboxThreadIndexState).where(MailboxThreadIndexState.account_id == account_id)
            ),
        )

    async def has_two_month_history(self, account_id: int) -> bool:
        return bool(
            await self._db.session.scalar(
                sa.select(
                    sa.exists().where(
                        MailboxThread.account_id == account_id,
                        MailboxThread.last_message_date <= sa.func.now() - sa.text("INTERVAL '2 months'"),
                    )
                )
            )
        )

    async def create_state(self, account_id: int) -> bool:
        stmt = (
            insert(MailboxThreadIndexState)
            .values(account_id=account_id, phase=MailboxThreadIndexPhase.backfill)
            .on_conflict_do_nothing(index_elements=["account_id"])
            .returning(MailboxThreadIndexState.id)
        )
        return await self._db.session.scalar(stmt) is not None

    async def reset(self, state: MailboxThreadIndexState) -> None:
        await self._db.session.execute(sa.delete(MailboxThread).where(MailboxThread.account_id == state.account_id))
        state.phase, state.page_token, state.history_id = MailboxThreadIndexPhase.backfill, None, None
        state.provider_state = None

    async def remove(self, account_id: int, thread_id: str) -> None:
        await self._db.session.execute(
            sa.delete(MailboxThread).where(MailboxThread.account_id == account_id, MailboxThread.thread_id == thread_id)
        )

    async def thread_ids_for_messages(self, account_id: int, message_ids: list[str]) -> list[str]:
        if not message_ids:
            return []
        result = await self._db.session.scalars(
            sa.select(MailboxThread.thread_id).where(
                MailboxThread.account_id == account_id, MailboxThread.message_ids.overlap(message_ids)
            )
        )
        return list(result.all())

    async def upsert(
        self,
        account_id: int,
        thread_id: str,
        messages: list[Message],
        *,
        provider: AccountProvider = AccountProvider.google,
    ) -> None:
        if not messages:
            await self.remove(account_id, thread_id)
            return
        thread = build_threads_from_messages(messages, unread_from_latest=provider == AccountProvider.microsoft)[0]
        # Thread lists use previews; bodies and headers are fetched only when opening a message.
        thread.latest_draft_or_message = thread.latest_draft_or_message.model_copy(update={"body": "", "headers": None})
        cached_thread = thread.model_dump(mode="json", by_alias=True)
        values = {
            "account_id": account_id,
            "thread_id": thread_id,
            "last_message_id": thread.latest_draft_or_message.id,
            "last_message_date": _email_datetime(thread.latest_message_received_date),
            "subject": thread.subject,
            "folders": thread.folders,
            "participants": sorted({address.email.lower() for address in thread.participants}),
            "senders": sorted({address.email.lower() for message in messages for address in message.from_}),
            "recipients": sorted({address.email.lower() for message in messages for address in message.to}),
            "cc": sorted({address.email.lower() for message in messages for address in message.cc}),
            "bcc": sorted({address.email.lower() for message in messages for address in message.bcc}),
            "unread": thread.unread,
            "starred": thread.starred,
            "has_attachment": thread.has_attachments,
            "message_ids": thread.message_ids,
            "cached_thread": cached_thread,
        }
        stmt = insert(MailboxThread).values(**values)
        await self._db.session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_mailbox_thread_account_thread",
                set_={key: getattr(stmt.excluded, key) for key in values if key not in {"account_id", "thread_id"}},
            )
        )

    @staticmethod
    def page_query(
        account_id: int, params: ListThreadsParams, after: tuple[int, str] | None = None
    ) -> sa.Select[tuple[MailboxThread]]:
        stmt = sa.select(MailboxThread).where(MailboxThread.account_id == account_id)
        if params.in_:
            stmt = stmt.where(MailboxThread.folders.contains([params.in_]))
        for value, column in (
            (params.from_, MailboxThread.senders),
            (params.to, MailboxThread.recipients),
            (params.cc, MailboxThread.cc),
            (params.bcc, MailboxThread.bcc),
        ):
            if value:
                stmt = stmt.where(column.contains([value.lower()]))
        if params.any_email:
            stmt = stmt.where(MailboxThread.participants.overlap([email.lower() for email in params.any_email]))
        if params.subject:
            stmt = stmt.where(MailboxThread.subject.contains(params.subject, autoescape=True))
        if params.latest_message_after is not None:
            stmt = stmt.where(MailboxThread.last_message_date >= _email_datetime(params.latest_message_after))
        if params.latest_message_before is not None:
            stmt = stmt.where(MailboxThread.last_message_date <= _email_datetime(params.latest_message_before))
        for bool_value, bool_column in (
            (params.unread, MailboxThread.unread),
            (params.starred, MailboxThread.starred),
            (params.has_attachment, MailboxThread.has_attachment),
        ):
            if bool_value is not None:
                stmt = stmt.where(bool_column == bool_value)
        if after is not None:
            date, thread_id = after
            stmt = stmt.where(
                sa.tuple_(MailboxThread.last_message_date, MailboxThread.thread_id) < (_email_datetime(date), thread_id)
            )
        return stmt.order_by(MailboxThread.last_message_date.desc(), MailboxThread.thread_id.desc()).limit(
            params.limit + 1
        )

    async def page(
        self, account_id: int, params: ListThreadsParams, after: tuple[int, str] | None = None
    ) -> Sequence[MailboxThread]:
        result = await self.execute(self.page_query(account_id, params, after))
        return result.all()
