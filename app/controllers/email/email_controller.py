import logging

from app.api.models.messages import EmailAddress
from app.controllers.email.message import MessageResult, SendMessageResult
from app.controllers.imap.message_controller import MessageController
from app.controllers.smtp.smtp_controller import (
    SMTPController,
    SMTPInvalidParameterError,
)
from app.models import Email
from app.models.account import Account
from app.repos.email import EmailRepo


class EmailController:
    """Controller for email operations."""

    def __init__(self, email_repo: EmailRepo, message_controller: MessageController, smtp_controller: SMTPController):
        self._logger = logging.getLogger(__name__)
        self._email_repo = email_repo
        self._message_controller = message_controller
        self._smtp_controller = smtp_controller

    async def get_message_by_id(self, account: Account, message_id: str) -> MessageResult | None:
        """Get message by id."""
        email = await self._email_repo.get_by_account_and_email_id(account.id, message_id)
        folder = email.folder if email else None
        uid = email.uid if email else None
        if email is not None:
            self._logger.info(
                f"Found email metadata; account_id: {account.id}, folder: {folder}, uid: {uid}, email_id: {message_id}"
            )

        message_result = await self._message_controller.get_message_by_id(account, message_id, folder, uid)
        if message_result is None:
            return None

        message = message_result.message
        if message is not None:
            if email is None:
                self._logger.info(f"Caching email metadata; account_id: {account.id}, email_id: {message_id}")
                # Cache metadata for future lookups.
                await self._email_repo.add(
                    Email(
                        account_id=account.id,
                        email_id=message_id,
                        thread_id=message.thread_id,
                        folder=message.folders[0],
                        uid=message_result.uid,
                    ),
                    commit=True,
                )
            elif folder != message.folders[0] or uid != message_result.uid:
                await self._email_repo.update(email, {"folder": message.folders[0], "uid": message_result.uid})

        return message_result

    async def send_email(
        self,
        account: Account,
        to: list[EmailAddress],
        subject: str,
        body: str,
        from_: list[EmailAddress] | None = None,
        cc: list[EmailAddress] | None = None,
        bcc: list[EmailAddress] | None = None,
        reply_to: list[EmailAddress] | None = None,
        reply_to_message_id: str | None = None,
    ) -> SendMessageResult:
        replied_message_result: MessageResult | None = None
        if reply_to_message_id:
            # Check if the original message exists
            replied_message_result = await self.get_message_by_id(account, reply_to_message_id)
            if not replied_message_result:
                raise SMTPInvalidParameterError("reply_to_message_id", reply_to_message_id)

        send_message_result = await self._smtp_controller.send_email(
            account, to, subject, body, from_, cc, bcc, reply_to, replied_message_result
        )

        if send_message_result.folder:
            await self._email_repo.add(
                Email(
                    account_id=account.id,
                    email_id=send_message_result.message_id,
                    thread_id=send_message_result.thread_id,
                    folder=send_message_result.folder,
                ),
                commit=True,
            )

        return send_message_result
