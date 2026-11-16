"""Resolve employees and buildings across HR, payroll, dispatch, TA, finance."""

from __future__ import annotations

import logging
import re
import unicodedata
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.ingest.files import add_exception
from app.models import (
    Building,
    Department,
    DispatchPosting,
    Employee,
    FinanceLaborCost,
    Punch,
)

logger = logging.getLogger(__name__)


def normalize_name(value: str) -> str:
    """Lowercase, strip accents/punct, collapse whitespace for fuzzy matching."""
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = text.replace("&", " and ")
    # Digits: keep (Meridian 1)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Common building abbreviations expanded for matching
    replacements = {
        " twr": " tower",
        " plz": " plaza",
        " hse": " house",
        " bldg": " building",
        " anx": " annex",
        " annx": " annex",
    }
    padded = f" {text} "
    for old, new in replacements.items():
        padded = padded.replace(old, new)
    text = re.sub(r"\s+", " ", padded).strip()
    # Meridian 1 ↔ Meridian One
    text = re.sub(r"\b1\b", "one", text)
    text = re.sub(r"\b2\b", "two", text)
    return text


def fuzzy_score(a: str, b: str) -> float:
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    # Substring bonus for aliases like "BRAMWELL ANX" vs "Bramwell Annex"
    if na in nb or nb in na:
        return max(0.9, SequenceMatcher(None, na, nb).ratio())
    return SequenceMatcher(None, na, nb).ratio()


def fuzzy_best_match(
    target: str, choices: list[str], *, min_score: float = 0.0
) -> tuple[Optional[str], float]:
    best: Optional[str] = None
    best_score = 0.0
    for choice in choices:
        score = fuzzy_score(target, choice)
        if score > best_score:
            best_score = score
            best = choice
    if best is None or best_score < min_score:
        return None, best_score
    return best, best_score


def employee_match_keys(emp: Employee) -> list[str]:
    """Primary and alternate name keys for an HR employee."""
    last = (emp.last_name or "").strip()
    first = (emp.first_name or "").strip()
    mi = (emp.middle_initial or "").strip()
    keys = [
        normalize_name(f"{last} {first}"),
        normalize_name(f"{first} {last}"),
        normalize_name(f"{last}, {first}"),
    ]
    if mi:
        keys.append(normalize_name(f"{last} {first} {mi}"))
        keys.append(normalize_name(f"{first} {mi} {last}"))
    return keys


def _payroll_name_keys(name: str) -> list[str]:
    text = (name or "").strip()
    if "," in text:
        last, rest = text.split(",", 1)
        return [
            normalize_name(f"{last} {rest}"),
            normalize_name(f"{rest} {last}"),
            normalize_name(text),
        ]
    parts = text.split()
    if len(parts) >= 2:
        return [
            normalize_name(text),
            normalize_name(f"{parts[-1]} {parts[0]}"),
            normalize_name(f"{parts[0]} {parts[-1]}"),
        ]
    return [normalize_name(text)]


# ---------------------------------------------------------------------------
# Buildings
# ---------------------------------------------------------------------------


