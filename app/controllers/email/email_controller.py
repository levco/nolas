import logging

from app.api.models.messages import Message
from app.controllers.imap.message_controller import MessageController
from app.models import Email
from app.models.account import Account
from app.repos.email import EmailRepo


class EmailController:
    """Controller for email operations."""

    def __init__(self, email_repo: EmailRepo, message_controller: MessageController):
        self._logger = logging.getLogger(__name__)
        self._email_repo = email_repo
        self._message_controller = message_controller

    async def get_message_by_id(self, account: Account, message_id: str) -> Message | None:
        """Get message by id."""
        email = await self._email_repo.get_by_account_and_email_id(account.id, message_id)
        folder = email.folder if email else None
        if email is not None:
            self._logger.info(
                f"Found email metadata; account_id: {account.id}, folder: {folder}, email_id: {email.email_id}, "
                f"thread_id: {email.thread_id}"
            )

        imap_message = await self._message_controller.get_message_by_id(account, message_id, folder)
        if imap_message is None:
            return None

        message = imap_message.message
        if (email is None and message is not None) or (message is not None and folder != message.folders[0]):
            self._logger.info(f"Caching email metadata; account_id: {account.id}, email_id: {message_id}")
            # Cache metadata for future lookups.
            await self._email_repo.add(
                Email(
                    account_id=account.id,
                    email_id=message_id,
                    thread_id=message.thread_id,
                    folder=message.folders[0],
                    uid=imap_message.uid,
                ),
                commit=True,
            )

        return message
