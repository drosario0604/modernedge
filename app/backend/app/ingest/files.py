"""Load client file drops (HR, TA, callouts, contracts, PAF, labor budget)."""

from __future__ import annotations

import csv
import json
import logging
import re
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import openpyxl
from pypdf import PdfReader
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    Building,
    CalloutEvent,
    Client,
    Employee,
    ExceptionRecord,
    Punch,
)

logger = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")

# WhatsApp-style chat line: [10/07/26, 9:47:00 AM] Name: message
CHAT_LINE_RE = re.compile(
    r"^\[(\d{1,2}/\d{1,2}/\d{2,4}),\s*(\d{1,2}:\d{2}(?::\d{2})?\s*[AP]M)\]\s*"
    r"([^:]+):\s*(.*)$",
    re.IGNORECASE,
)

# System / non-message chat lines to skip after the bracketed header
CHAT_SYSTEM_RE = re.compile(
    r"(end-to-end encrypted|created group|Messages and calls)",
    re.IGNORECASE,
)


def _dec(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).strip().replace(",", "").replace("$", ""))
    except (InvalidOperation, ValueError):
        return None


def _parse_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_local_datetime(value: str, *, assume_eastern: bool = True) -> Optional[datetime]:
    """Parse punch/chat timestamps into timezone-aware datetimes."""
    text = (value or "").strip()
    if not text:
        return None

    # ISO with offset
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None and assume_eastern:
            dt = dt.replace(tzinfo=EASTERN)
        return dt
    except ValueError:
        pass

    for fmt in (
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%y %H:%M",
        "%m/%d/%y %I:%M:%S %p",
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%y %I:%M %p",
    ):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(tzinfo=EASTERN) if assume_eastern else dt
        except ValueError:
            continue
    return None


def _yn_bool(value: Any) -> Optional[bool]:
    if value is None or value == "":
        return None
    text = str(value).strip().upper()
    if text in {"Y", "YES", "TRUE", "1"}:
        return True
    if text in {"N", "NO", "FALSE", "0"}:
        return False
    return None


def add_exception(
    session: Session,
    *,
    source: str,
    record_ref: str,
    issue: str,
    action_taken: str,
    confidence: Decimal | float | None = None,
    meta: dict[str, Any] | None = None,
) -> ExceptionRecord:
    row = ExceptionRecord(
        source=source,
        record_ref=record_ref,
        issue=issue,
        action_taken=action_taken,
        confidence=Decimal(str(confidence)) if confidence is not None else None,
        resolved=False,
        meta=meta,
    )
    session.add(row)
    return row


# ---------------------------------------------------------------------------
# HR roster
# ---------------------------------------------------------------------------


def load_hr_roster(session: Session, data_dir: Path) -> list[Employee]:
    path = data_dir / "hr" / "hr_roster.csv"
    if not path.exists():
        add_exception(
            session,
            source="hr_roster",
            record_ref=str(path),
            issue="HR roster file missing",
            action_taken="Skipped HR ingest",
            confidence=1.0,
        )
        return []

    created: list[Employee] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            emp_id = (raw.get("employee_id") or "").strip()
            if not emp_id:
                continue
            existing = (
                session.query(Employee).filter_by(hr_employee_id=emp_id).one_or_none()
            )
            hire = _parse_date(raw.get("hire_date"))
            term = _parse_date(raw.get("termination_date"))
            fields = dict(
                last_name=(raw.get("last_name") or "").strip(),
                first_name=(raw.get("first_name") or "").strip(),
                middle_initial=(raw.get("middle_initial") or "").strip() or None,
                display_name=None,
                job_class=(raw.get("job_class") or "").strip() or None,
                union_member=_yn_bool(raw.get("union_member")),
                local_member_no=(raw.get("local_member_no") or "").strip() or None,
                home_location=(raw.get("home_location") or "").strip() or None,
                department_code=(raw.get("department") or "").strip() or None,
                shift=(raw.get("shift") or "").strip() or None,
                employment_type=(raw.get("employment_type") or "").strip() or None,
                hire_date=hire,
                termination_date=term,
                status=(raw.get("status") or "ACTIVE").strip() or "ACTIVE",
                sick_days_remaining=_dec(raw.get("sick_days_remaining_2026_10_01")),
                clinic_day_used=_yn_bool(raw.get("clinic_day_used_2026")),
                primary_language=(raw.get("primary_language") or "").strip() or None,
                meta={"source": "hr_roster.csv", "raw": dict(raw)},
            )
            fields["display_name"] = (
                f"{fields['first_name']} {fields['last_name']}".strip()
            )
            if existing:
                for k, v in fields.items():
                    setattr(existing, k, v)
                created.append(existing)
            else:
                emp = Employee(hr_employee_id=emp_id, **fields)
                session.add(emp)
                created.append(emp)

    session.flush()
    logger.info("Loaded %s HR employees from %s", len(created), path.name)
    return created