def resolve_buildings(session: Session) -> dict[str, Any]:
    """
    Link canonical Schedule B buildings to HR locations, TA sites, dispatch,
    and finance sites via fuzzy name matching + labor-budget name bridges.

    Contract Schedule B names remain the canonical `Building.name`.
    """
    buildings = session.query(Building).all()
    if not buildings:
        add_exception(
            session,
            source="resolve_buildings",
            record_ref="buildings",
            issue="No buildings loaded from contracts",
            action_taken="Cannot resolve site aliases",
            confidence=0.2,
        )
        return {"buildings": 0}

    canonical = {b.name: b for b in buildings}
    canon_names = list(canonical.keys())

    # --- Labor budget: employee name → building; join HR for home_location ---
    location_votes: dict[str, dict[str, int]] = {}
    labor_rows = session.info.get("labor_budget_rows") or []
    hr_by_key: dict[str, Employee] = {}
    for emp in session.query(Employee).all():
        for k in employee_match_keys(emp):
            hr_by_key.setdefault(k, emp)

    for row in labor_rows:
        bname = row.get("building") or ""
        match, score = fuzzy_best_match(bname, canon_names, min_score=0.72)
        if not match:
            continue
        emp_name = row.get("employee") or ""
        emp_match, emp_score = fuzzy_best_match(
            normalize_name(emp_name), list(hr_by_key.keys()), min_score=0.85
        )
        if not emp_match:
            continue
        emp = hr_by_key[emp_match]
        loc = (emp.home_location or "").strip()
        if not loc or loc.upper() == "FLOAT":
            continue
        location_votes.setdefault(loc, {})
        location_votes[loc][match] = location_votes[loc].get(match, 0) + 1

    # Assign HR location code from majority vote
    for loc, votes in location_votes.items():
        winner = max(votes.items(), key=lambda kv: kv[1])[0]
        bldg = canonical[winner]
        # Avoid collisions: if another building already claimed this code, flag
        conflict = (
            session.query(Building)
            .filter(Building.hr_location_code == loc, Building.id != bldg.id)
            .one_or_none()
        )
        if conflict:
            add_exception(
                session,
                source="resolve_buildings",
                record_ref=loc,
                issue=f"HR location {loc} contested between {bldg.name} and {conflict.name}",
                action_taken=f"Assigned to {bldg.name} by labor-budget majority vote",
                confidence=0.6,
                meta={"votes": votes},
            )
        bldg.hr_location_code = loc

    # Department code from HR employees at that location (majority)
    for bldg in buildings:
        if not bldg.hr_location_code:
            continue
        dept_votes: dict[str, int] = {}
        for emp in (
            session.query(Employee)
            .filter(Employee.home_location == bldg.hr_location_code)
            .all()
        ):
            if emp.department_code:
                dept_votes[emp.department_code] = (
                    dept_votes.get(emp.department_code, 0) + 1
                )
        if dept_votes:
            bldg.department_code = max(dept_votes.items(), key=lambda kv: kv[1])[0]

    # --- TA site codes / names ---
    ta_codes: dict[str, str] = session.info.get("ta_site_codes") or {}
    # Also collect distinct punch.site values
    punch_sites = {
        p.site for p in session.query(Punch.site).distinct() if p.site
    }
    alias_map: dict[str, str] = {}  # alias → canonical name

    def link_alias(alias: str, *, min_score: float = 0.72) -> Optional[Building]:
        match, score = fuzzy_best_match(alias, canon_names, min_score=min_score)
        if not match:
            return None
        alias_map[alias] = match
        return canonical[match]

    for code, site_name in ta_codes.items():
        bldg = link_alias(site_name)
        if bldg is None:
            # Inactive / HQ training rooms etc.
            if "train" in site_name.lower() or "hq" in site_name.lower():
                continue
            add_exception(
                session,
                source="resolve_buildings",
                record_ref=code,
                issue=f"TA site '{site_name}' ({code}) could not be matched to a contract building",
                action_taken="Left unmatched; punches at this site may lack building_id",
                confidence=0.5,
            )
            continue
        meta = dict(bldg.meta or {})
        aliases = list(meta.get("ta_aliases") or [])
        for a in (site_name, code):
            if a not in aliases:
                aliases.append(a)
        meta["ta_aliases"] = aliases
        # Prefer primary active code (not duplicate ANX vs ANNEX) — keep first
        bldg.meta = meta

    for site in punch_sites:
        link_alias(site)

    # Explicit Bramwell Annex dual codes: both BRAMWELL ANNEX and BRAMWELL ANX
    annex = None
    for name in canon_names:
        if "annex" in normalize_name(name):
            annex = canonical[name]
            break
    if annex:
        meta = dict(annex.meta or {})
        aliases = list(meta.get("ta_aliases") or [])
        for a in ("BRAMWELL ANNEX", "BRAMWELL ANX", "S-106", "S-116"):
            if a not in aliases:
                aliases.append(a)
        meta["ta_aliases"] = aliases
        annex.meta = meta
        alias_map["BRAMWELL ANX"] = annex.name
        alias_map["BRAMWELL ANNEX"] = annex.name

    # --- Dispatch sites ---
    for site in session.info.get("dispatch_sites") or []:
        site_id = str(site.get("site_id") or "").strip()
        name = str(site.get("name") or "").strip()
        if not name:
            continue
        bldg = link_alias(name, min_score=0.70)
        if bldg is None:
            add_exception(
                session,
                source="resolve_buildings",
                record_ref=site_id or name,
                issue=f"Dispatch site '{name}' unmatched to contract building",
                action_taken="Postings at this site will lack building_id until mapped",
                confidence=0.5,
            )
            continue
        bldg.dispatch_site_id = site_id or bldg.dispatch_site_id
        meta = dict(bldg.meta or {})
        aliases = list(meta.get("dispatch_aliases") or [])
        if name not in aliases:
            aliases.append(name)
        meta["dispatch_aliases"] = aliases
        bldg.meta = meta

    # --- Finance sites ---
    for site in session.info.get("finance_sites") or []:
        name = str(site.get("site_name") or site.get("name") or "").strip()
        dept = str(
            site.get("department_code")
            or site.get("department")
            or site.get("ledger_department")
            or ""
        ).strip()
        if not name:
            continue
        bldg = link_alias(name, min_score=0.70)
        if bldg is None:
            add_exception(
                session,
                source="resolve_buildings",
                record_ref=name,
                issue=f"Finance site '{name}' unmatched to contract building",
                action_taken="GL/invoice lines may need manual building link",
                confidence=0.5,
            )
            continue
        bldg.finance_site_name = name
        if dept:
            bldg.department_code = bldg.department_code or dept
            dept_row = session.query(Department).filter_by(code=dept).one_or_none()
            if dept_row:
                dept_row.building_id = bldg.id
        meta = dict(bldg.meta or {})
        aliases = list(meta.get("finance_aliases") or [])
        if name not in aliases:
            aliases.append(name)
        meta["finance_aliases"] = aliases
        bldg.meta = meta

    session.info["building_alias_map"] = alias_map

    # Link punches → buildings
    for punch in session.query(Punch).filter(Punch.building_id.is_(None)).all():
        if not punch.site:
            continue
        match, score = fuzzy_best_match(punch.site, canon_names, min_score=0.72)
        if match:
            punch.building_id = canonical[match].id
        elif punch.site in alias_map:
            punch.building_id = canonical[alias_map[punch.site]].id

    # Link dispatch postings
    from app.models import DispatchPosting

    site_id_to_building = {
        b.dispatch_site_id: b for b in buildings if b.dispatch_site_id
    }
    for posting in session.query(DispatchPosting).all():
        if posting.site_id and posting.site_id in site_id_to_building:
            posting.building_id = site_id_to_building[posting.site_id].id

    # Link finance labor costs by department
    dept_to_building = {
        b.department_code: b.id for b in buildings if b.department_code
    }
    for row in session.query(FinanceLaborCost).filter(
        FinanceLaborCost.building_id.is_(None)
    ):
        if row.department_code and row.department_code in dept_to_building:
            row.building_id = dept_to_building[row.department_code]

    # Link invoice lines by site_name fuzzy match
    from app.models import FinanceInvoiceLine

    for line in session.query(FinanceInvoiceLine).filter(
        FinanceInvoiceLine.building_id.is_(None)
    ):
        if not line.site_name:
            continue
        match, score = fuzzy_best_match(line.site_name, canon_names, min_score=0.72)
        if match:
            line.building_id = canonical[match].id

    # Flag buildings still missing HR location
    for bldg in buildings:
        if not bldg.hr_location_code:
            add_exception(
                session,
                source="resolve_buildings",
                record_ref=bldg.name,
                issue="Could not determine HR home_location code for building",
                action_taken="Active headcount may under-count until mapped",
                confidence=0.55,
            )

    session.flush()
    return {
        "buildings": len(buildings),
        "aliases": len(alias_map),
        "hr_location_mapped": sum(1 for b in buildings if b.hr_location_code),
    }


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------


