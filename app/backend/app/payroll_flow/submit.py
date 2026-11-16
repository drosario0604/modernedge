"""Submit coded absences to payroll with dry-run, idempotency, and audit."""

from __future__ import annotations

import csv
import hashlib
import logging
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.callouts.rules import CodedAbsence
from app.clients.payroll import PayrollAPIError, PayrollClient, PayrollUnavailableError
from app.config import Settings, get_settings
from app.models import AuditLog, ExceptionRecord, PayrollSubmission, ReviewItem

logger = logging.getLogger(__name__)


def _stable_source_ref(item: CodedAbsence) -> str:
    """Never persist a blank source_ref — it breaks the unique (source, source_ref) key."""
    raw = (item.source_ref or "").strip()
    if raw:
        return raw[:128]
    digest = hashlib.sha1(
        f"{item.source}|{item.event_date.isoformat()}|{item.hr_employee_id}|"
        f"{item.payroll_employee_id}|{item.code}|{item.reason}".encode("utf-8")
    ).hexdigest()[:12]
    return f"{item.event_date.isoformat()}-{digest}"[:128]


def _with_ref(item: CodedAbsence) -> CodedAbsence:
    ref = _stable_source_ref(item)
    if ref == item.source_ref:
        return item
    # dataclasses-style / plain object: mutate if possible
    try:
        object.__setattr__(item, "source_ref", ref)
    except Exception:
        item.source_ref = ref  # type: ignore[attr-defined]
    return item

SUBMISSION_COLUMNS = [
    "source",
    "source_ref",
    "employee_id",
    "date",
    "code",
    "decision",
    "submission_id",
    "final_status",
    "reason",
]

CONFIDENCE_FLOOR = 0.70


def _audit(
    session: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: str | None,
    detail: dict[str, Any],
    success: bool = True,
) -> None:
    session.add(
        AuditLog(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            actor="payroll_flow",
            detail=detail,
            success=success,
        )
    )


def _existing_local(
    session: Session, source: str, source_ref: str
) -> Optional[PayrollSubmission]:
    return session.scalar(
        select(PayrollSubmission).where(
            PayrollSubmission.source == source,
            PayrollSubmission.source_ref == source_ref,
        )
    )


def _payroll_has_absence(
    client: PayrollClient,
    *,
    payroll_employee_id: int,
    absence_date: str,
) -> Optional[dict[str, Any]]:
    """Return an existing payroll absence for employee+date if present."""
    try:
        payload = client.list_absences(
            employee_id=payroll_employee_id,
            date=absence_date,
            page_size=50,
        )
    except (PayrollAPIError, PayrollUnavailableError) as exc:
        logger.warning("Could not list absences for idempotency check: %s", exc)
        return None

    items: list[dict[str, Any]]
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = (
            payload.get("absences")
            or payload.get("items")
            or payload.get("data")
            or payload.get("results")
            or []
        )
    else:
        items = []

    for item in items:
        status = str(item.get("status", "")).upper()
        if status in {"VOID", "VOIDED", "DELETED"}:
            continue
        return item
    return None


def _enqueue_review(
    session: Session,
    coded: CodedAbsence,
    *,
    reason: str,
) -> ReviewItem:
    item = ReviewItem(
        kind="payroll_absence",
        status="pending",
        title=f"{coded.hr_employee_id or coded.payroll_employee_id or 'unknown'} {coded.event_date} {coded.code}",
        reason=reason,
        confidence=Decimal(str(round(coded.confidence, 4))),
        source=coded.source,
        source_ref=coded.source_ref,
        payload={
            "code": coded.code,
            "date": coded.event_date.isoformat(),
            "payroll_employee_id": coded.payroll_employee_id,
            "hr_employee_id": coded.hr_employee_id,
            "reason_category": coded.reason_category,
            "coding_reason": coded.reason,
        },
    )
    session.add(item)
    return item