# ---------------------------------------------------------------------------
# Time & attendance
# ---------------------------------------------------------------------------


def load_ta_site_codes(data_dir: Path) -> dict[str, str]:
    """Return SiteCode → SiteName from ta_site_codes.csv."""
    path = data_dir / "time_attendance" / "ta_site_codes.csv"
    mapping: dict[str, str] = {}
    if not path.exists():
        return mapping
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            code = (row.get("SiteCode") or "").strip()
            name = (row.get("SiteName") or "").strip()
            if code and name:
                mapping[code] = name
    return mapping


def _normalize_punch_type(value: str) -> str:
    text = (value or "").strip().upper()
    if text in {"IN", "CLOCK_IN", "CLOCKIN", "CI"}:
        return "IN"
    if text in {"OUT", "CLOCK_OUT", "CLOCKOUT", "CO"}:
        return "OUT"
    return text


def load_time_attendance(session: Session, data_dir: Path) -> list[Punch]:
    ta_dir = data_dir / "time_attendance"
    site_codes = load_ta_site_codes(data_dir)
    punches: list[Punch] = []

    # Layout A (pre-firmware): badge_no, employee_name, site, punch_type, punch_time, device_id
    old_path = ta_dir / "ta_punches_2026-10-01_to_2026-10-18.csv"
    if old_path.exists():
        with old_path.open(newline="", encoding="utf-8-sig") as fh:
            for raw in csv.DictReader(fh):
                punch = Punch(
                    badge_no=(raw.get("badge_no") or "").strip() or None,
                    employee_name=(raw.get("employee_name") or "").strip() or None,
                    site=(raw.get("site") or "").strip() or None,
                    punch_type=_normalize_punch_type(raw.get("punch_type") or ""),
                    punch_time=_parse_local_datetime(raw.get("punch_time") or ""),
                    device_id=(raw.get("device_id") or "").strip() or None,
                    source_file=old_path.name,
                    raw=dict(raw),
                )
                session.add(punch)
                punches.append(punch)

    # Layout B (post-firmware): EmpBadge, Name, SiteCode, Event, Timestamp, Terminal
    new_path = ta_dir / "ta_punches_2026-10-19_to_2026-10-31.csv"
    if new_path.exists():
        with new_path.open(newline="", encoding="utf-8-sig") as fh:
            for raw in csv.DictReader(fh):
                site_code = (raw.get("SiteCode") or "").strip()
                site_name = site_codes.get(site_code, site_code) or None
                badge = (raw.get("EmpBadge") or "").strip()
                # Zero-pad short badges to match older 7-digit layout when possible
                if badge.isdigit() and len(badge) < 7:
                    badge_padded = badge.zfill(7)
                else:
                    badge_padded = badge
                punch = Punch(
                    badge_no=badge_padded or None,
                    employee_name=(raw.get("Name") or "").strip() or None,
                    site=site_name,
                    punch_type=_normalize_punch_type(raw.get("Event") or ""),
                    punch_time=_parse_local_datetime(raw.get("Timestamp") or ""),
                    device_id=(raw.get("Terminal") or "").strip() or None,
                    source_file=new_path.name,
                    raw={**dict(raw), "site_code": site_code},
                )
                session.add(punch)
                punches.append(punch)

    if not old_path.exists() and not new_path.exists():
        add_exception(
            session,
            source="time_attendance",
            record_ref=str(ta_dir),
            issue="No time & attendance punch files found",
            action_taken="Skipped TA ingest",
            confidence=1.0,
        )

    # Persist site-code map for resolve (as meta on a sentinel exception? better: Building later)
    session.flush()
    logger.info(
        "Loaded %s punches (site codes: %s)", len(punches), len(site_codes)
    )
    # Stash site codes on session.info for resolve
    session.info["ta_site_codes"] = site_codes
    return punches


