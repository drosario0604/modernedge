"""Orchestrate ingest → resolve → (kpi/callout stubs) and write exceptions.csv."""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import SessionLocal, init_db
from app.ingest.apis import ingest_apis
from app.ingest.files import ingest_files
from app.models import ExceptionRecord
from app.resolve.entities import resolve_all

logger = logging.getLogger(__name__)


def write_exceptions_csv(session: Session, output_dir: Path) -> Path:
    """Write every ExceptionRecord to output/exceptions.csv (early artifact)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "exceptions.csv"
    rows = (
        session.query(ExceptionRecord)
        .order_by(ExceptionRecord.id.asc())
        .all()
    )
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["source", "record_ref", "issue", "action_taken", "confidence"],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "source": row.source,
                    "record_ref": row.record_ref,
                    "issue": row.issue,
                    "action_taken": row.action_taken,
                    "confidence": (
                        f"{row.confidence:.4f}" if row.confidence is not None else ""
                    ),
                }
            )
    logger.info("Wrote %s exception rows to %s", len(rows), path)
    return path


def _try_callouts(session: Session, settings: Settings) -> dict[str, Any]:
    """Extract → code → payroll submit → backup notes."""
    try:
        from app.callouts.extract import extract_callouts, load_and_extract
        from app.callouts.rules import code_callouts
        from app.payroll_flow.submit import submit_absences, write_payroll_submissions_csv
    except ImportError as exc:
        logger.info("Callouts module not ready: %s", exc)
        return {"status": "skipped", "reason": "import_error"}

    try:
        try:
            extracted = load_and_extract(session, settings=settings)
        except TypeError:
            extracted = extract_callouts()
        coded = code_callouts(session, extracted)

        # Persist callout events for the building view / KPIs
        try:
            from sqlalchemy import select
            from app.models import CalloutEvent

            for item in extracted:
                if not getattr(item, "is_absence", True):
                    continue
                exists = session.scalar(
                    select(CalloutEvent).where(
                        CalloutEvent.source == item.source,
                        CalloutEvent.source_ref == item.source_ref,
                    )
                )
                if exists:
                    continue
                session.add(
                    CalloutEvent(
                        source=item.source,
                        source_ref=item.source_ref,
                        raw_text=getattr(item, "raw_text", None),
                        employee_name_raw=getattr(item, "employee_hint", None),
                        event_date=getattr(item, "event_date", None),
                        shift=getattr(item, "shift", None),
                        building_raw=getattr(item, "building_hint", None),
                        reason_text=getattr(item, "reason_category", None),
                        reason_code=getattr(item, "reason_category", None),
                        noticed_at=getattr(item, "notice_time", None),
                        is_absence=getattr(item, "is_absence", True),
                        confidence=getattr(item, "confidence", None),
                        structured=item.to_dict() if hasattr(item, "to_dict") else None,
                        status="parsed",
                    )
                )
            session.commit()
        except Exception:  # noqa: BLE001
            logger.exception("Failed persisting callout events")
            session.rollback()

        rows = submit_absences(session, coded, dry_run=settings.payroll_dry_run)
        csv_path = write_payroll_submissions_csv(rows)

        backup: dict[str, Any] = {"status": "skipped"}
        try:
            from app.billing.backup_notes import write_backup_notes

            paths = write_backup_notes(session)
            backup = {"status": "ok", "count": len(paths)}
        except Exception as exc:  # noqa: BLE001
            logger.exception("Backup notes failed")
            backup = {"status": "error", "error": str(exc)}

        session.commit()
        return {
            "status": "ok",
            "extracted": len(extracted),
            "coded": len(coded),
            "payroll_rows": len(rows),
            "payroll_csv": str(csv_path),
            "backup_notes": backup,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("Callouts processing failed")
        session.rollback()
        return {"status": "error", "error": str(exc)}


def _try_kpis(session: Session, settings: Settings) -> dict[str, Any]:
    try:
        from app.kpis.compute import write_kpis_by_building
        from app.kpis.ar_aging import write_ar_aging
    except ImportError as exc:
        logger.info("KPIs module not ready: %s", exc)
        return {"status": "skipped", "reason": "import_error"}
    try:
        kpi_path = write_kpis_by_building(session)
        ar_path = write_ar_aging(session)
        session.commit()
        return {
            "status": "ok",
            "kpis_csv": str(kpi_path),
            "ar_aging_csv": str(ar_path),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("KPI computation failed")
        session.rollback()
        return {"status": "error", "error": str(exc)}


def run_pipeline(session: Session | None = None) -> dict[str, Any]:
    """
    Full spine: files → APIs → resolve → optional callouts/kpis → exceptions.csv.

    When `session` is None, opens a new SessionLocal and always closes it.
    """
    settings = get_settings()
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    owns_session = session is None
    if owns_session:
        init_db()
        session = SessionLocal()

    assert session is not None
    summary: dict[str, Any] = {"period": settings.period}

    try:
        logger.info("Pipeline: ingest files from %s", settings.data_dir)
        summary["files"] = ingest_files(session, settings)

        logger.info("Pipeline: ingest APIs")
        summary["apis"] = ingest_apis(session, settings)

        logger.info("Pipeline: resolve entities")
        summary["resolve"] = resolve_all(session, settings)

        # Write exceptions early so partial runs still produce the required artifact
        summary["exceptions_csv"] = str(
            write_exceptions_csv(session, Path(settings.output_dir))
        )

        summary["callouts"] = _try_callouts(session, settings)
        summary["kpis"] = _try_kpis(session, settings)

        # Refresh exceptions after downstream steps
        summary["exceptions_csv"] = str(
            write_exceptions_csv(session, Path(settings.output_dir))
        )
        summary["exception_count"] = session.query(ExceptionRecord).count()
        summary["status"] = "ok"
    except Exception as exc:  # noqa: BLE001
        logger.exception("Pipeline failed")
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        # Best-effort exceptions dump
        try:
            write_exceptions_csv(session, Path(settings.output_dir))
        except Exception:  # noqa: BLE001
            pass
        summary["status"] = "error"
        summary["error"] = str(exc)
        raise
    finally:
        if owns_session:
            session.close()

    logger.info("Pipeline finished: %s", summary.get("status"))
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run_pipeline())
