"""Pipeline orchestration endpoints for the React UI."""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.callouts.extract import extract_callouts
from app.callouts.rules import code_callouts
from app.config import get_settings
from app.db import SessionLocal, get_db
from app.kpis.ar_aging import write_ar_aging
from app.kpis.compute import write_kpis_by_building
from app.payroll_flow.submit import submit_absences, write_payroll_submissions_csv

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


class PipelineRequest(BaseModel):
    stages: list[str] = Field(
        default_factory=lambda: [
            "callouts",
            "payroll",
            "kpis",
            "ar_aging",
            "backup_notes",
        ]
    )
    dry_run: Optional[bool] = None
    persist_callouts: bool = True


def _run_pipeline_sync(stages: list[str], dry_run: bool | None) -> dict[str, Any]:
    settings = get_settings()
    result: dict[str, Any] = {"stages": {}, "output_dir": str(settings.output_dir)}
    session = SessionLocal()
    try:
        coded = []
        if "callouts" in stages:
            extracted = extract_callouts()
            coded = code_callouts(session, extracted)
            result["stages"]["callouts"] = {
                "extracted": len(extracted),
                "coded": len(coded),
                "codes": {c.code: 1 for c in coded},  # placeholder; replace below
            }
            # tally codes
            tally: dict[str, int] = {}
            for c in coded:
                tally[c.code or "NONE"] = tally.get(c.code or "NONE", 0) + 1
            result["stages"]["callouts"]["codes"] = tally

            if "callouts" in stages and getattr(settings, "persist", True):
                # Persist lightweight callout events when models available
                try:
                    from app.models import CalloutEvent
                    from sqlalchemy import select

                    for item in extracted:
                        if not item.is_absence:
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
                                raw_text=item.raw_text,
                                employee_name_raw=item.employee_hint,
                                event_date=item.event_date,
                                shift=item.shift,
                                building_raw=item.building_hint,
                                reason_text=item.reason_category,
                                reason_code=item.reason_category,
                                noticed_at=item.notice_time,
                                is_absence=item.is_absence,
                                confidence=item.confidence,
                                structured=item.to_dict(),
                                status="parsed",
                            )
                        )
                    session.commit()
                except Exception:
                    logger.exception("Failed persisting callout events")
                    session.rollback()

        if "payroll" in stages:
            if not coded:
                extracted = extract_callouts()
                coded = code_callouts(session, extracted)
            rows = submit_absences(session, coded, dry_run=dry_run)
            path = write_payroll_submissions_csv(rows)
            result["stages"]["payroll"] = {
                "rows": len(rows),
                "csv": str(path),
                "written": sum(1 for r in rows if r.get("decision") == "written"),
                "held": sum(1 for r in rows if r.get("decision") == "held_for_review"),
            }

        if "kpis" in stages:
            path = write_kpis_by_building(session)
            result["stages"]["kpis"] = {"csv": str(path)}

        if "ar_aging" in stages:
            path = write_ar_aging(session)
            result["stages"]["ar_aging"] = {"csv": str(path)}

        if "backup_notes" in stages:
            from app.billing.backup_notes import write_backup_notes

            paths = write_backup_notes(session)
            result["stages"]["backup_notes"] = {
                "count": len(paths),
                "files": [str(p) for p in paths[:50]],
            }

        result["status"] = "ok"
        return result
    except Exception as exc:
        logger.exception("Pipeline failed")
        result["status"] = "error"
        result["error"] = str(exc)
        return result
    finally:
        session.close()


@router.get("/status")
def pipeline_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    from sqlalchemy import func, select
    from app.models import (
        Building,
        CalloutEvent,
        CoverageBackupNote,
        KPISnapshot,
        PayrollSubmission,
        ReviewItem,
    )

    def count(model: type) -> int:
        return int(db.scalar(select(func.count()).select_from(model)) or 0)

    settings = get_settings()
    return {
        "payroll_dry_run": settings.payroll_dry_run,
        "counts": {
            "buildings": count(Building),
            "callout_events": count(CalloutEvent),
            "kpi_snapshots": count(KPISnapshot),
            "payroll_submissions": count(PayrollSubmission),
            "review_items": count(ReviewItem),
            "backup_notes": count(CoverageBackupNote),
        },
    }


@router.post("/run")
def run_pipeline(
    background_tasks: BackgroundTasks,
    body: PipelineRequest | None = None,
    background: bool = Query(default=False),
) -> dict[str, Any]:
    req = body or PipelineRequest()
    if background:
        background_tasks.add_task(_run_pipeline_sync, req.stages, req.dry_run)
        return {"status": "started", "stages": req.stages}
    return _run_pipeline_sync(req.stages, req.dry_run)


@router.post("/run-kpis")
def run_kpis_only(db: Session = Depends(get_db)) -> dict[str, Any]:
    kpi_path = write_kpis_by_building(db)
    ar_path = write_ar_aging(db)
    return {"status": "ok", "kpis_csv": str(kpi_path), "ar_aging_csv": str(ar_path)}