# ---------------------------------------------------------------------------
# Callouts — reception log
# ---------------------------------------------------------------------------


def load_callout_log(session: Session, data_dir: Path) -> list[CalloutEvent]:
    path = data_dir / "callouts" / "callout_log_october_2026.xlsx"
    if not path.exists():
        add_exception(
            session,
            source="reception_log",
            record_ref=str(path),
            issue="Callout log workbook missing",
            action_taken="Skipped reception callout ingest",
            confidence=1.0,
        )
        return []

    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []

    headers = [str(h).strip() if h is not None else "" for h in rows[0]]
    events: list[CalloutEvent] = []

    for idx, values in enumerate(rows[1:], start=2):
        if not any(v is not None and str(v).strip() for v in values):
            continue
        raw = {
            headers[i]: values[i] if i < len(values) else None
            for i in range(len(headers))
        }
        event_date = _parse_date(raw.get("Date"))
        time_text = str(raw.get("Time") or "").strip()
        noticed_at = None
        if event_date and time_text:
            noticed_at = _parse_local_datetime(
                f"{event_date.strftime('%m/%d/%Y')} {time_text}"
            )
        vm = raw.get("VM Received")
        message = str(raw.get("Message") or "").strip()
        caller = str(raw.get("Caller") or "").strip()
        source_ref = f"row-{idx}"

        event = CalloutEvent(
            source="reception_log",
            source_ref=source_ref,
            raw_text=message or None,
            employee_name_raw=caller or None,
            event_date=event_date,
            noticed_at=noticed_at,
            reason_text=message or None,
            hours=Decimal("8"),
            is_absence=None,  # classified later in callouts module
            confidence=None,
            status="ingested",
            structured={
                "taken_by": raw.get("Taken By"),
                "channel": raw.get("Channel"),
                "vm_received": str(vm) if vm is not None else None,
                "sent_to_ops": raw.get("Sent to Ops"),
            },
            meta={"excel_row": idx, "raw": {k: _jsonable(v) for k, v in raw.items()}},
        )
        session.add(event)
        events.append(event)

    session.flush()
    logger.info("Loaded %s reception callout rows", len(events))
    return events


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


# ---------------------------------------------------------------------------
# Callouts — night ops WhatsApp-style export
# ---------------------------------------------------------------------------


def load_night_ops_chat(session: Session, data_dir: Path) -> list[CalloutEvent]:
    path = data_dir / "callouts" / "night_ops_group_chat_export.txt"
    if not path.exists():
        add_exception(
            session,
            source="area_manager_chat",
            record_ref=str(path),
            issue="Night ops chat export missing",
            action_taken="Skipped chat ingest",
            confidence=1.0,
        )
        return []

    events: list[CalloutEvent] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for line_no, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        m = CHAT_LINE_RE.match(line)
        if not m:
            continue
        date_part, time_part, sender, message = m.groups()
        if CHAT_SYSTEM_RE.search(message) or CHAT_SYSTEM_RE.search(sender):
            # e.g. "Paul Brennan created group ..."
            if ":" not in line.split("]")[-1] or CHAT_SYSTEM_RE.search(message):
                if not message or CHAT_SYSTEM_RE.search(line):
                    continue
        # Skip pure system notifications without a conversational payload pattern
        if re.search(r"created group|end-to-end encrypted", line, re.I):
            continue

        # Normalize 2-digit year
        dt = _parse_local_datetime(f"{date_part} {time_part}")
        event_date = dt.date() if dt else _parse_date(date_part)
        # Chat source_ref is the message timestamp string per README
        source_ref = f"{date_part}, {time_part}"

        event = CalloutEvent(
            source="area_manager_chat",
            source_ref=source_ref,
            raw_text=message,
            employee_name_raw=None,  # extracted later from message text
            event_date=event_date,
            noticed_at=dt,
            reason_text=message,
            hours=Decimal("8"),
            is_absence=None,
            status="ingested",
            structured={"sender": sender.strip(), "line_no": line_no},
            meta={"line": line},
        )
        session.add(event)
        events.append(event)

    session.flush()
    logger.info("Loaded %s night-ops chat lines", len(events))
    return events