def resolve_employees(session: Session) -> dict[str, Any]:
    """Match HR employees to payroll IDs and dispatch worker IDs; set building_id."""
    employees = session.query(Employee).all()
    buildings = session.query(Building).all()
    loc_to_building = {
        b.hr_location_code: b for b in buildings if b.hr_location_code
    }
    canon_names = [b.name for b in buildings]
    name_to_building = {b.name: b for b in buildings}

    # Payroll match by numeric id (HR E10001 ↔ payroll 10001) then by name
    payroll_emps: list[dict[str, Any]] = session.info.get("payroll_employees") or []
    payroll_by_key: dict[str, dict[str, Any]] = {}
    payroll_by_id: dict[int, dict[str, Any]] = {}
    for pe in payroll_emps:
        name = str(pe.get("name") or "")
        for k in _payroll_name_keys(name):
            payroll_by_key.setdefault(k, pe)
        try:
            payroll_by_id[int(pe.get("employee_id"))] = pe
        except (TypeError, ValueError):
            pass

    # Dispatch workers by hr_employee_no and by name
    dispatch_workers: list[dict[str, Any]] = session.info.get("dispatch_workers") or []
    dispatch_by_hr: dict[str, dict[str, Any]] = {}
    dispatch_by_name: dict[str, dict[str, Any]] = {}
    for w in dispatch_workers:
        hr_no = str(w.get("hr_employee_no") or "").strip()
        if hr_no:
            dispatch_by_hr[hr_no] = w
            dispatch_by_hr[hr_no.upper()] = w
        dname = str(w.get("display_name") or "")
        for k in _payroll_name_keys(dname):
            dispatch_by_name.setdefault(k, w)

    matched_payroll = 0
    matched_dispatch = 0
    unmatched_payroll_keys = set(payroll_by_key.keys())
    payroll_available = bool(payroll_emps)

    for emp in employees:
        # Building from home_location
        loc = (emp.home_location or "").strip()
        if loc and loc.upper() != "FLOAT" and loc in loc_to_building:
            emp.building_id = loc_to_building[loc].id
        # PAF free-text building override
        meta = dict(emp.meta or {})
        paf_bldg = meta.get("paf_to_building_name")
        if paf_bldg:
            match, score = fuzzy_best_match(str(paf_bldg), canon_names, min_score=0.72)
            if match:
                emp.building_id = name_to_building[match].id
                # Also set home_location if that building has a code
                code = name_to_building[match].hr_location_code
                if code:
                    emp.home_location = code

        # Payroll — prefer deterministic HR id digit match, then name keys
        keys = employee_match_keys(emp)
        pe = None
        best = 0.0
        hr_digits = re.sub(r"\D", "", emp.hr_employee_id or "")
        if hr_digits:
            try:
                pe = payroll_by_id.get(int(hr_digits))
                if pe is not None:
                    best = 1.0
            except ValueError:
                pe = None
        if pe is None:
            for k in keys:
                if k in payroll_by_key:
                    pe = payroll_by_key[k]
                    best = 1.0
                    break
        if pe is None and keys:
            match, score = fuzzy_best_match(keys[0], list(payroll_by_key.keys()), min_score=0.88)
            if match:
                pe = payroll_by_key[match]
                best = score
        if pe is not None:
            pid = pe.get("employee_id")
            try:
                emp.payroll_employee_id = int(pid)
            except (TypeError, ValueError):
                add_exception(
                    session,
                    source="resolve_employees",
                    record_ref=emp.hr_employee_id,
                    issue=f"Payroll employee_id not int: {pid}",
                    action_taken="Left payroll_employee_id null",
                    confidence=0.4,
                )
            else:
                matched_payroll += 1
                for k in _payroll_name_keys(str(pe.get("name") or "")):
                    unmatched_payroll_keys.discard(k)
            if best < 0.95:
                notes = (emp.resolution_notes or "") + f"; payroll fuzzy={best:.2f}"
                emp.resolution_notes = notes.strip("; ")
        elif payroll_available:
            add_exception(
                session,
                source="resolve_employees",
                record_ref=emp.hr_employee_id,
                issue=f"No payroll match for {emp.first_name} {emp.last_name}",
                action_taken="payroll_employee_id left null; absences held if needed",
                confidence=0.45,
            )

        # Dispatch
        dw = dispatch_by_hr.get(emp.hr_employee_id) or dispatch_by_hr.get(
            emp.hr_employee_id.upper()
        )
        if dw is None:
            for k in keys:
                if k in dispatch_by_name:
                    dw = dispatch_by_name[k]
                    break
        if dw is None:
            match, score = fuzzy_best_match(
                keys[0], list(dispatch_by_name.keys()), min_score=0.88
            )
            if match:
                dw = dispatch_by_name[match]
        if dw is not None:
            emp.dispatch_worker_id = str(dw.get("worker_id") or "") or None
            matched_dispatch += 1

    if not payroll_available:
        add_exception(
            session,
            source="resolve_employees",
            record_ref="payroll",
            issue="No payroll employees available to match (API empty or unreachable)",
            action_taken="Left all payroll_employee_id null",
            confidence=0.3,
        )

    # Payroll employees with no HR match
    seen_ids: set[Any] = set()
    for k in list(unmatched_payroll_keys):
        pe = payroll_by_key.get(k)
        if not pe:
            continue
        pid = pe.get("employee_id")
        if pid in seen_ids:
            continue
        # Only flag if no HR emp has this payroll id
        if any(e.payroll_employee_id == pid for e in employees):
            continue
        seen_ids.add(pid)
        add_exception(
            session,
            source="resolve_employees",
            record_ref=str(pid),
            issue=f"Payroll employee '{pe.get('name')}' has no HR roster match",
            action_taken="Skipped; not in HR spine",
            confidence=0.5,
        )

    # Link punches to employees by name / badge when possible
    emp_by_name: dict[str, Employee] = {}
    for emp in employees:
        for k in employee_match_keys(emp):
            emp_by_name.setdefault(k, emp)

    for punch in session.query(Punch).filter(Punch.employee_id.is_(None)).limit(50000):
        if not punch.employee_name:
            continue
        keys = _payroll_name_keys(punch.employee_name)
        emp = None
        for k in keys:
            if k in emp_by_name:
                emp = emp_by_name[k]
                break
        if emp is None:
            match, score = fuzzy_best_match(
                keys[0], list(emp_by_name.keys()), min_score=0.90
            )
            if match:
                emp = emp_by_name[match]
        if emp:
            punch.employee_id = emp.id
            if punch.badge_no and not emp.badge_no:
                emp.badge_no = punch.badge_no

    # Link filled postings' assigned workers → covering_employee_id
    by_worker = {
        e.dispatch_worker_id: e
        for e in employees
        if e.dispatch_worker_id
    }
    covering_linked = 0
    for posting in session.query(DispatchPosting).all():
        if posting.covering_employee_id:
            continue
        wid = (posting.assigned_worker_id or "").strip()
        if not wid:
            continue
        cover = by_worker.get(wid)
        if cover:
            posting.covering_employee_id = cover.id
            covering_linked += 1

    session.flush()
    return {
        "employees": len(employees),
        "matched_payroll": matched_payroll,
        "matched_dispatch": matched_dispatch,
        "covering_linked": covering_linked,
    }


