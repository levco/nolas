"""
Subscription renewal worker.

Periodically scans active Google/Microsoft accounts and renews Gmail watches and
Microsoft Graph change-notification subscriptions before they expire. Also heals
accounts that are missing a watch/subscription entirely (e.g. when setup failed
during grant creation).

Run with: python workers/subscription_renewal.py
"""

import asyncio
import logging
import os
import signal
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../")))
load_dotenv("./.env", override=True)

import sentry_sdk

from app.container import get_wire_container
from app.controllers.providers.exceptions import ProviderAuthError
from app.db import fastapi_sqlalchemy_context
from app.models.account import AccountProvider
from logging_config import setup_logging
from settings import settings

if settings.sentry.is_enabled:
    sentry_sdk.init(dsn=settings.sentry.dsn, environment=settings.environment.value)

logger = logging.getLogger(__name__)
setup_logging()
container = get_wire_container()


async def renew_once() -> None:
    async with fastapi_sqlalchemy_context():
        account_repo = container.repos.account()
        subscription_manager = container.controllers.subscription_manager()
        token_service = container.controllers.token_service()

        accounts = await account_repo.get_all_active_by_providers([AccountProvider.google, AccountProvider.microsoft])
        renew_within_seconds = settings.subscription_renewal.renew_within_hours * 3600

        renewed = 0
        for account in accounts:
            if not subscription_manager.needs_renewal(account, renew_within_seconds):
                continue
            try:
                await subscription_manager.ensure_subscription(account)
                renewed += 1
            except ProviderAuthError:
                # handle_auth_failure has already marked the grant expired and
                # emitted grant.expired via the token service / http client.
                logger.warning(f"Auth failure renewing notifications for {account.email}")
            except Exception:
                logger.exception(f"Failed to renew notifications for {account.email}")

        await account_repo.commit()
        await token_service.close()
        logger.info(f"Subscription renewal pass complete: {renewed}/{len(accounts)} renewed")


async def main() -> None:
    shutdown_event = asyncio.Event()

    def signal_handler() -> None:
        logger.info("Received shutdown signal")
        shutdown_event.set()

    for sig in [signal.SIGINT, signal.SIGTERM]:
        signal.signal(sig, lambda s, f: signal_handler())

    while not shutdown_event.is_set():
        try:
            await renew_once()
        except Exception:
            logger.exception("Subscription renewal pass failed")
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=settings.subscription_renewal.poll_interval)
        except asyncio.TimeoutError:
            continue


if __name__ == "__main__":
    logger.info("Starting subscription renewal worker")
    asyncio.run(main())
