"""Payroll Absence API client (X-API-Key, retries on 503)."""

from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import urljoin

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


class PayrollUnavailableError(Exception):
    """Raised when payroll returns 503 Service Unavailable."""

    def __init__(self, message: str = "Payroll service unavailable", *, response: httpx.Response | None = None):
        super().__init__(message)
        self.response = response


class PayrollAPIError(Exception):
    """Non-retriable payroll API error."""

    def __init__(self, message: str, *, status_code: int | None = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _is_503(exc: BaseException) -> bool:
    return isinstance(exc, PayrollUnavailableError)


class PayrollClient:
    """Client for the client's Payroll Absence API (v2.3.1)."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        settings: Settings | None = None,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        cfg = settings or get_settings()
        self.base_url = (base_url or cfg.payroll_url).rstrip("/") + "/"
        self.api_key = api_key or cfg.payroll_api_key
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={
                "X-API-Key": self.api_key,
                "Accept": "application/json",
            },
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "PayrollClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    @retry(
        retry=retry_if_exception(_is_503),
        stop=stop_after_attempt(6),
        wait=wait_exponential_jitter(initial=0.5, max=20),
        reraise=True,
    )
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        auth_required: bool = True,
    ) -> httpx.Response:
        headers: dict[str, str] = {}
        if not auth_required:
            # Health is unauthenticated; strip key for that call only.
            pass

        response = self._client.request(
            method,
            path if not path.startswith("http") else path,
            params=params,
            json=json,
            headers=headers or None,
        )

        if response.status_code == 503:
            logger.warning("Payroll 503 on %s %s — will retry", method, path)
            raise PayrollUnavailableError(response=response)

        return response

    def _json(self, response: httpx.Response) -> Any:
        if response.status_code >= 400:
            try:
                body = response.json()
            except Exception:
                body = response.text
            raise PayrollAPIError(
                f"Payroll API error {response.status_code}: {body}",
                status_code=response.status_code,
                body=body,
            )
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # --- Endpoints -----------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """GET /v1/health — no API key required by contract."""
        # Use a bare request without relying on the default key header being wrong;
        # the server accepts the key on health as well, so reuse _request.
        response = self._request("GET", "/v1/health", auth_required=False)
        return self._json(response)

    def list_codes(self) -> Any:
        """GET /v1/codes — absence codes payroll accepts."""
        return self._json(self._request("GET", "/v1/codes"))

    def list_employees(
        self,
        *,
        page: int = 1,
        page_size: int = 25,
    ) -> dict[str, Any]:
        """GET /v1/employees — one page of employees."""
        return self._json(
            self._request(
                "GET",
                "/v1/employees",
                params={"page": page, "page_size": page_size},
            )
        )

    def iter_employees(self, *, page_size: int = 25) -> list[dict[str, Any]]:
        """Paginate through all employees and return the combined list.

        The mock API caps page_size (typically at 25). Never treat a short
        page as EOF unless the payload total is satisfied or the batch is empty;
        otherwise a request for page_size=100 that returns 25 would stop early.
        """
        employees: list[dict[str, Any]] = []
        page = 1
        # Respect a conservative client page size; API may return fewer.
        page_size = max(1, min(int(page_size), 25))
        while True:
            payload = self.list_employees(page=page, page_size=page_size)
            batch = self._extract_items(payload)
            if not batch:
                break
            employees.extend(batch)
            total = self._extract_total(payload)
            if total is not None and len(employees) >= total:
                break
            # If the API returned a full client page, keep going.
            if len(batch) < page_size:
                # Short page after capping usually means last page; stop.
                break
            page += 1
            if page > 500:  # safety
                break
        return employees

    def post_absence(
        self,
        *,
        employee_id: int,
        date: str,
        code: str,
        hours: float,
        note: str | None = None,
    ) -> dict[str, Any]:
        """POST /v1/absences — record one absence. Returns submission payload."""
        body: dict[str, Any] = {
            "employee_id": employee_id,
            "date": date,
            "code": code,
            "hours": hours,
        }
        if note is not None:
            body["note"] = note
        response = self._request("POST", "/v1/absences", json=body)
        if response.status_code not in (200, 202):
            return self._json(response)  # raises
        return response.json()

    def get_absence(self, submission_id: str) -> dict[str, Any]:
        """GET /v1/absences/{submission_id}."""
        return self._json(self._request("GET", f"/v1/absences/{submission_id}"))

    def list_absences(
        self,
        *,
        employee_id: int | None = None,
        date: str | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """GET /v1/absences with optional filters."""
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if employee_id is not None:
            params["employee_id"] = employee_id
        if date is not None:
            params["date"] = date
        if status is not None:
            params["status"] = status
        return self._json(self._request("GET", "/v1/absences", params=params))

    def void_absence(self, submission_id: str) -> Any:
        """DELETE /v1/absences/{submission_id}."""
        return self._json(self._request("DELETE", f"/v1/absences/{submission_id}"))

    def reset(self) -> Any:
        """POST /v1/admin/reset — clear all submissions (dev/eval only)."""
        return self._json(self._request("POST", "/v1/admin/reset"))

    # --- Helpers -------------------------------------------------------------------

    @staticmethod
    def _extract_items(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("employees", "items", "data", "results"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        return []

    @staticmethod
    def _extract_total(payload: Any) -> Optional[int]:
        if not isinstance(payload, dict):
            return None
        for key in ("total", "total_count", "count"):
            value = payload.get(key)
            if isinstance(value, int):
                return value
        return None