# ---------------------------------------------------------------------------
# Wage rates / burden
# ---------------------------------------------------------------------------


def apply_wage_rates(session: Session) -> dict[str, Any]:
    """Set employee.base_rate from CBA 2026 + non-union rate card; stash burden."""
    wages = session.info.get("wage_schedule") or {}
    if not wages:
        # Attempt to read from any client meta / re-parse not available — flag
        add_exception(
            session,
            source="wage_schedule",
            record_ref="CBA",
            issue="Wage schedule not present in session; rates may be missing",
            action_taken="Employees without rates left null",
            confidence=0.4,
        )
        return {"updated": 0}

    union_rates = {
        k: Decimal(v) for k, v in (wages.get("union_rates_2026") or {}).items()
    }
    nonunion_rates = {
        k: Decimal(v) for k, v in (wages.get("nonunion_rates_2026") or {}).items()
    }
    burden_union = Decimal(str(wages.get("burden_union") or "0.32"))
    burden_nonunion = Decimal(str(wages.get("burden_nonunion") or "0.18"))

    updated = 0
    for emp in session.query(Employee).all():
        cls = (emp.job_class or "").strip()
        is_union = bool(emp.union_member)
        # Temps / non-union use rate card; floaters covering posts use union class rate per CBA note
        if emp.employment_type and emp.employment_type.upper() == "TEMP":
            rate = nonunion_rates.get(cls)
            burden = burden_nonunion
            is_union = False
        elif is_union:
            rate = union_rates.get(cls)
            burden = burden_union
        else:
            rate = nonunion_rates.get(cls) or union_rates.get(cls)
            burden = burden_nonunion

        if rate is not None:
            emp.base_rate = rate
            updated += 1
        meta = dict(emp.meta or {})
        meta["burden_rate"] = str(burden)
        meta["rate_source"] = "union_cba_2026" if is_union else "nonunion_rate_card_2026"
        emp.meta = meta

        if rate is None:
            add_exception(
                session,
                source="wage_schedule",
                record_ref=emp.hr_employee_id,
                issue=f"No wage rate for job_class={cls!r} union={is_union}",
                action_taken="base_rate left null",
                confidence=0.5,
            )

    # Stash global wage table for KPI/billing modules
    session.info["burden_union"] = burden_union
    session.info["burden_nonunion"] = burden_nonunion
    session.flush()
    return {
        "updated": updated,
        "union_rates": {k: str(v) for k, v in union_rates.items()},
        "nonunion_rates": {k: str(v) for k, v in nonunion_rates.items()},
        "burden_union": str(burden_union),
        "burden_nonunion": str(burden_nonunion),
    }


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


def resolve_all(session: Session, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    summary: dict[str, Any] = {}
    summary["buildings"] = resolve_buildings(session)
    summary["employees"] = resolve_employees(session)
    summary["wages"] = apply_wage_rates(session)
    session.commit()
    logger.info("Resolve complete: %s", summary)
    return summary
