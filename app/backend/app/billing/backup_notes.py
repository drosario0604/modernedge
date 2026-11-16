"""Generate Template A/B coverage backup notes for cost-plus filled postings."""

from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.kpis.compute import UNION_BURDEN, NONUNION_BURDEN, billable_coverage_amount
from app.models import (
    Building,
    Client,
    CoverageBackupNote,
    DispatchPosting,
    Employee,
    ExceptionRecord,
    Punch,
)

logger = logging.getLogger(__name__)

DAY_HOURS = ("07:00", "15:00")
NIGHT_HOURS = ("16:00", "00:00")


def _burden(employee: Employee | None) -> Decimal:
    if employee is None:
        return UNION_BURDEN
    return UNION_BURDEN if employee.union_member else NONUNION_BURDEN


def _template_for_client(client: Client | None, building_name: str) -> str:
    """
    Meridian → Template A (full detail); Corlett → Template B (no names).

    Falls back to client.backup_note_template when set.
    """
    if client and client.backup_note_template:
        t = client.backup_note_template.strip().upper()
        if t in {"A", "TEMPLATE_A", "TEMPLATE A", "FULL"}:
            return "A"
        if t in {"B", "TEMPLATE_B", "TEMPLATE B", "NO_NAMES", "NO NAMES"}:
            return "B"

    name = (client.name if client else "") or ""
    blob = f"{name} {building_name}".lower()
    if "meridian" in blob:
        return "A"
    if "corlett" in blob:
        return "B"
    # Default to A for other cost-plus if unspecified
    return "A"


def _shift_label(shift: str | None) -> tuple[str, str, str]:
    s = (shift or "").upper()
    if s in {"N", "NIGHT"}:
        return "Night", NIGHT_HOURS[0], NIGHT_HOURS[1]
    return "Day", DAY_HOURS[0], DAY_HOURS[1]


def _fmt_date(d: date | None) -> str:
    if d is None:
        return ""
    return d.strftime("%m/%d/%Y")


def _fmt_money(amount: Decimal) -> str:
    return f"${amount:.2f}"


def _coverage_type(fill_type: str | None) -> str:
    if (fill_type or "").upper() == "OT":
        return "Overtime"
    return "Replacement"


def _punch_window(
    session: Session,
    covering: Employee | None,
    building_id: int | None,
    shift_date: date | None,
    shift: str | None,
) -> tuple[str, str, bool]:
    """
    Return (clock_in, clock_out, missing) for Template A.
    Missing times → 'Not recorded'.
    """
    if covering is None or shift_date is None:
        return "Not recorded", "Not recorded", True

    # Day: same calendar date; Night: afternoon through next morning
    if (shift or "").upper() in {"N", "NIGHT"}:
        start_bound = datetime.combine(shift_date, dtime(12, 0))
        end_bound = datetime.combine(shift_date + timedelta(days=1), dtime(6, 0))
    else:
        start_bound = datetime.combine(shift_date, dtime(5, 0))
        end_bound = datetime.combine(shift_date, dtime(18, 0))

    q = select(Punch).where(Punch.employee_id == covering.id)
    if building_id is not None:
        q = q.where(
            (Punch.building_id == building_id) | (Punch.building_id.is_(None))
        )
    punches = list(session.scalars(q).all())
    timed = [
        p
        for p in punches
        if p.punch_time is not None
        and start_bound
        <= (p.punch_time.replace(tzinfo=None) if p.punch_time.tzinfo else p.punch_time)
        <= end_bound
    ]
    if not timed:
        return "Not recorded", "Not recorded", True

    timed.sort(
        key=lambda p: p.punch_time.replace(tzinfo=None) if p.punch_time and p.punch_time.tzinfo else p.punch_time  # type: ignore[arg-type]
    )
    first = timed[0].punch_time
    last = timed[-1].punch_time
    assert first is not None and last is not None
    first_naive = first.replace(tzinfo=None) if first.tzinfo else first
    last_naive = last.replace(tzinfo=None) if last.tzinfo else last
    return first_naive.strftime("%H:%M"), last_naive.strftime("%H:%M"), False


def render_template_a(
    *,
    client_name: str,
    building_name: str,
    shift_date: date | None,
    shift: str | None,
    position: str,
    fill_type: str | None,
    replacement_name: str,
    on_site_in: str,
    on_site_out: str,
    billable_amount: Decimal,
    posting_id: str,
) -> str:
    shift_name, start, end = _shift_label(shift)
    return "\n".join(
        [
            "COVERAGE BACKUP NOTE",
            f"Client:            {client_name}",
            f"Building:          {building_name}",
            f"Date of service:   {_fmt_date(shift_date)}",
            f"Shift:             {shift_name}, {start} to {end}",
            f"Position covered:  {position}",
            f"Coverage type:     {_coverage_type(fill_type)}",
            f"Replacement:       {replacement_name}",
            f"On site:           {on_site_in} to {on_site_out}",
            "Billable hours:    8.00",
            f"Billable amount:   {_fmt_money(billable_amount)}",
            f"Posting reference: {posting_id}",
            "",
        ]
    )


