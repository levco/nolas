"""
Connect controller for handling OAuth2 authorization flow business logic.
"""

import logging
import secrets
from typing import Optional

from app.controllers.imap.connection import ConnectionManager
from app.controllers.smtp.smtp_controller import SMTPController
from app.models.account import Account, AccountProvider, AccountStatus
from app.models.app import App
from app.models.oauth2 import OAuth2AuthorizationRequest, OAuth2RequestStatus
from app.repos.account import AccountRepo
from app.repos.oauth2 import OAuth2AuthorizationRequestRepo
from app.utils.password import PasswordUtils

logger = logging.getLogger(__name__)


class AuthorizationController:
    """Controller for OAuth2 connect operations."""

    def __init__(
        self,
        account_repo: AccountRepo,
        oauth2_authorization_request_repo: OAuth2AuthorizationRequestRepo,
        connection_manager: ConnectionManager,
        smtp_controller: SMTPController,
    ) -> None:
        self._account_repo = account_repo
        self._oauth2_authorization_request_repo = oauth2_authorization_request_repo
        self._connection_manager = connection_manager
        self._smtp_controller = smtp_controller

    async def process_authorization(
        self,
        app: App,
        client_id: str,
        redirect_uri: str,
        state: str,
        scope: Optional[str],
        email: str,
        password: str,
        imap_host: str,
        imap_port: int,
        smtp_host: str,
        smtp_port: int,
    ) -> tuple[bool, str]:
        """
        Process the authorization request and create/update account.

        Returns:
            tuple: (success, result) where result is authorization code on success or error message on failure
        """
        try:
            if not await self._test_imap_connection(email, password, imap_host, imap_port):
                return False, "Unable to connect to IMAP server. Please check your credentials and try again."

            if not await self._smtp_controller.login(email, password, smtp_host, smtp_port):
                return False, "Unable to connect to SMTP server. Please check your credentials and try again."

            account = await self._create_or_update_account(
                app, email, password, imap_host, imap_port, smtp_host, smtp_port
            )

            auth_code = self._generate_authorization_code()
            auth_request = OAuth2AuthorizationRequest(
                app_id=app.id,
                client_id=client_id,
                redirect_uri=redirect_uri,
                state=state,
                scope=scope,
                status=OAuth2RequestStatus.authorized,
                code=auth_code,
                account=account,
            )
            await self._oauth2_authorization_request_repo.add(auth_request)

            return True, auth_code

        except Exception:
            logger.exception("Error processing authorization")
            return False, "Internal server error during authorization"

    def _generate_authorization_code(self) -> str:
        """Generate a secure authorization code."""
        return secrets.token_urlsafe(32)

    async def _create_or_update_account(
        self, app: App, email: str, password: str, imap_host: str, imap_port: int, smtp_host: str, smtp_port: int
    ) -> Account:
        """Create or update account."""
        existing_account = await self._account_repo.get_by_email(email)
        if existing_account:
            account = await self._account_repo.update(
                existing_account,
                {
                    "credentials": PasswordUtils.encrypt_password(password),
                    "provider_context": {
                        "imap_host": imap_host,
                        "imap_port": imap_port,
                        "smtp_host": smtp_host,
                        "smtp_port": smtp_port,
                    },
                    "status": (
                        AccountStatus.active
                        if existing_account.status == AccountStatus.active
                        else AccountStatus.pending
                    ),
                },
            )
        else:
            account = Account(
                app_id=app.id,
                email=email,
                provider=AccountProvider.imap,
                credentials=PasswordUtils.encrypt_password(password),
                provider_context={
                    "imap_host": imap_host,
                    "imap_port": imap_port,
                    "smtp_host": smtp_host,
                    "smtp_port": smtp_port,
                },
                status=AccountStatus.pending,
            )
            await self._account_repo.add(account)
        return account

    async def _test_imap_connection(self, email: str, password: str, imap_host: str, imap_port: int) -> bool:
        """Test IMAP connection with provided credentials."""
        try:
            test_account = Account(
                email=email,
                provider=AccountProvider.imap,
                credentials=PasswordUtils.encrypt_password(password),
                provider_context={"imap_host": imap_host, "imap_port": imap_port},
                status=AccountStatus.active,
                app_id=0,  # Temporary
            )

            try:
                connection = await self._connection_manager.get_connection(test_account)
                if connection is None:
                    return False
                await self._connection_manager.close_connection(connection, test_account)
                return True
            except Exception as e:
                logger.warning(f"IMAP connection test failed for {email}: {e}")
                return False
        except Exception as e:
            logger.error(f"Error testing IMAP connection: {e}")
            return False
