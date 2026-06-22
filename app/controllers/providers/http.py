import asyncio
import logging
from typing import Any

import aiohttp

from app.controllers.providers.exceptions import (
    ProviderAuthError,
    ProviderError,
    ProviderNotFoundError,
    ProviderRateLimitError,
)
from app.controllers.providers.token_service import TokenService
from app.models.account import Account

logger = logging.getLogger(__name__)


class AuthorizedHttpClient:
    """aiohttp wrapper that injects provider bearer tokens and maps errors.

    Retries exactly once with a force-refreshed token on 401; a second 401 marks
    the grant expired (webhook + status) and raises ProviderAuthError.
    """

    def __init__(self, token_service: TokenService, timeout: int) -> None:
        self._token_service = token_service
        self._timeout = timeout
        self._http_session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._http_session is None or self._http_session.closed:
                self._http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self._timeout))
            return self._http_session

    async def close(self) -> None:
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()

    async def request(
        self,
        account: Account,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        expect_json: bool = True,
        include_auth: bool = True,
    ) -> Any:
        access_token = ""
        if include_auth:
            try:
                access_token = await self._token_service.get_access_token(account)
            except ProviderAuthError:
                await self._token_service.handle_auth_failure(account)
                raise

        for attempt in (1, 2):
            request_headers = dict(headers or {})
            if include_auth:
                request_headers = {"Authorization": f"Bearer {access_token}", **request_headers}
            session = await self._get_session()
            async with session.request(
                method, url, params=params, json=json_body, data=data, headers=request_headers
            ) as response:
                if response.status == 401 and attempt == 1 and include_auth:
                    try:
                        access_token = await self._token_service.get_access_token(account, force_refresh=True)
                    except ProviderAuthError:
                        await self._token_service.handle_auth_failure(account)
                        raise
                    continue
                return await self._handle_response(account, response, expect_json, include_auth)
        raise ProviderError("Unreachable")  # pragma: no cover

    async def _handle_response(
        self, account: Account, response: aiohttp.ClientResponse, expect_json: bool, include_auth: bool = True
    ) -> Any:
        if 200 <= response.status < 300:
            if response.status == 204 or not expect_json:
                return await response.read()
            content_type = response.headers.get("Content-Type", "")
            if "json" in content_type:
                return await response.json()
            return await response.read()

        body_text = await response.text()
        if response.status == 401:
            if not include_auth:
                # Pre-authenticated URL (e.g. Graph upload session) rejected its own
                # token — not a grant credential failure; do not expire the grant.
                raise ProviderError(f"Pre-authenticated request rejected (401): {body_text[:500]}", status_code=401)
            await self._token_service.handle_auth_failure(account)
            raise ProviderAuthError(f"Provider rejected credentials: {body_text[:500]}")
        if response.status == 404:
            raise ProviderNotFoundError()
        if response.status == 429 or (response.status == 403 and "rate" in body_text.lower()):
            raise ProviderRateLimitError(f"Rate limited: {body_text[:500]}")
        raise ProviderError(
            f"Provider request failed ({response.status}): {body_text[:1000]}", status_code=response.status
        )