def render_template_b(
    *,
    client_name: str,
    building_name: str,
    shift_date: date | None,
    shift: str | None,
    position: str,
    billable_amount: Decimal,
    posting_id: str,
) -> str:
    shift_name, _, _ = _shift_label(shift)
    return "\n".join(
        [
            "COVERAGE BACKUP NOTE",
            f"Client:            {client_name}",
            f"Building:          {building_name}",
            f"Date of service:   {_fmt_date(shift_date)}",
            f"Shift:             {shift_name}",
            f"Position covered:  {position}",
            "Coverage:          Replacement provided",
            "Billable hours:    8.00",
            f"Billable amount:   {_fmt_money(billable_amount)}",
            f"Posting reference: {posting_id}",
            "",
        ]
    )


def generate_backup_notes(
    session: Session,
    *,
    output_dir: Path | str | None = None,
) -> list[CoverageBackupNote]:
    """
    For each filled coverage posting at a cost-plus building, write a backup note.

    Never names the absent employee. Template A for Meridian, B for Corlett.
    """
    settings = get_settings()
    out_root = Path(output_dir) if output_dir else settings.output_dir / "backup_notes"
    out_root.mkdir(parents=True, exist_ok=True)

    buildings = {b.id: b for b in session.scalars(select(Building)).all()}
    clients = {c.id: c for c in session.scalars(select(Client)).all()}
    employees = {e.id: e for e in session.scalars(select(Employee)).all()}

    postings = list(
        session.scalars(
            select(DispatchPosting).where(
                DispatchPosting.status == "FILLED",
            )
        ).all()
    )

    notes: list[CoverageBackupNote] = []
    for posting in postings:
        if (posting.reason or "").upper() not in {"CALLOUT", "VACATION"}:
            continue
        building = buildings.get(posting.building_id) if posting.building_id else None
        if building is None:
            continue
        client = clients.get(building.client_id) if building.client_id else None
        billing = (client.billing_type or "").lower() if client else ""
        if billing not in {"cost_plus", "cost-plus", "costplus"}:
            continue

        covering = (
            employees.get(posting.covering_employee_id)
            if posting.covering_employee_id
            else None
        )
        # Position = job class of the *absent* employee — allowed; name is not.
        absent = (
            employees.get(posting.absent_employee_id)
            if posting.absent_employee_id
            else None
        )
        position = (
            (absent.job_class if absent and absent.job_class else None)
            or "Cleaner"
        )
        # Never echo absent name from absent_employee_text into the note.

        markup = (
            client.cost_plus_markup
            if client and client.cost_plus_markup is not None
            else Decimal("0")
        )
        amount = billable_coverage_amount(
            base_rate=covering.base_rate if covering else Decimal("0"),
            fill_type=posting.fill_type,
            burden=_burden(covering),
            markup=markup,
        )

        template = _template_for_client(client, building.name)
        client_name = client.name if client else "Client"
        missing_punches = False

        if template == "A":
            on_in, on_out, missing_punches = _punch_window(
                session, covering, building.id, posting.shift_date, posting.shift
            )
            replacement = (
                f"{covering.first_name} {covering.last_name}".strip()
                if covering
                else "Not recorded"
            )
            text = render_template_a(
                client_name=client_name,
                building_name=building.name,
                shift_date=posting.shift_date,
                shift=posting.shift,
                position=position,
                fill_type=posting.fill_type,
                replacement_name=replacement,
                on_site_in=on_in,
                on_site_out=on_out,
                billable_amount=amount,
                posting_id=posting.posting_id,
            )
            if missing_punches:
                session.add(
                    ExceptionRecord(
                        source="backup_notes",
                        record_ref=posting.posting_id,
                        issue="Missing clock-in/out for Template A backup note",
                        action_taken="Wrote 'Not recorded' for on-site times",
                        confidence=Decimal("1.0000"),
                    )
                )
        else:
            text = render_template_b(
                client_name=client_name,
                building_name=building.name,
                shift_date=posting.shift_date,
                shift=posting.shift,
                position=position,
                billable_amount=amount,
                posting_id=posting.posting_id,
            )

        # Safety: ensure absent free-text name never appears
        absent_text = (posting.absent_employee_text or "").strip()
        if absent_text and absent_text in text:
            text = text.replace(absent_text, "[redacted]")

        file_path = out_root / f"{posting.posting_id}.txt"
        file_path.write_text(text, encoding="utf-8")

        existing = session.scalar(
            select(CoverageBackupNote).where(
                CoverageBackupNote.posting_id == posting.posting_id
            )
        )
        note = existing or CoverageBackupNote(posting_id=posting.posting_id)
        note.dispatch_posting_id = posting.id
        note.building_id = building.id
        note.client_id = client.id if client else None
        note.covering_employee_id = covering.id if covering else None
        note.shift_date = posting.shift_date
        note.fill_type = posting.fill_type
        note.billable_amount = amount
        note.note_text = text
        note.file_path = str(file_path)
        note.template_used = f"Template {template}"
        note.meta = {"missing_punches": missing_punches}
        if existing is None:
            session.add(note)
        notes.append(note)

    session.commit()
    logger.info("Wrote %s backup notes to %s", len(notes), out_root)
    return notes


def write_backup_notes(session: Session, output_dir: Path | str | None = None) -> list[Path]:
    notes = generate_backup_notes(session, output_dir=output_dir)
    return [Path(n.file_path) for n in notes if n.file_path]