# ---------------------------------------------------------------------------
# Labor budget (secondary / cross-check source)
# ---------------------------------------------------------------------------


def load_labor_budget(session: Session, data_dir: Path) -> list[dict[str, Any]]:
    path = data_dir / "labor_budget" / "labor_budget_FY26.xlsx"
    if not path.exists():
        add_exception(
            session,
            source="labor_budget",
            record_ref=str(path),
            issue="Labor budget workbook missing",
            action_taken="Continued without secondary headcount cross-check",
            confidence=0.9,
        )
        return []

    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))

    # Find header row containing Building / Employee
    header_idx = None
    for i, row in enumerate(rows):
        cells = [str(c).strip().lower() if c is not None else "" for c in row]
        if "building" in cells and "employee" in cells:
            header_idx = i
            break
    if header_idx is None:
        add_exception(
            session,
            source="labor_budget",
            record_ref=path.name,
            issue="Could not locate header row in labor budget",
            action_taken="Skipped labor budget parse",
            confidence=0.8,
        )
        return []

    headers = [str(c).strip() if c is not None else "" for c in rows[header_idx]]
    records: list[dict[str, Any]] = []
    for values in rows[header_idx + 1 :]:
        raw = {
            headers[i]: values[i] if i < len(values) else None
            for i in range(len(headers))
        }
        building = str(raw.get("Building") or "").strip()
        employee = str(raw.get("Employee") or "").strip()
        if not building or building.lower().endswith("subtotal"):
            continue
        if not employee:
            continue
        records.append(
            {
                "building": building,
                "position": str(raw.get("Position") or "").strip() or None,
                "employee": employee,
                "shift": str(raw.get("Shift") or "").strip() or None,
                "status": str(raw.get("Status") or "").strip() or None,
                "weekly_hours": _dec(raw.get("Wkly Hrs")),
                "rate": _dec(raw.get("Rate")),
            }
        )

    session.info["labor_budget_rows"] = records
    logger.info("Loaded %s labor budget rows", len(records))
    return records


# ---------------------------------------------------------------------------
# Contracts (PDF text → structured client / building terms)
# ---------------------------------------------------------------------------


def _extract_pdf_text(path: Path) -> str:
    reader = PdfReader(str(path))
    parts: list[str] = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    return "\n".join(parts)


