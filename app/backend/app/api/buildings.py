"""Building listing and detail endpoints."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Building, Client, Employee, KPISnapshot

router = APIRouter(prefix="/buildings", tags=["buildings"])


def _building_dict(b: Building, client: Client | None = None) -> dict[str, Any]:
    return {
        "id": b.id,
        "code": b.code,
        "name": b.name,
        "hr_location_code": b.hr_location_code,
        "dispatch_site_id": b.dispatch_site_id,
        "finance_site_name": b.finance_site_name,
        "department_code": b.department_code,
        "contracted_day_posts": b.contracted_day_posts,
        "contracted_night_posts": b.contracted_night_posts,
        "contracted_headcount": (b.contracted_day_posts or 0) + (b.contracted_night_posts or 0),
        "active": b.active,
        "client": (
            {
                "id": client.id,
                "code": client.code,
                "name": client.name,
                "billing_type": client.billing_type,
                "cost_plus_markup": float(client.cost_plus_markup)
                if client.cost_plus_markup is not None
                else None,
                "backup_note_template": client.backup_note_template,
            }
            if client
            else None
        ),
    }


@router.get("")
def list_buildings(
    db: Session = Depends(get_db),
    active_only: bool = Query(default=True),
) -> dict[str, Any]:
    stmt = select(Building).order_by(Building.name)
    if active_only:
        stmt = stmt.where(Building.active.is_(True))
    buildings = list(db.scalars(stmt).all())
    clients = {c.id: c for c in db.scalars(select(Client)).all()}
    return {
        "buildings": [
            _building_dict(b, clients.get(b.client_id) if b.client_id else None)
            for b in buildings
        ]
    }


@router.get("/{building_id}")
def get_building(building_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    building = db.get(Building, building_id)
    if building is None:
        raise HTTPException(status_code=404, detail="Building not found")
    client = db.get(Client, building.client_id) if building.client_id else None
    emp_count = len(
        list(db.scalars(select(Employee).where(Employee.building_id == building.id)).all())
    )
    kpi = db.scalar(
        select(KPISnapshot).where(
            KPISnapshot.building_id == building.id,
            KPISnapshot.period == "2026-10",
        )
    )
    payload = _building_dict(building, client)
    payload["employee_count"] = emp_count
    payload["kpi"] = _kpi_dict(kpi) if kpi else None
    return payload


@router.get("/by-name/{name}")
def get_building_by_name(name: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    building = db.scalar(select(Building).where(Building.name == name))
    if building is None:
        raise HTTPException(status_code=404, detail="Building not found")
    return get_building(building.id, db)


def _kpi_dict(k: KPISnapshot) -> dict[str, Any]:
    return {
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
