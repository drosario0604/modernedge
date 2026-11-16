"""KPI and AR aging endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.kpis.ar_aging import compute_ar_aging, write_ar_aging
from app.kpis.compute import compute_kpis_by_building, write_kpis_by_building
from app.models import Building, KPISnapshot

router = APIRouter(prefix="/kpis", tags=["kpis"])


def _snap_dict(k: KPISnapshot) -> dict[str, Any]:
    return {
        "id": k.id,
        "building_id": k.building_id,
        "building": k.building,
        "period": k.period,
        "contracted_headcount": k.contracted_headcount,
        "active_headcount": k.active_headcount,
        "headcount_variance": k.headcount_variance,
        "scheduled_shifts": k.scheduled_shifts,
        "callout_shifts": k.callout_shifts,
        "callout_rate": float(k.callout_rate) if k.callout_rate is not None else None,
        "fill_rate": float(k.fill_rate) if k.fill_rate is not None else None,
        "overtime_hours": float(k.overtime_hours) if k.overtime_hours is not None else None,
        "overtime_dollars": float(k.overtime_dollars) if k.overtime_dollars is not None else None,
        "revenue": float(k.revenue) if k.revenue is not None else None,
        "labor_cost": float(k.labor_cost) if k.labor_cost is not None else None,
        "gross_margin": float(k.gross_margin) if k.gross_margin is not None else None,
        "gross_margin_pct": float(k.gross_margin_pct) if k.gross_margin_pct is not None else None,
        "unbilled_coverage_count": k.unbilled_coverage_count,
        "unbilled_coverage_dollars": float(k.unbilled_coverage_dollars)
        if k.unbilled_coverage_dollars is not None
        else None,
    }


@router.get("")
def list_kpis(
    db: Session = Depends(get_db),
    period: str = Query(default="2026-10"),
) -> dict[str, Any]:
    snaps = list(
        db.scalars(
            select(KPISnapshot)
            .where(KPISnapshot.period == period)
            .order_by(KPISnapshot.building)
        ).all()
    )
    return {"period": period, "kpis": [_snap_dict(s) for s in snaps]}


@router.get("/ar-aging")
def get_ar_aging(
    db: Session = Depends(get_db),
    recompute: bool = Query(default=False),
) -> dict[str, Any]:
    if recompute:
        rows = compute_ar_aging(db)
    else:
        rows = compute_ar_aging(db, refresh_buckets=False)
        # If empty buckets all zero and no source rows, still fine
    return {"as_of": "2026-10-31", "buckets": rows}


@router.get("/building/{building_id}")
def kpi_for_building(
    building_id: int,
    db: Session = Depends(get_db),
    period: str = Query(default="2026-10"),
) -> dict[str, Any]:
    building = db.get(Building, building_id)
    if building is None:
        raise HTTPException(status_code=404, detail="Building not found")
    snap = db.scalar(
        select(KPISnapshot).where(
            KPISnapshot.building_id == building_id,
            KPISnapshot.period == period,
        )
    )
    if snap is None:
        raise HTTPException(status_code=404, detail="KPI snapshot not found")
    return _snap_dict(snap)


@router.post("/recompute")
def recompute_kpis(
    db: Session = Depends(get_db),
    write_csv: bool = Query(default=True),
) -> dict[str, Any]:
    if write_csv:
        path = write_kpis_by_building(db)
        ar_path = write_ar_aging(db)
        rows = compute_kpis_by_building(db, persist=False)
        return {
            "kpis": rows,
            "csv": str(path),
            "ar_aging_csv": str(ar_path),
        }
    rows = compute_kpis_by_building(db, persist=True)
    ar = compute_ar_aging(db)
    return {"kpis": rows, "ar_aging": ar}