def _client_name_from_msa(text: str, filename: str) -> str:
    m = re.search(
        r'by and between\s+(.+?)\s+\("Client"\)',
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    # Fallback from filename: MSA_Meridian_Realty_Partners.pdf
    stem = Path(filename).stem
    stem = re.sub(r"^MSA_", "", stem)
    stem = re.sub(r"_Amendment.*$", "", stem, flags=re.I)
    return stem.replace("_", " ").strip()


def _detect_backup_template(text: str) -> Optional[str]:
    """Template A requires replacement name; Template B forbids personnel names."""
    lower = text.lower()
    if "shall not include" in lower and "name" in lower:
        return "B"
    if "does not require" in lower and "name" in lower:
        return "B"
    if "full name of the replacement" in lower or "replacement worker" in lower:
        return "A"
    if "actual time on site" in lower:
        return "A"
    return None


def _parse_schedule_b_buildings(text: str) -> list[dict[str, Any]]:
    """
    Extract building rows from Schedule B.

    PDFs often wrap cells; we look for known-style lines that start with a
    building-like name followed by two integers (day/night posts).
    """
    # Isolate Schedule B heading (not earlier prose that merely mentions Schedule B).
    # Stop at a real EXHIBIT/SCHEDULE heading line, not mid-sentence "subject to Exhibit C".
    sched = text
    m = re.search(
        r"SCHEDULE B\s*:\s*BUILDINGS.*?"
        r"(?=\n\s*EXHIBIT\s+[A-Z]\s*:|\n\s*SCHEDULE\s+[A-Z]\s*:|\Z)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        m = re.search(
            r"SCHEDULE B\s*:.*?"
            r"(?=\n\s*EXHIBIT\s+[A-Z]\s*:|\n\s*SCHEDULE\s+[A-Z]\s*:|\Z)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
    if m:
        sched = m.group(0)

    buildings: list[dict[str, Any]] = []
    # Match: BuildingName <day> <night> then pricing / coverage prose nearby
    pattern = re.compile(
        r"(?P<name>[A-Z][A-Za-z0-9][A-Za-z0-9 .'/-]{1,60}?)\s+"
        r"(?P<day>\d{1,2})\s+(?P<night>\d{1,2})\s+"
        r"(?P<pricing>Fixed monthly fee of\s*\$?[\d,]+\.?\d*|Cost plus\s+\d+\s*%[^\n]*)",
        re.IGNORECASE,
    )
    for m in pattern.finditer(sched):
        name = re.sub(r"\s+", " ", m.group("name")).strip()
        # Skip header leftovers
        if name.lower() in {"building", "day posts", "night posts", "pricing basis"}:
            continue
        if "post" in name.lower() and len(name) < 20:
            continue
        pricing = re.sub(r"\s+", " ", m.group("pricing")).strip()
        billing_type = "fixed" if pricing.lower().startswith("fixed") else "cost_plus"
        markup = None
        mm = re.search(r"cost plus\s+(\d+)\s*%", pricing, re.I)
        if mm:
            markup = Decimal(mm.group(1)) / Decimal(100)
        # Coverage billable?
        # Look ahead ~400 chars for "Not separately billable" vs "Billable"
        start = m.end()
        window = sched[start : start + 400]
        coverage_billable = True
        if re.search(r"not separately billable|included in the fixed fee", window, re.I):
            coverage_billable = False
        elif re.search(r"billable at cost", window, re.I):
            coverage_billable = True

        buildings.append(
            {
                "name": name,
                "day_posts": int(m.group("day")),
                "night_posts": int(m.group("night")),
                "pricing_basis": pricing,
                "billing_type": billing_type,
                "cost_plus_markup": markup,
                "coverage_billable": coverage_billable,
            }
        )
    return buildings


def _parse_amendments(text: str) -> list[dict[str, Any]]:
    """Extract night/day post amendments with effective dates."""
    amendments: list[dict[str, Any]] = []
    # Effective September 1, 2026, the number of night posts at Corlett Building ... from 7 to 8
    for m in re.finditer(
        r"[Ee]ffective\s+(?P<eff>[A-Za-z]+\s+\d{1,2},\s*\d{4}),?\s+"
        r"the number of night posts at\s+(?P<bldg>[A-Za-z0-9 .'/-]+?)\s+"
        r"(?:stated in Schedule B\s+)?is increased\s+from\s+(?P<from>\d+)\s+to\s+(?P<to>\d+)",
        text,
        re.IGNORECASE | re.DOTALL,
    ):
        eff = datetime.strptime(
            re.sub(r"\s+", " ", m.group("eff")).strip(), "%B %d, %Y"
        ).date()
        amendments.append(
            {
                "building": re.sub(r"\s+", " ", m.group("bldg")).strip(),
                "field": "night_posts",
                "from_value": int(m.group("from")),
                "to_value": int(m.group("to")),
                "effective_date": eff.isoformat(),
            }
        )
    for m in re.finditer(
        r"[Dd]ay posts is unchanged at (?P<day>\d+)",
        text,
    ):
        if amendments:
            amendments[-1]["day_posts_unchanged"] = int(m.group("day"))
    return amendments


def _parse_wage_schedule(text: str) -> dict[str, Any]:
    """Parse CBA 2026 union rates, non-union rate card, and burden percents."""
    union_rates: dict[str, Decimal] = {}
    nonunion_rates: dict[str, Decimal] = {}

    # Classification followed by three dollar amounts (2025, 2026, 2027)
    for cls in ("Cleaner", "Porter", "Handyperson", "Supervisor"):
        m = re.search(
            rf"{cls}\s*\$?\s*([\d.]+)\s*\$?\s*([\d.]+)\s*\$?\s*([\d.]+)",
            text,
            re.IGNORECASE,
        )
        if m:
            union_rates[cls] = Decimal(m.group(2))

    # Non-union rate card section: Classification then single 2026 rate
    rate_card = text
    idx = text.lower().find("non-union")
    if idx >= 0:
        rate_card = text[idx:]
    for cls in ("Cleaner", "Porter", "Handyperson", "Supervisor"):
        m = re.search(
            rf"{cls}\s*\$?\s*([\d.]+)",
            rate_card,
            re.IGNORECASE,
        )
        if m:
            nonunion_rates[cls] = Decimal(m.group(1))

    burden_union = Decimal("0.32")
    burden_nonunion = Decimal("0.18")
    m = re.search(
        r"(\d+)\s*%\s*for bargaining unit.*?(\d+)\s*%\s*for non-union",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        burden_union = Decimal(m.group(1)) / Decimal(100)
        burden_nonunion = Decimal(m.group(2)) / Decimal(100)

    return {
        "union_rates_2026": {k: str(v) for k, v in union_rates.items()},
        "nonunion_rates_2026": {k: str(v) for k, v in nonunion_rates.items()},
        "burden_union": str(burden_union),
        "burden_nonunion": str(burden_nonunion),
    }


def load_contracts(session: Session, data_dir: Path) -> dict[str, Any]:
    contracts_dir = data_dir / "contracts"
    result: dict[str, Any] = {
        "clients": [],
        "buildings": [],
        "amendments": [],
        "wages": {},
    }
    if not contracts_dir.exists():
        add_exception(
            session,
            source="contracts",
            record_ref=str(contracts_dir),
            issue="Contracts directory missing",
            action_taken="Cannot derive canonical building names",
            confidence=1.0,
        )
        return result

    for path in sorted(contracts_dir.glob("*.pdf")):
        text = _extract_pdf_text(path)
        lower_name = path.name.lower()

        if "wage" in lower_name or "cba" in lower_name:
            result["wages"] = _parse_wage_schedule(text)
            session.info["wage_schedule"] = result["wages"]
            continue

        if "amendment" in lower_name:
            amends = _parse_amendments(text)
            for a in amends:
                a["source_file"] = path.name
            result["amendments"].extend(amends)
            continue

        client_name = _client_name_from_msa(text, path.name)
        template = _detect_backup_template(text)
        buildings = _parse_schedule_b_buildings(text)
        # Infer billing from first building when client-level
        billing_type = buildings[0]["billing_type"] if buildings else None
        markup = buildings[0]["cost_plus_markup"] if buildings else None

        client = (
            session.query(Client).filter_by(name=client_name).one_or_none()
        )
        code = re.sub(r"[^A-Za-z0-9]+", "_", client_name).strip("_").upper()[:64]
        if client is None:
            client = Client(
                code=code,
                name=client_name,
                billing_type=billing_type,
                cost_plus_markup=markup,
                backup_note_template=template,
                contract_path=str(path),
                meta={"source_file": path.name},
            )
            session.add(client)
            session.flush()
        else:
            client.billing_type = billing_type or client.billing_type
            client.cost_plus_markup = markup if markup is not None else client.cost_plus_markup
            client.backup_note_template = template or client.backup_note_template
            client.contract_path = str(path)

        result["clients"].append({"id": client.id, "name": client.name, "code": client.code})

        for b in buildings:
            bldg = session.query(Building).filter_by(name=b["name"]).one_or_none()
            if bldg is None:
                bldg = Building(
                    name=b["name"],
                    client_id=client.id,
                    contracted_day_posts=b["day_posts"],
                    contracted_night_posts=b["night_posts"],
                    active=True,
                    meta={
                        "pricing_basis": b["pricing_basis"],
                        "billing_type": b["billing_type"],
                        "cost_plus_markup": str(b["cost_plus_markup"])
                        if b["cost_plus_markup"] is not None
                        else None,
                        "coverage_billable": b["coverage_billable"],
                        "source_file": path.name,
                        "schedule_b_name": b["name"],
                    },
                )
                session.add(bldg)
            else:
                bldg.client_id = client.id
                bldg.contracted_day_posts = b["day_posts"]
                bldg.contracted_night_posts = b["night_posts"]
                meta = dict(bldg.meta or {})
                meta.update(
                    {
                        "pricing_basis": b["pricing_basis"],
                        "billing_type": b["billing_type"],
                        "cost_plus_markup": str(b["cost_plus_markup"])
                        if b["cost_plus_markup"] is not None
                        else None,
                        "coverage_billable": b["coverage_billable"],
                        "source_file": path.name,
                        "schedule_b_name": b["name"],
                    }
                )
                bldg.meta = meta
            result["buildings"].append(b)

        # Propagate template onto buildings
        if template:
            session.flush()
            for bldg in session.query(Building).filter_by(client_id=client.id):
                meta = dict(bldg.meta or {})
                meta["backup_note_template"] = template
                bldg.meta = meta

    # Apply amendments as of period end (Oct 31)
    period_end = date(2026, 10, 31)
    for a in result["amendments"]:
        eff = _parse_date(a["effective_date"])
        if eff is None or eff > period_end:
            continue
        bldg = _find_building_fuzzy(session, a["building"])
        if bldg is None:
            add_exception(
                session,
                source="contracts_amendment",
                record_ref=a.get("source_file", "amendment"),
                issue=f"Amendment building not matched: {a['building']}",
                action_taken="Left contracted posts unchanged",
                confidence=0.5,
                meta=a,
            )
            continue
        if a.get("field") == "night_posts":
            bldg.contracted_night_posts = a["to_value"]
            meta = dict(bldg.meta or {})
            meta["amendments_applied"] = meta.get("amendments_applied", []) + [a]
            bldg.meta = meta
            logger.info(
                "Applied amendment: %s night posts → %s (eff %s)",
                bldg.name,
                a["to_value"],
                a["effective_date"],
            )

    session.flush()
    session.info["contract_parse"] = result
    logger.info(
        "Contracts: %s clients, %s buildings, %s amendments",
        len(result["clients"]),
        len(session.query(Building).all()),
        len(result["amendments"]),
    )
    return result


def _find_building_fuzzy(session: Session, name: str) -> Optional[Building]:
    from app.resolve.entities import fuzzy_best_match, normalize_name

    target = normalize_name(name)
    buildings = session.query(Building).all()
    if not buildings:
        return None
    choices = {b.name: b for b in buildings}
    match, score = fuzzy_best_match(target, list(choices.keys()))
    if match and score >= 0.72:
        return choices[match]
    return None


# ---------------------------------------------------------------------------
# Personnel Action Forms (scanned PDFs)
# ---------------------------------------------------------------------------


PAF_OCR_PROMPT = """
You are reading a scanned Personnel Action Form (PAF) for a building services company.
Extract JSON with these keys:
- employee_name (string)
- hr_employee_id (string or null)
- action_type (e.g. TRANSFER, RATE_CHANGE, STATUS_CHANGE, OTHER)
- from_location (string or null) — previous home building/location
- to_location (string or null) — new home building/location
- effective_date (YYYY-MM-DD or null)
- approvals: object with keys employee, supervisor, hr (each true/false whether a signature/approval mark is present)
- all_three_approvals (boolean) — true only if employee, supervisor, AND hr approvals are all present
- notes (string)

Return ONLY valid JSON, no markdown.
""".strip()


def _ocr_paf_with_llm(path: Path, settings: Settings) -> dict[str, Any]:
    from app.llm import vision_pdf_or_image_json

    return vision_pdf_or_image_json(
        path.read_bytes(),
        PAF_OCR_PROMPT,
        media_type="application/pdf",
        settings=settings,
        max_tokens=1200,
    )


def load_personnel_action_forms(session: Session, data_dir: Path, settings: Settings) -> list[dict[str, Any]]:
    paf_dir = data_dir / "hr" / "personnel_action_forms"
    results: list[dict[str, Any]] = []
    if not paf_dir.exists():
        return results

    from app.llm import resolve_provider

    provider, _api_key = resolve_provider(settings)
    period_end = date(2026, 10, 31)

    for path in sorted(paf_dir.glob("*.pdf")):
        ref = path.name
        if not provider:
            add_exception(
                session,
                source="paf",
                record_ref=ref,
                issue="PAF scanned PDF requires OCR but OPENAI_API_KEY / ANTHROPIC_API_KEY is not set",
                action_taken="Flagged for manual review; roster not updated from this form",
                confidence=0.0,
            )
            results.append({"file": ref, "status": "flagged_no_api_key"})
            continue

        try:
            extracted = _ocr_paf_with_llm(path, settings)
        except Exception as exc:  # noqa: BLE001
            add_exception(
                session,
                source="paf",
                record_ref=ref,
                issue=f"PAF OCR failed: {exc}",
                action_taken="Flagged for manual review; roster not updated",
                confidence=0.2,
            )
            results.append({"file": ref, "status": "ocr_failed", "error": str(exc)})
            continue

        approvals = extracted.get("approvals") or {}
        all_three = bool(extracted.get("all_three_approvals"))
        if not all_three:
            # Also compute from individual flags if model omitted the rollup
            flags = [
                bool(approvals.get("employee")),
                bool(approvals.get("supervisor")),
                bool(approvals.get("hr")),
            ]
            all_three = all(flags)

        extracted["all_three_approvals"] = all_three
        results.append({"file": ref, "status": "ocr_ok", "data": extracted})

        if not all_three:
            add_exception(
                session,
                source="paf",
                record_ref=ref,
                issue="PAF missing one or more of the three required approvals; form has no effect per KPI defs",
                action_taken="Ignored PAF for home-building updates",
                confidence=0.85,
                meta=extracted,
            )
            continue

        # Apply transfer / location change when fully approved
        eff = _parse_date(extracted.get("effective_date"))
        to_loc = (extracted.get("to_location") or "").strip()
        emp = _match_employee_for_paf(session, extracted)
        if emp is None:
            add_exception(
                session,
                source="paf",
                record_ref=ref,
                issue=f"Could not resolve employee for approved PAF: {extracted.get('employee_name')}",
                action_taken="Held PAF; no roster update",
                confidence=0.4,
                meta=extracted,
            )
            continue

        if to_loc and (eff is None or eff <= period_end):
            # Store intended location; building_id linked in resolve
            meta = dict(emp.meta or {})
            meta["paf_applied"] = {
                "file": ref,
                "to_location": to_loc,
                "from_location": extracted.get("from_location"),
                "effective_date": extracted.get("effective_date"),
                "action_type": extracted.get("action_type"),
            }
            emp.meta = meta
            # If to_location looks like an HR code (BLD-xx / FLOAT), update home_location
            if re.match(r"^(BLD-\d+|FLOAT)$", to_loc, re.I):
                emp.home_location = to_loc.upper()
            else:
                # Free-text building name — stash for resolve to map
                meta["paf_to_building_name"] = to_loc
                emp.meta = meta
            logger.info("Applied approved PAF %s to %s → %s", ref, emp.hr_employee_id, to_loc)
        else:
            add_exception(
                session,
                source="paf",
                record_ref=ref,
                issue="Approved PAF has no applicable location change for period end",
                action_taken="Recorded OCR only",
                confidence=0.7,
                meta=extracted,
            )

    session.flush()
    session.info["paf_results"] = results
    return results


def _match_employee_for_paf(session: Session, extracted: dict[str, Any]) -> Optional[Employee]:
    hr_id = (extracted.get("hr_employee_id") or "").strip()
    if hr_id:
        emp = session.query(Employee).filter_by(hr_employee_id=hr_id).one_or_none()
        if emp:
            return emp
    name = (extracted.get("employee_name") or "").strip()
    if not name:
        return None
    from app.resolve.entities import fuzzy_best_match, normalize_name, employee_match_keys

    emps = session.query(Employee).all()
    keys = {employee_match_keys(e)[0]: e for e in emps}
    match, score = fuzzy_best_match(normalize_name(name), list(keys.keys()))
    if match and score >= 0.85:
        return keys[match]
    return None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def ingest_files(session: Session, settings: Settings | None = None) -> dict[str, Any]:
    """Load every file drop under DATA_DIR into the session."""
    settings = settings or get_settings()
    data_dir = Path(settings.data_dir)

    summary: dict[str, Any] = {"data_dir": str(data_dir)}
    summary["contracts"] = load_contracts(session, data_dir)
    summary["hr_count"] = len(load_hr_roster(session, data_dir))
    summary["paf"] = load_personnel_action_forms(session, data_dir, settings)
    summary["punch_count"] = len(load_time_attendance(session, data_dir))
    summary["callout_log_count"] = len(load_callout_log(session, data_dir))
    summary["chat_count"] = len(load_night_ops_chat(session, data_dir))
    summary["labor_budget_count"] = len(load_labor_budget(session, data_dir))
    session.commit()
    return summary