def submit_absences(
    session: Session,
    coded: Iterable[CodedAbsence],
    *,
    dry_run: bool | None = None,
    client: PayrollClient | None = None,
    settings: Settings | None = None,
    poll_final_status: bool = True,
) -> list[dict[str, Any]]:
    """
    Submit (or dry-run) each coded absence exactly once.

    - Low confidence / unresolved → held_for_review + review queue
    - Existing local or payroll absence → skip write (idempotent)
    - 503s retried inside PayrollClient
    - Writes audit_logs and returns row dicts for payroll_submissions.csv
    """
    cfg = settings or get_settings()
    is_dry = cfg.payroll_dry_run if dry_run is None else dry_run
    owns_client = client is None
    payroll = client or PayrollClient(settings=cfg)

    rows: list[dict[str, Any]] = []
    try:
        for raw_item in coded:
            item = _with_ref(raw_item)
            try:
                row = _submit_one(
                    session,
                    item,
                    payroll=payroll,
                    is_dry=is_dry,
                    poll_final_status=poll_final_status,
                )
                if row is not None:
                    rows.append(row)
            except IntegrityError:
                session.rollback()
                existing = _existing_local(session, item.source, item.source_ref)
                if existing is not None:
                    rows.append(_row_from_submission(existing))
                    continue
                # Last resort: uniquify ref and retry once as hold
                item.source_ref = f"{item.source_ref}-{uuid.uuid4().hex[:6]}"[:128]
                rows.append(
                    _hold(
                        session,
                        item,
                        reason=item.reason or "Retry after unique constraint",
                        dry_run=is_dry,
                    )
                )

        session.commit()
    finally:
        if owns_client:
            payroll.close()

    return rows


def _submit_one(
    session: Session,
    item: CodedAbsence,
    *,
    payroll: PayrollClient,
    is_dry: bool,
    poll_final_status: bool,
) -> dict[str, Any] | None:
    if item.decision_hint == "skip":
        return None

    if not item.code:
        return _hold(
            session,
            item,
            reason=item.reason or "Missing code or unresolved employee",
            dry_run=is_dry,
        )

    existing = _existing_local(session, item.source, item.source_ref)
    if existing is not None:
        _audit(
            session,
            action="payroll.skip_existing_local",
            entity_type="payroll_submission",
            entity_id=str(existing.id),
            detail={"source": item.source, "source_ref": item.source_ref},
        )
        return _row_from_submission(existing)

    if item.decision_hint == "review" or item.confidence < CONFIDENCE_FLOOR:
        return _hold(
            session,
            item,
            reason=item.reason or "Held for review (low confidence or ambiguous coding)",
            dry_run=is_dry,
        )

    if item.payroll_employee_id is None:
        return _hold(
            session,
            item,
            reason="No payroll employee_id mapped",
            dry_run=is_dry,
        )

    date_str = item.event_date.isoformat()

    if not is_dry:
        prior = _payroll_has_absence(
            payroll,
            payroll_employee_id=item.payroll_employee_id,
            absence_date=date_str,
        )
        if prior is not None:
            sub = PayrollSubmission(
                source=item.source,
                source_ref=item.source_ref,
                employee_id=item.employee_id,
                payroll_employee_id=item.payroll_employee_id,
                absence_date=item.event_date,
                code=item.code,
                hours=item.hours,
                note=item.reason[:500] if item.reason else None,
                decision="written",
                submission_id=str(prior.get("submission_id") or ""),
                final_status=str(prior.get("status") or "EXISTS"),
                reason="Idempotent skip: absence already in payroll",
                dry_run=False,
                response_payload=prior,
            )
            session.add(sub)
            _audit(
                session,
                action="payroll.skip_existing_remote",
                entity_type="absence",
                entity_id=str(prior.get("submission_id")),
                detail={"employee_id": item.payroll_employee_id, "date": date_str},
            )
            session.flush()
            return _row_from_submission(sub)

    payload = {
        "employee_id": item.payroll_employee_id,
        "date": date_str,
        "code": item.code,
        "hours": float(item.hours),
        "note": (item.reason or "")[:240] or None,
    }

    if is_dry:
        fake_id = f"DRY-{uuid.uuid4().hex[:10].upper()}"
        sub = PayrollSubmission(
            source=item.source,
            source_ref=item.source_ref,
            employee_id=item.employee_id,
            payroll_employee_id=item.payroll_employee_id,
            absence_date=item.event_date,
            code=item.code,
            hours=item.hours,
            note=item.reason[:500] if item.reason else None,
            decision="written",
            submission_id=fake_id,
            final_status="DRY_RUN",
            reason=item.reason or "Dry-run submission",
            dry_run=True,
            request_payload=payload,
            response_payload={"submission_id": fake_id, "status": "DRY_RUN"},
        )
        session.add(sub)
        _audit(
            session,
            action="payroll.dry_run_write",
            entity_type="absence",
            entity_id=fake_id,
            detail=payload,
        )
        session.flush()
        return _row_from_submission(sub)

    try:
        response = payroll.post_absence(
            employee_id=item.payroll_employee_id,
            date=date_str,
            code=item.code,
            hours=float(item.hours),
            note=payload["note"],
        )
    except PayrollUnavailableError as exc:
        _audit(
            session,
            action="payroll.write_failed_503",
            entity_type="absence",
            entity_id=None,
            detail=payload,
            success=False,
        )
        return _hold(
            session,
            item,
            reason=f"Payroll unavailable after retries: {exc}",
            dry_run=False,
        )
    except PayrollAPIError as exc:
        _audit(
            session,
            action="payroll.write_failed",
            entity_type="absence",
            entity_id=None,
            detail={"payload": payload, "error": str(exc)},
            success=False,
        )
        return _hold(
            session,
            item,
            reason=f"Payroll API error: {exc}",
            dry_run=False,
        )

    submission_id = str(response.get("submission_id") or "")
    final_status = str(response.get("status") or "RECEIVED")

    if poll_final_status and submission_id:
        try:
            detail = payroll.get_absence(submission_id)
            final_status = str(detail.get("status") or final_status)
            response = detail
        except Exception as exc:  # noqa: BLE001
            logger.info("Could not poll submission %s: %s", submission_id, exc)

    sub = PayrollSubmission(
        source=item.source,
        source_ref=item.source_ref,
        employee_id=item.employee_id,
        payroll_employee_id=item.payroll_employee_id,
        absence_date=item.event_date,
        code=item.code,
        hours=item.hours,
        note=item.reason[:500] if item.reason else None,
        decision="written",
        submission_id=submission_id,
        final_status=final_status,
        reason=item.reason,
        dry_run=False,
        request_payload=payload,
        response_payload=response if isinstance(response, dict) else {"raw": response},
    )
    session.add(sub)
    _audit(
        session,
        action="payroll.write",
        entity_type="absence",
        entity_id=submission_id,
        detail={"payload": payload, "response": response},
    )
    session.flush()
    return _row_from_submission(sub)


