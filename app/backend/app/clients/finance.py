"""Finance Reporting API client (HTTP Basic auth, async report jobs)."""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import httpx

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset({"COMPLETE", "FAILED"})


class FinanceAPIError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        faultcode: str | None = None,
        faultstring: str | None = None,
        body: Any = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.faultcode = faultcode
        self.faultstring = faultstring
        self.body = body


class FinanceReportFailed(FinanceAPIError):
    """Report job reached FAILED status."""


class FinanceClient:
    """Client for the client's Finance ERP reporting interface."""

    def __init__(
        self,
        base_url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        *,
        settings: Settings | None = None,
        timeout: float = 60.0,
        poll_interval: float = 1.0,
        poll_timeout: float = 180.0,
        client: httpx.Client | None = None,
    ) -> None:
        cfg = settings or get_settings()
        self.base_url = (base_url or cfg.finance_url).rstrip("/") + "/"
        self.username = username or cfg.finance_user
        self.password = password or cfg.finance_password
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            auth=(self.username, self.password),
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "FinanceClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _raise_for_error(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        body: Any
        try:
            body = response.json()
        except Exception:
            body = response.text
        faultcode = faultstring = None
        if isinstance(body, dict):
            faultcode = body.get("faultcode")
            faultstring = body.get("faultstring")
        raise FinanceAPIError(
            faultstring
            or f"Finance API error {response.status_code}: {body}",
            status_code=response.status_code,
            faultcode=faultcode,
            faultstring=faultstring,
            body=body,
        )

    def _json(self, response: httpx.Response) -> Any:
        self._raise_for_error(response)
        if not response.content:
            return None
        return response.json()

    # --- Reference data ------------------------------------------------------------

    def list_entities(self) -> list[dict[str, Any]]:
        """GET /api/v1/entities."""
        payload = self._json(self._client.get("/api/v1/entities"))
        return self._as_list(payload)

    def list_customers(
        self,
        *,
        offset: int = 0,
        limit: int = 100,
        entity_code: str | None = None,
    ) -> list[dict[str, Any]]:
        """GET /api/v1/customers — one page (limit max 100)."""
        params: dict[str, Any] = {
            "offset": offset,
            "limit": min(max(limit, 1), 100),
        }
        if entity_code:
            params["entity_code"] = entity_code
        payload = self._json(self._client.get("/api/v1/customers", params=params))
        return self._as_list(payload)

    def iter_customers(
        self,
        *,
        limit: int = 100,
        entity_code: str | None = None,
    ) -> list[dict[str, Any]]:
        """Paginate customers via offset/limit until exhausted."""
        results: list[dict[str, Any]] = []
        offset = 0
        page_size = min(max(limit, 1), 100)
        while True:
            batch = self.list_customers(
                offset=offset, limit=page_size, entity_code=entity_code
            )
            if not batch:
                break
            results.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size
        return results

    def list_sites(self) -> list[dict[str, Any]]:
        """GET /api/v1/sites — finance site names and ledger departments."""
        payload = self._json(self._client.get("/api/v1/sites"))
        return self._as_list(payload)

    # --- Reports -------------------------------------------------------------------

    def submit_report(
        self,
        *,
        report_code: str,
        entity_code: str,
        period: str,
    ) -> dict[str, Any]:
        """POST /api/v1/reports — enqueue a report job. Returns payload with job_id."""
        body = {
            "report_code": report_code,
            "entity_code": entity_code,
            "period": period,
        }
        response = self._client.post("/api/v1/reports", json=body)
        if response.status_code not in (200, 202):
            self._raise_for_error(response)
        return response.json()

    def get_report_job(self, job_id: str) -> dict[str, Any]:
        """GET /api/v1/reports/{job_id}."""
        return self._json(self._client.get(f"/api/v1/reports/{job_id}"))

    def wait_for_report(
        self,
        job_id: str,
        *,
        poll_interval: float | None = None,
        poll_timeout: float | None = None,
    ) -> dict[str, Any]:
        """Poll report job until COMPLETE or FAILED."""
        interval = poll_interval if poll_interval is not None else self.poll_interval
        timeout = poll_timeout if poll_timeout is not None else self.poll_timeout
        deadline = time.monotonic() + timeout
        last: dict[str, Any] = {}

        while time.monotonic() < deadline:
            last = self.get_report_job(job_id)
            status = str(last.get("status", "")).upper()
            if status == "COMPLETE":
                return last
            if status == "FAILED":
                raise FinanceReportFailed(
                    last.get("error") or f"Report job {job_id} failed",
                    body=last,
                )
            time.sleep(interval)

        raise FinanceAPIError(
            f"Timed out waiting for report job {job_id} after {timeout}s",
            body=last,
        )

    def download_report_file(self, job_id: str) -> bytes:
        """GET /api/v1/reports/{job_id}/file — raw report bytes."""
        response = self._client.get(
            f"/api/v1/reports/{job_id}/file",
            headers={"Accept": "*/*"},
        )
        self._raise_for_error(response)
        return response.content

    def download_report_file_from_url(self, file_url: str) -> bytes:
        """Download using the file_url from a COMPLETE job (absolute or relative)."""
        response = self._client.get(file_url, headers={"Accept": "*/*"})
        self._raise_for_error(response)
        return response.content

    def run_report(
        self,
        *,
        report_code: str,
        entity_code: str,
        period: str,
        poll_interval: float | None = None,
        poll_timeout: float | None = None,
        max_submit_attempts: int = 5,
    ) -> tuple[dict[str, Any], bytes]:
        """
        Submit a report, poll until COMPLETE, and download the file.

        On FAILED (e.g. simulated ORA-01555), resubmit per API guidance.
        Returns (job_payload, file_bytes).
        """
        last_error: Exception | None = None
        for attempt in range(1, max_submit_attempts + 1):
            submitted = self.submit_report(
                report_code=report_code,
                entity_code=entity_code,
                period=period,
            )
            job_id = submitted.get("job_id")
            if not job_id:
                raise FinanceAPIError(
                    f"Report submit response missing job_id: {submitted}",
                    body=submitted,
                )
            try:
                job = self.wait_for_report(
                    job_id,
                    poll_interval=poll_interval,
                    poll_timeout=poll_timeout,
                )
            except FinanceReportFailed as exc:
                last_error = exc
                logger.warning(
                    "Finance report %s/%s FAILED (attempt %s/%s): %s — resubmitting",
                    report_code,
                    entity_code,
                    attempt,
                    max_submit_attempts,
                    exc,
                )
                time.sleep(0.5 * attempt)
                continue
            file_bytes = self.download_report_file(job_id)
            return job, file_bytes
        raise FinanceReportFailed(
            f"Report {report_code}/{entity_code} failed after "
            f"{max_submit_attempts} submit attempts: {last_error}",
            body=getattr(last_error, "body", None),
        )

    # --- Helpers -------------------------------------------------------------------

    @staticmethod
    def _as_list(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("data", "items", "results", "entities", "customers", "sites"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        return []
