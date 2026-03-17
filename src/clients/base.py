"""Base async API client with rate limiting, retry, and structured logging."""
from __future__ import annotations

import asyncio

import aiohttp
import structlog

from src.core.rate_limiter import TokenBucket

log = structlog.get_logger()

_TIMEOUT = aiohttp.ClientTimeout(total=60, connect=30)
_MAX_RETRIES = 3


class BaseAPIClient:
    """Shared aiohttp session with per-request rate limiting and retry.

    Subclasses set ``BASE_URL`` and ``AUTH_HEADER`` class attributes and
    expose thin endpoint wrappers that delegate to :meth:`get` or :meth:`post`.
    """

    BASE_URL: str = ""
    AUTH_HEADER: str = ""

    def __init__(
        self,
        api_key: str,
        rate_limiter: TokenBucket,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.api_key = api_key
        self.rate_limiter = rate_limiter
        self._external_session = session is not None
        self._session = session

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=_TIMEOUT)
        return self._session

    async def get(self, path: str, params: dict | None = None) -> dict:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, json: dict | None = None) -> dict:
        return await self._request("POST", path, json=json)

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        await self.rate_limiter.acquire()
        session = await self._ensure_session()
        url = f"{self.BASE_URL}{path}"

        for attempt in range(_MAX_RETRIES):
            try:
                async with session.request(
                    method, url,
                    headers={self.AUTH_HEADER: self.api_key},
                    **kwargs,
                ) as resp:
                    if resp.status == 429:
                        retry_after = float(
                            resp.headers.get("Retry-After", 2 ** attempt)
                        )
                        log.warning("rate_limited", url=url,
                                    retry_after=retry_after, attempt=attempt + 1)
                        await asyncio.sleep(retry_after)
                        continue
                    resp.raise_for_status()
                    try:
                        return await resp.json()
                    except (ValueError, aiohttp.ContentTypeError) as exc:
                        body_preview = (await resp.text())[:200]
                        log.error("json_parse_error", url=url,
                                  status=resp.status, body=body_preview,
                                  error=str(exc))
                        raise aiohttp.ClientResponseError(
                            resp.request_info, resp.history,
                            status=resp.status,
                            message=f"Invalid JSON from {url}",
                        ) from exc
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.warning("request_error", url=url,
                            attempt=attempt + 1, error=str(exc))
                if attempt == _MAX_RETRIES - 1:
                    raise
                await asyncio.sleep(2 ** attempt)

        raise aiohttp.ClientError(f"All {_MAX_RETRIES} retries exhausted for {url}")

    async def close(self) -> None:
        if self._session and not self._external_session and not self._session.closed:
            await self._session.close()