def _hold(
    session: Session,
    item: CodedAbsence,
    *,
    reason: str,
    dry_run: bool,
) -> dict[str, Any]:
    item = _with_ref(item)
    existing = _existing_local(session, item.source, item.source_ref)
    if existing is not None:
        return _row_from_submission(existing)

    _enqueue_review(session, item, reason=reason)
    session.add(
        ExceptionRecord(
            source=item.source,
            record_ref=item.source_ref,
            issue=reason,
            action_taken="held_for_review",
            confidence=Decimal(str(round(item.confidence, 4))),
            meta={
                "code": item.code,
                "date": item.event_date.isoformat(),
                "payroll_employee_id": item.payroll_employee_id,
            },
        )
    )
    sub = PayrollSubmission(
        source=item.source,
        source_ref=item.source_ref,
        employee_id=item.employee_id,
        payroll_employee_id=item.payroll_employee_id,
        absence_date=item.event_date,
        code=item.code or None,
        hours=item.hours,
        note=reason[:500],
        decision="held_for_review",
        submission_id=None,
        final_status=None,
        reason=reason,
        dry_run=dry_run,
        request_payload={
            "employee_id": item.payroll_employee_id,
            "date": item.event_date.isoformat(),
            "code": item.code,
            "hours": float(item.hours),
        },
    )
    session.add(sub)
    _audit(
        session,
        action="payroll.held_for_review",
        entity_type="absence",
        entity_id=f"{item.source}:{item.source_ref}",
        detail={"reason": reason, "code": item.code},
        success=True,
    )
    session.flush()
    return _row_from_submission(sub)


def _row_from_submission(sub: PayrollSubmission) -> dict[str, Any]:
    return {
        "source": sub.source,
        "source_ref": sub.source_ref,
        "employee_id": sub.payroll_employee_id if sub.payroll_employee_id is not None else "",
        "date": sub.absence_date.isoformat() if sub.absence_date else "",
        "code": sub.code or "",
        "decision": sub.decision,
        "submission_id": sub.submission_id or "",
        "final_status": sub.final_status or "",
        "reason": sub.reason or "",
    }


def write_payroll_submissions_csv(
    rows: Iterable[dict[str, Any]],
    *,
    output_path: Path | str | None = None,
) -> Path:
    settings = get_settings()
    path = Path(output_path) if output_path else settings.output_dir / "payroll_submissions.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SUBMISSION_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in SUBMISSION_COLUMNS})
    return path
