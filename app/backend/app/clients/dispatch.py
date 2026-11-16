"""Dispatch API v2 client (OAuth client credentials, cursor pagination, 429 handling)."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Iterator, Optional

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_fixed

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


class DispatchAuthError(Exception):
    """OAuth or bearer token failure."""


class DispatchRateLimitError(Exception):
    """Raised on 429; carries Retry-After seconds."""

    def __init__(self, retry_after: float, message: str = "Dispatch rate limited"):
        super().__init__(message)
        self.retry_after = retry_after


class DispatchTransientError(Exception):
    """Raised on 502/503 so callers can retry."""


class DispatchAPIError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _is_retriable(exc: BaseException) -> bool:
    return isinstance(exc, (DispatchRateLimitError, DispatchTransientError))


class DispatchClient:
    """Client for the client's Dispatch app API (v2)."""

    TOKEN_SKEW_SECONDS = 30

    def __init__(
        self,
        base_url: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        *,
        settings: Settings | None = None,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        cfg = settings or get_settings()
        self.base_url = (base_url or cfg.dispatch_url).rstrip("/") + "/"
        self.client_id = client_id or cfg.dispatch_client_id
        self.client_secret = client_secret or cfg.dispatch_client_secret
        self._owns_client = client is None
        self._client = client or httpx.Client(base_url=self.base_url, timeout=timeout)
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._lock = threading.Lock()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "DispatchClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- Auth ----------------------------------------------------------------------

    def _token_valid(self) -> bool:
        return bool(self._token) and time.time() < (self._token_expires_at - self.TOKEN_SKEW_SECONDS)

    def fetch_token(self, *, force: bool = False) -> str:
        """POST /oauth/token with client credentials; cache until near expiry."""
        with self._lock:
            if not force and self._token_valid():
                assert self._token is not None
                return self._token

            response = self._client.post(
                "/oauth/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if response.status_code >= 400:
                raise DispatchAuthError(
                    f"Token request failed ({response.status_code}): {response.text}"
                )
            payload = response.json()
            token = payload.get("access_token")
            if not token:
                raise DispatchAuthError(f"Token response missing access_token: {payload}")
            expires_in = float(payload.get("expires_in", 300))
            self._token = token
            self._token_expires_at = time.time() + expires_in
            logger.debug("Dispatch token acquired, expires_in=%s", expires_in)
            return token

    def _auth_headers(self) -> dict[str, str]:
        token = self.fetch_token()
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

    # --- HTTP ----------------------------------------------------------------------

    def _parse_retry_after(self, response: httpx.Response) -> float:
        header = response.headers.get("Retry-After", "1")
        try:
            return max(float(header), 0.1)
        except ValueError:
            return 1.0

    @retry(
        retry=retry_if_exception(_is_retriable),
        stop=stop_after_attempt(10),
        wait=wait_fixed(0),  # sleep is done explicitly using Retry-After / backoff
        reraise=True,
    )
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        retry_on_invalid_token: bool = True,
    ) -> httpx.Response:
        response = self._client.request(
            method,
            path,
            params=params,
            headers=self._auth_headers(),
        )

        if response.status_code == 401 and retry_on_invalid_token:
            body: Any
            try:
                body = response.json()
            except Exception:
                body = {}
            if isinstance(body, dict) and body.get("error") == "invalid_token":
                logger.info("Dispatch token expired; refreshing")
                self.fetch_token(force=True)
                return self._request(
                    method, path, params=params, retry_on_invalid_token=False
                )

        if response.status_code == 429:
            delay = self._parse_retry_after(response)
            logger.warning("Dispatch 429 on %s %s — sleeping %.2fs", method, path, delay)
            time.sleep(delay)
            raise DispatchRateLimitError(delay)

        if response.status_code in (502, 503):
            logger.warning(
                "Dispatch %s on %s %s — backing off",
                response.status_code,
                method,
                path,
            )
            time.sleep(1.5)
            raise DispatchTransientError(
                f"Dispatch {response.status_code} on {method} {path}"
            )

        return response

    def _json(self, response: httpx.Response) -> Any:
        if response.status_code >= 400:
            try:
                body = response.json()
            except Exception:
                body = response.text
            raise DispatchAPIError(
                f"Dispatch API error {response.status_code}: {body}",
                status_code=response.status_code,
                body=body,
            )
        if not response.content:
            return None
        return response.json()

    def _paginate(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        limit: int = 50,
    ) -> Iterator[dict[str, Any]]:
        cursor: Optional[str] = None
        base_params = dict(params or {})
        base_params["limit"] = min(max(limit, 1), 50)

        while True:
            page_params = dict(base_params)
            if cursor:
                page_params["cursor"] = cursor
            payload = self._json(self._request("GET", path, params=page_params))
            if not isinstance(payload, dict):
                return
            data = payload.get("data") or []
            for item in data:
                yield item
            cursor = payload.get("next_cursor")
            if not cursor or not data:
                break

    # --- Endpoints -----------------------------------------------------------------

    def list_sites(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """GET /v2/sites — all sites including inactive."""
        return list(self._paginate("/v2/sites", limit=limit))

    def list_workers(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """GET /v2/workers — every person with a dispatch login."""
        return list(self._paginate("/v2/workers", limit=limit))

    def get_worker(self, worker_id: str) -> dict[str, Any]:
        """GET /v2/workers/{worker_id}."""
        return self._json(self._request("GET", f"/v2/workers/{worker_id}"))

    def list_postings(
        self,
        *,
        limit: int = 50,
        updated_since: str | None = None,
        site_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """GET /v2/postings with optional filters; returns all pages."""
        params: dict[str, Any] = {}
        if updated_since:
            params["updated_since"] = updated_since
        if site_id:
            params["site_id"] = site_id
        if status:
            params["status"] = status
        return list(self._paginate("/v2/postings", params=params, limit=limit))

    def iter_postings(
        self,
        *,
        limit: int = 50,
        updated_since: str | None = None,
        site_id: str | None = None,
        status: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield postings page by page (cursor pagination)."""
        params: dict[str, Any] = {}
        if updated_since:
            params["updated_since"] = updated_since
        if site_id:
            params["site_id"] = site_id
        if status:
            params["status"] = status
        yield from self._paginate("/v2/postings", params=params, limit=limit)

    def get_posting(self, posting_id: str) -> dict[str, Any]:
        """GET /v2/postings/{posting_id}."""
        return self._json(self._request("GET", f"/v2/postings/{posting_id}"))
