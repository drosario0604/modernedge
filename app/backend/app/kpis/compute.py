"""Compute per-building KPIs for October 2026 (Mon–Fri)."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.kpis.writers import KPI_COLUMNS, round_money, write_kpis_csv
from app.models import (
    Building,
    CalloutEvent,
    Client,
    DispatchPosting,
    Employee,
    FinanceInvoiceLine,
    FinanceLaborCost,
    KPISnapshot,
)

logger = logging.getLogger(__name__)

PERIOD = "2026-10"
PERIOD_START = date(2026, 10, 1)
PERIOD_END = date(2026, 10, 31)
AS_OF = PERIOD_END

UNION_BURDEN = Decimal("0.32")
NONUNION_BURDEN = Decimal("0.18")
OT_MULTIPLIER = Decimal("1.5")
SHIFT_HOURS = Decimal("8")

# Re-export for callers / package __init__
__all__ = [
    "KPI_COLUMNS",
    "PERIOD",
    "compute_kpis_by_building",
    "write_kpis_by_building",
    "billable_coverage_amount",
    "october_weekdays",
]


def october_weekdays(
    start: date = PERIOD_START, end: date = PERIOD_END
) -> list[date]:
    """Return Mon–Fri dates inclusive within the period."""
    days: list[date] = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


def _burden_rate(employee: Employee | None) -> Decimal:
    if employee is None:
        return UNION_BURDEN
    if employee.union_member:
        return UNION_BURDEN
    return NONUNION_BURDEN


def billable_coverage_amount(
    *,
    base_rate: Decimal | float | str | None,
    fill_type: str | None,
    burden: Decimal | float | str | None,
    markup: Decimal | float | str | None,
) -> Decimal:
    """
    8 × base × OT mult × (1+burden) × (1+markup), rounded to the cent.
    """
    rate = Decimal(str(base_rate or 0))
    bur = Decimal(str(burden if burden is not None else 0))
    mk = Decimal(str(markup if markup is not None else 0))
    ot = OT_MULTIPLIER if (fill_type or "").upper() in {"OT", "OVERTIME"} else Decimal("1")
    amount = SHIFT_HOURS * rate * ot * (Decimal("1") + bur) * (Decimal("1") + mk)
    return round_money(amount)


def _home_building_id_on(
    employee: Employee, on_date: date, building_by_code: dict[str, int]
) -> Optional[int]:
    """
    Resolve home building for a day.

    Prefers meta['home_history'] entries
    ({effective_date, building_id|building_name|hr_location}) when present;
    otherwise uses employee.building_id / home_location.
    """
    history = None
    if isinstance(employee.meta, dict):
        history = employee.meta.get("home_history") or employee.meta.get("building_history")

    if isinstance(history, list) and history:
        applicable: list[tuple[date, Optional[int]]] = []
        for entry in history:
            if not isinstance(entry, dict):
                continue
            raw_eff = entry.get("effective_date") or entry.get("effective") or entry.get("date")
            if not raw_eff:
                continue
            if isinstance(raw_eff, date):
                eff = raw_eff
            else:
                try:
                    eff = date.fromisoformat(str(raw_eff)[:10])
                except ValueError:
                    continue
            if eff > on_date:
                continue
            bid = entry.get("building_id")
            if bid is None:
                name = entry.get("building_name") or entry.get("building")
                loc = entry.get("hr_location") or entry.get("home_location")
                if name and isinstance(name, str):
                    # reverse lookup filled below by caller via name map if needed
                    bid = entry.get("_resolved_building_id")
                    if bid is None:
                        # stash name resolution via building_by_code only for codes
                        bid = building_by_code.get(str(name))
                if bid is None and loc:
                    bid = building_by_code.get(str(loc))
            if bid is not None:
                applicable.append((eff, int(bid)))
        if applicable:
            applicable.sort(key=lambda t: t[0])
            return applicable[-1][1]

    if employee.building_id is not None:
        return employee.building_id
    if employee.home_location:
        return building_by_code.get(employee.home_location)
    return None


def _employed_on(employee: Employee, on_date: date) -> bool:
    if employee.hire_date and employee.hire_date > on_date:
        return False
    if employee.termination_date and employee.termination_date < on_date:
        return False
    return True


def _is_permanent(employee: Employee) -> bool:
    et = (employee.employment_type or "").upper()
    return et == "PERMANENT"


def _active_on_as_of(employee: Employee, as_of: date = AS_OF) -> bool:
    if not _is_permanent(employee):
        return False
    if (employee.status or "").upper() == "TERMINATED":
        # Still count if terminated after as_of
        if employee.termination_date and employee.termination_date > as_of:
            pass
        elif employee.termination_date and employee.termination_date <= as_of:
            return False
        else:
            return False
    if employee.hire_date and employee.hire_date > as_of:
        return False
    if employee.termination_date and employee.termination_date <= as_of:
        return False
    # Exclude float / FLOAT home
    home = (employee.home_location or "").upper()
    if home in {"FLOAT", "FLOATER"}:
        return False
    return True


def _coverage_invoice_keys(lines: list[FinanceInvoiceLine]) -> set[str]:
    """
    Build a set of keys that indicate a coverage line was billed.

    Matching is intentionally loose: posting_id in description, or
    site+date+shift tokens, plus any explicit meta posting_id.
    """
    keys: set[str] = set()
    for line in lines:
        lt = (line.line_type or "").lower()
        desc = (line.description or "").lower()
        if "coverage" not in lt and "coverage" not in desc and "cover" not in lt:
            # still allow explicit posting refs
            pass
        if line.raw and isinstance(line.raw, dict):
            pid = line.raw.get("posting_id") or line.raw.get("dispatch_posting_id")
            if pid:
                keys.add(f"posting:{pid}")
        # description may contain posting id like DSP-...
        if line.description:
            for token in str(line.description).replace(",", " ").split():
                if token.upper().startswith("DSP") or token.upper().startswith("POST"):
                    keys.add(f"posting:{token}")
        if line.building_id and line.service_period == PERIOD:
            if "coverage" in lt or "coverage" in desc or "cover" in desc:
                keys.add(f"bldg:{line.building_id}:{line.amount}")
                keys.add(f"bldg_cov:{line.building_id}")
    return keys


def _posting_is_billed(posting: DispatchPosting, keys: set[str]) -> bool:
    if f"posting:{posting.posting_id}" in keys:
        return True
    if posting.building_id is not None and f"bldg_cov:{posting.building_id}" in keys:
        # Conservative: only treat as billed when amount-matched elsewhere;
        # count-level key alone is not enough — require raw/description hit
        # OR an exact posting id. Fall through.
        pass
    if posting.raw and isinstance(posting.raw, dict):
        inv = posting.raw.get("invoice_matched")
        if inv:
            return True
    return f"posting:{posting.posting_id}" in keys


def compute_kpis_by_building(
    session: Session,
    *,
    period: str = PERIOD,
    persist: bool = True,
) -> list[dict[str, Any]]:
    """
    Compute all KPIs for every building for the assessment period.

    Returns one dict per building with keys matching KPI_COLUMNS.
    """
    weekdays = october_weekdays()
    buildings = list(session.scalars(select(Building).where(Building.active.is_(True))).all())
    if not buildings:
        buildings = list(session.scalars(select(Building)).all())

    clients = {c.id: c for c in session.scalars(select(Client)).all()}
    employees = list(session.scalars(select(Employee)).all())
    callouts = list(
        session.scalars(
            select(CalloutEvent).where(
                CalloutEvent.event_date >= PERIOD_START,
                CalloutEvent.event_date <= PERIOD_END,
            )
        ).all()
    )
    postings = list(
        session.scalars(
            select(DispatchPosting).where(
                DispatchPosting.shift_date >= PERIOD_START,
                DispatchPosting.shift_date <= PERIOD_END,
            )
        ).all()
    )
    invoice_lines = list(
        session.scalars(
            select(FinanceInvoiceLine).where(FinanceInvoiceLine.service_period == period)
        ).all()
    )
    labor_rows = list(
        session.scalars(
            select(FinanceLaborCost).where(FinanceLaborCost.period == period)
        ).all()
    )

    building_by_code: dict[str, int] = {}
    building_by_name: dict[str, Building] = {}
    for b in buildings:
        building_by_name[b.name] = b
        if b.code:
            building_by_code[b.code] = b.id
        if b.hr_location_code:
            building_by_code[b.hr_location_code] = b.id
        if b.dispatch_site_id:
            building_by_code[b.dispatch_site_id] = b.id
        if b.finance_site_name:
            building_by_code[b.finance_site_name] = b.id
        building_by_code[b.name] = b.id

    # Enrich home_history name → id on employees for lookups
    for emp in employees:
        if not isinstance(emp.meta, dict):
            continue
        history = emp.meta.get("home_history") or emp.meta.get("building_history")
        if not isinstance(history, list):
            continue
        for entry in history:
            if not isinstance(entry, dict):
                continue
            if entry.get("building_id") is not None:
                continue
            name = entry.get("building_name") or entry.get("building")
            if name and name in building_by_name:
                entry["_resolved_building_id"] = building_by_name[name].id
                entry["building_id"] = building_by_name[name].id

    coverage_keys = _coverage_invoice_keys(invoice_lines)
    emp_by_id = {e.id: e for e in employees}

    # Dedup call-outs: one per employee+date (prefer resolved employee_id)
    callout_seen: set[tuple[Any, date]] = set()
    callouts_by_building: dict[int, int] = {b.id: 0 for b in buildings}
    for co in callouts:
        if co.is_absence is False:
            continue
        if co.event_date is None:
            continue
        if co.event_date.weekday() >= 5:
            continue
        # Planned vacation is not a call-out
        reason = (co.reason_code or co.reason_text or "").lower()
        if "vacation" in reason or (co.structured or {}).get("reason_category") == "vacation":
            continue
        if co.is_absence is None and not reason:
            # unparsed — skip unless structured marks absence
            if not (co.structured or {}).get("is_absence", True):
                continue

        key_emp = co.employee_id if co.employee_id is not None else (co.employee_name_raw or co.source_ref)
        dedupe_key = (key_emp, co.event_date)
        if dedupe_key in callout_seen:
            continue
        callout_seen.add(dedupe_key)

        bldg_id = co.building_id
        if bldg_id is None and co.employee_id is not None:
            emp = emp_by_id.get(co.employee_id)
            if emp:
                bldg_id = _home_building_id_on(emp, co.event_date, building_by_code)
        if bldg_id is None and co.building_raw:
            bldg_id = building_by_code.get(co.building_raw)
        if bldg_id is not None and bldg_id in callouts_by_building:
            callouts_by_building[bldg_id] += 1

    rows: list[dict[str, Any]] = []

    for building in sorted(buildings, key=lambda b: b.name):
        client = clients.get(building.client_id) if building.client_id else None
        day_posts = building.contracted_day_posts or 0
        night_posts = building.contracted_night_posts or 0
        contracted = int(day_posts) + int(night_posts)

        # Active headcount on Oct 31
        active = 0
        for emp in employees:
            if not _active_on_as_of(emp, AS_OF):
                continue
            home_id = _home_building_id_on(emp, AS_OF, building_by_code)
            if home_id == building.id:
                active += 1

        # Scheduled shifts: permanent employees, each weekday employed, at home bldg
        scheduled = 0
        for emp in employees:
            if not _is_permanent(emp):
                continue
            home = (emp.home_location or "").upper()
            if home in {"FLOAT", "FLOATER"}:
                continue
            for d in weekdays:
                if not _employed_on(emp, d):
                    continue
                home_id = _home_building_id_on(emp, d, building_by_code)
                if home_id == building.id:
                    scheduled += 1

        callout_shifts = callouts_by_building.get(building.id, 0)
        callout_rate = (
            Decimal(callout_shifts) / Decimal(scheduled) if scheduled else Decimal("0")
        )

        # Fill rate: filled / all non-cancelled postings for building
        bldg_postings = [
            p
            for p in postings
            if p.building_id == building.id
            and (p.status or "").upper() != "CANCELLED"
        ]
        filled = [p for p in bldg_postings if (p.status or "").upper() == "FILLED"]
        fill_rate = (
            Decimal(len(filled)) / Decimal(len(bldg_postings))
            if bldg_postings
            else Decimal("0")
        )

        # Overtime hours / dollars on coverage fills at this building
        ot_hours = Decimal("0")
        ot_dollars = Decimal("0")
        for p in filled:
            if (p.fill_type or "").upper() != "OT":
                continue
            ot_hours += SHIFT_HOURS
            cover = emp_by_id.get(p.covering_employee_id) if p.covering_employee_id else None
            rate = Decimal(str(cover.base_rate)) if cover and cover.base_rate is not None else Decimal("0")
            ot_dollars += SHIFT_HOURS * rate * OT_MULTIPLIER
        ot_dollars = round_money(ot_dollars)

        # Revenue
        revenue = round_money(
            sum(
                (ln.amount or Decimal("0"))
                for ln in invoice_lines
                if ln.building_id == building.id
            )
        )

        # Labor cost
        labor_cost = round_money(
            sum(
                (lc.amount or Decimal("0"))
                for lc in labor_rows
                if lc.building_id == building.id
            )
        )

        gross_margin = round_money(revenue - labor_cost)
        gross_margin_pct = (
            (gross_margin / revenue) if revenue != 0 else Decimal("0")
        )

        # Unbilled coverage (cost-plus buildings only)
        unbilled_count = 0
        unbilled_dollars = Decimal("0.00")
        billing_type = (client.billing_type or "").lower() if client else ""
        coverage_billable = billing_type in {"cost_plus", "cost-plus", "costplus"}
        if client and client.meta and isinstance(client.meta, dict):
            if client.meta.get("coverage_billable") is False:
                coverage_billable = False
            elif client.meta.get("coverage_billable") is True:
                coverage_billable = True

        if coverage_billable:
            markup = client.cost_plus_markup if client and client.cost_plus_markup is not None else Decimal("0")
            for p in filled:
                if (p.reason or "").upper() not in {"CALLOUT", "VACATION"}:
                    continue
                if _posting_is_billed(p, coverage_keys):
                    continue
                # Also check invoice lines for this building+date in description
                matched = False
                for ln in invoice_lines:
                    if ln.building_id != building.id:
                        continue
                    desc = (ln.description or "").lower()
                    lt = (ln.line_type or "").lower()
                    if "coverage" not in desc and "coverage" not in lt and "cover" not in desc:
                        continue
                    if p.posting_id and p.posting_id.lower() in desc:
                        matched = True
                        break
                    if p.shift_date and str(p.shift_date) in desc:
                        matched = True
                        break
                if matched:
                    continue

                cover = emp_by_id.get(p.covering_employee_id) if p.covering_employee_id else None
                rate = cover.base_rate if cover else None
                amt = billable_coverage_amount(
                    base_rate=rate,
                    fill_type=p.fill_type,
                    burden=_burden_rate(cover),
                    markup=markup,
                )
                unbilled_count += 1
                unbilled_dollars += amt
            unbilled_dollars = round_money(unbilled_dollars)

        row: dict[str, Any] = {
            "building": building.name,
            "contracted_headcount": contracted,
            "active_headcount": active,
            "headcount_variance": active - contracted,
            "scheduled_shifts": scheduled,
            "callout_shifts": callout_shifts,
            "callout_rate": callout_rate,
            "fill_rate": fill_rate,
            "overtime_hours": ot_hours,
            "overtime_dollars": ot_dollars,
            "revenue": revenue,
            "labor_cost": labor_cost,
            "gross_margin": gross_margin,
            "gross_margin_pct": gross_margin_pct,
            "unbilled_coverage_count": unbilled_count,
            "unbilled_coverage_dollars": unbilled_dollars,
        }
        rows.append(row)

        if persist:
            existing = session.scalar(
                select(KPISnapshot).where(
                    KPISnapshot.building_id == building.id,
                    KPISnapshot.period == period,
                )
            )
            snap = existing or KPISnapshot(building_id=building.id, building=building.name, period=period)
            for col in KPI_COLUMNS:
                if col == "building":
                    snap.building = building.name
                else:
                    setattr(snap, col, row[col])
            if existing is None:
                session.add(snap)

    if persist:
        session.commit()

    logger.info("Computed KPIs for %s buildings", len(rows))
    return rows


def write_kpis_by_building(
    session: Session,
    *,
    output_path: Path | str | None = None,
    period: str = PERIOD,
    persist: bool = True,
) -> Path:
    """Compute KPIs and write output/kpis_by_building.csv."""
    settings = get_settings()
    path = Path(output_path) if output_path else settings.output_dir / "kpis_by_building.csv"
    rows = compute_kpis_by_building(session, period=period, persist=persist)
    return write_kpis_csv(path, rows)
