"""Deterministic Article 14 absence coding (no LLM)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.callouts.extract import ExtractedCallout
from app.models import Employee

logger = logging.getLogger(__name__)

DAY_START = time(7, 0)
NIGHT_START = time(16, 0)
NOTICE_HOURS = timedelta(hours=4)
PROBATION_DAYS = 90
AS_OF_SICK_BALANCE = date(2026, 10, 1)

PAID_CODES = frozenset({"SKP", "CLN", "BRV", "JRY"})


@dataclass
class CodedAbsence:
    source: str
    source_ref: str
    employee_id: Optional[int]  # internal DB id
    payroll_employee_id: Optional[int]
    hr_employee_id: Optional[str]
    event_date: date
    shift: Optional[str]
    reason_category: str
    code: str
    hours: Decimal = Decimal("8")
    notice_time: Optional[datetime] = None
    notice_ok: Optional[bool] = None
    confidence: float = 0.0
    decision_hint: str = "write"  # write | review
    reason: str = ""
    building_hint: Optional[str] = None
    meta: dict[str, Any] = field(default_factory=dict)


def shift_start_datetime(event_date: date, shift: Optional[str]) -> datetime:
    """Day 07:00 / Night 16:00 local (naive)."""
    s = (shift or "DAY").upper()
    if s in {"N", "NIGHT"}:
        return datetime.combine(event_date, NIGHT_START)
    return datetime.combine(event_date, DAY_START)


def measure_notice(
    notice_time: Optional[datetime],
    event_date: date,
    shift: Optional[str],
) -> tuple[Optional[bool], Optional[timedelta]]:
    """
    Return (gave_required_notice, lead_time).

    Notice is measured from the time the call-out is received vs shift start.
    """
    if notice_time is None:
        return None, None
    start = shift_start_datetime(event_date, shift)
    # Normalize timezone-aware to naive for comparison
    nt = notice_time
    if nt.tzinfo is not None:
        nt = nt.replace(tzinfo=None)
    lead = start - nt
    return lead >= NOTICE_HOURS, lead


def eligible_for_paid_sick_or_clinic(employee: Employee, on_date: date) -> bool:
    """Union + 90 calendar days of employment (Art. 14.3 / 14.10)."""
    if not employee.union_member:
        return False
    if not employee.hire_date:
        return False
    return (on_date - employee.hire_date).days >= PROBATION_DAYS


def _match_employee(
    session: Session,
    hint: str,
    employees: list[Employee] | None = None,
) -> tuple[Optional[Employee], float]:
    """Fuzzy match employee_hint to roster; returns (employee, confidence)."""
    employees = employees or list(session.scalars(select(Employee)).all())
    hint_clean = hint.strip().lower()
    hint_clean = hint_clean.replace(".", "")
    parts = [p for p in hint_clean.replace(",", " ").split() if p]
    if not parts:
        return None, 0.0

    best: Optional[Employee] = None
    best_score = 0.0

    for emp in employees:
        first = (emp.first_name or "").lower()
        last = (emp.last_name or "").lower()
        display = (emp.display_name or "").lower()
        score = 0.0

        if hint_clean in {f"{first} {last}", f"{last}, {first}", display}:
            score = 1.0
        elif len(parts) >= 2:
            # first + last initial or last name
            if parts[0] == first and (parts[-1] == last or parts[-1][0] == last[:1]):
                score = 0.92 if parts[-1] == last else 0.8
            elif parts[0] == last and parts[-1] == first:
                score = 0.9
            elif parts[0] == first and last.startswith(parts[-1]):
                score = 0.85
        elif len(parts) == 1:
            token = parts[0]
            if token == first:
                # ambiguous first name
                score = 0.55
            elif token == last:
                score = 0.6
            elif first.startswith(token) and len(token) >= 3:
                score = 0.5

        # Spanish/Albanian log often uses first name only — boost if unique
        if score == 0.55:
            same_first = [
                e
                for e in employees
                if (e.first_name or "").lower() == parts[0]
                and (e.status or "").upper() == "ACTIVE"
            ]
            if len(same_first) == 1:
                score = 0.82
                emp = same_first[0]

        if score > best_score:
            best_score = score
            best = emp

    if best_score < 0.5:
        return None, best_score
    return best, best_score


def _default_shift(employee: Employee | None, extracted: ExtractedCallout) -> Optional[str]:
    if extracted.shift:
        return extracted.shift
    if employee and employee.shift:
        s = employee.shift.upper()
        if s in {"DAY", "NIGHT"}:
            return s
    return extracted.shift


def code_one(
    extracted: ExtractedCallout,
    employee: Employee | None,
    *,
    sick_balance: Decimal,
    clinic_used: bool,
    match_confidence: float,
) -> CodedAbsence:
    """
    Apply Article 14 coding in order: eligibility → notice → balance.

    Mapping:
      sick → SKP / SKU / LTN
      clinic → CLN / PER (or LTN if late and would have been paid)
      bereavement → BRV / LTN
      jury → JRY
      family → FEM
      personal → PER
    """
    category = (extracted.reason_category or "unknown").lower()
    shift = _default_shift(employee, extracted)
    notice_ok, lead = measure_notice(extracted.notice_time, extracted.event_date, shift)

    base_conf = min(extracted.confidence, match_confidence if employee else extracted.confidence * 0.4)
    reason_parts: list[str] = []

    if category == "vacation":
        return CodedAbsence(
            source=extracted.source,
            source_ref=extracted.source_ref,
            employee_id=employee.id if employee else None,
            payroll_employee_id=employee.payroll_employee_id if employee else None,
            hr_employee_id=employee.hr_employee_id if employee else None,
            event_date=extracted.event_date,
            shift=shift,
            reason_category=category,
            code="",
            notice_time=extracted.notice_time,
            notice_ok=notice_ok,
            confidence=base_conf,
            decision_hint="skip",
            reason="Planned vacation is not a call-out",
            building_hint=extracted.building_hint,
            meta={"sick_balance": str(sick_balance), "clinic_used": clinic_used},
        )

    code = "PER"
    decision = "write"

    if employee is None:
        decision = "review"
        reason_parts.append("Could not resolve employee from hint")
        code = ""
    elif category == "jury":
        # 14.6 — no notice requirement; all employees
        code = "JRY"
        reason_parts.append("Jury duty (Art. 14.6); notice rule N/A")
    elif category == "family":
        code = "FEM"
        reason_parts.append("Family emergency unpaid (Art. 14.7)")
    elif category == "personal":
        code = "PER"
        reason_parts.append("Personal unpaid (Art. 14.8)")
    elif category == "bereavement":
        # All employees; paid if notice ok else LTN
        if notice_ok is False:
            code = "LTN"
            reason_parts.append("Bereavement would be paid but late notice (Art. 14.9)")
        elif notice_ok is None:
            code = "BRV"
            decision = "review"
            reason_parts.append("Bereavement: notice time unknown — default BRV pending review")
        else:
            code = "BRV"
            reason_parts.append("Bereavement paid with required notice (Art. 14.5)")
    elif category == "clinic":
        eligible = eligible_for_paid_sick_or_clinic(employee, extracted.event_date)
        if not eligible:
            code = "PER"
            reason_parts.append("Clinic: not eligible (non-union or <90 days) → PER")
        elif clinic_used:
            code = "PER"
            reason_parts.append("Clinic: annual clinic day already used → PER")
        elif notice_ok is False:
            code = "LTN"
            reason_parts.append("Clinic would be paid but late notice → LTN")
        elif notice_ok is None:
            code = "CLN"
            decision = "review"
            reason_parts.append("Clinic: notice unknown — default CLN pending review")
        else:
            code = "CLN"
            reason_parts.append("Clinic day paid (Art. 14.4)")
    elif category == "sick":
        eligible = eligible_for_paid_sick_or_clinic(employee, extracted.event_date)
        would_be_paid = eligible and sick_balance > 0
        if notice_ok is False and would_be_paid:
            code = "LTN"
            reason_parts.append("Sick would be paid but late notice → LTN")
        elif not eligible:
            code = "SKU"
            reason_parts.append("Sick unpaid: not eligible for paid sick leave")
        elif sick_balance <= 0:
            code = "SKU"
            reason_parts.append("Sick unpaid: no sick days remaining")
        elif notice_ok is None:
            code = "SKP"
            decision = "review"
            reason_parts.append("Sick: notice unknown — default SKP pending review")
        else:
            code = "SKP"
            reason_parts.append("Sick paid with notice and balance (Art. 14.3)")
    else:
        code = "PER"
        decision = "review"
        reason_parts.append(f"Unknown reason category '{category}' — held pattern PER")

    if base_conf < 0.7 and decision == "write":
        decision = "review"
        reason_parts.append(f"Low confidence ({base_conf:.2f})")

    if lead is not None:
        reason_parts.append(f"notice_lead={lead}")

    return CodedAbsence(
        source=extracted.source,
        source_ref=extracted.source_ref,
        employee_id=employee.id if employee else None,
        payroll_employee_id=employee.payroll_employee_id if employee else None,
        hr_employee_id=employee.hr_employee_id if employee else None,
        event_date=extracted.event_date,
        shift=shift,
        reason_category=category,
        code=code,
        notice_time=extracted.notice_time,
        notice_ok=notice_ok,
        confidence=base_conf,
        decision_hint=decision,
        reason="; ".join(reason_parts),
        building_hint=extracted.building_hint,
        meta={
            "sick_balance_before": str(sick_balance),
            "clinic_used_before": clinic_used,
            "match_confidence": match_confidence,
        },
    )


def code_callouts(
    session: Session,
    extracted: Iterable[ExtractedCallout],
) -> list[CodedAbsence]:
    """
    Code all extracted call-outs deterministically.

    Sick balances are taken from the roster as of Oct 1 and consumed in
    date order when SKP is assigned. Clinic day is consumed once on CLN.
    """
    employees = list(session.scalars(select(Employee)).all())
    # Working balances keyed by employee DB id
    sick_left: dict[int, Decimal] = {}
    clinic_used: dict[int, bool] = {}
    for emp in employees:
        sick_left[emp.id] = Decimal(str(emp.sick_days_remaining or 0))
        clinic_used[emp.id] = bool(emp.clinic_day_used)

    # Sort by date for balance consumption
    items = [e for e in extracted if e.is_absence and e.reason_category != "vacation"]
    items.sort(key=lambda e: (e.event_date, e.source_ref))

    coded: list[CodedAbsence] = []
    for item in items:
        emp, match_conf = _match_employee(session, item.employee_hint, employees)
        bal = sick_left.get(emp.id, Decimal("0")) if emp else Decimal("0")
        used = clinic_used.get(emp.id, False) if emp else False
        result = code_one(
            item,
            emp,
            sick_balance=bal,
            clinic_used=used,
            match_confidence=match_conf,
        )
        # Consume balances only when we would write a paid sick/clinic code
        if emp and result.code == "SKP":
            sick_left[emp.id] = max(Decimal("0"), bal - Decimal("1"))
            result.meta["sick_balance_after"] = str(sick_left[emp.id])
        if emp and result.code == "CLN":
            clinic_used[emp.id] = True
            result.meta["clinic_used_after"] = True
        coded.append(result)

    logger.info("Coded %s absences", len(coded))
    return coded
