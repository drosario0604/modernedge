"""Pull payroll / dispatch / finance data via HTTP clients and parse reports."""

from __future__ import annotations

import csv
import io
import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.clients import DispatchClient, FinanceClient, PayrollClient
from app.config import Settings, get_settings
from app.ingest.files import add_exception
from app.models import (
    Department,
    DispatchPosting,
    FinanceAROpen,
    FinanceInvoiceLine,
    FinanceLaborCost,
)

logger = logging.getLogger(__name__)

REPORT_CODES = ("INV_REG", "AR_OPEN", "GL_LABOR")


def _dec(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    text = str(value).strip().replace(",", "").replace("$", "")
    if text.endswith("-") and text[:-1].replace(".", "", 1).isdigit():
        text = "-" + text[:-1]
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _parse_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y%m%d", "%d-%b-%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Payroll
# ---------------------------------------------------------------------------


def ingest_payroll_employees(session: Session, settings: Settings) -> list[dict[str, Any]]:
    employees: list[dict[str, Any]] = []
    try:
        with PayrollClient(settings=settings) as client:
            employees = client.iter_employees(page_size=25)
    except Exception as exc:  # noqa: BLE001
        add_exception(
            session,
            source="payroll_api",
            record_ref="GET /v1/employees",
            issue=f"Failed to pull payroll employees: {exc}",
            action_taken="Continued without payroll IDs; employee resolve will flag unmatched",
            confidence=0.3,
        )
        session.info["payroll_employees"] = []
        return []

    session.info["payroll_employees"] = employees
    logger.info("Pulled %s payroll employees", len(employees))
    return employees


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def ingest_dispatch(session: Session, settings: Settings) -> dict[str, Any]:
    sites: list[dict[str, Any]] = []
    workers: list[dict[str, Any]] = []
    postings: list[dict[str, Any]] = []
    # Fetch each resource independently so a workers flake does not drop postings.
    with DispatchClient(settings=settings) as client:
        for label, loader, sink in (
            ("sites", client.list_sites, "sites"),
            ("workers", client.list_workers, "workers"),
            ("postings", client.list_postings, "postings"),
        ):
            try:
                data = loader()
                if sink == "sites":
                    sites = data
                elif sink == "workers":
                    workers = data
                else:
                    postings = data
            except Exception as exc:  # noqa: BLE001
                add_exception(
                    session,
                    source="dispatch_api",
                    record_ref=label,
                    issue=f"Failed to pull dispatch {label}: {exc}",
                    action_taken="Continued with partial dispatch data",
                    confidence=0.35,
                )

    session.info["dispatch_sites"] = sites
    session.info["dispatch_workers"] = workers

    for raw in postings:
        posting_id = str(raw.get("posting_id") or "").strip()
        if not posting_id:
            continue
        existing = (
            session.query(DispatchPosting)
            .filter_by(posting_id=posting_id)
            .one_or_none()
        )
        events = raw.get("events") or []
        assigned_worker = None
        fill_type = None
        for ev in events:
            if str(ev.get("type") or ev.get("event_type") or "").upper() == "ASSIGNED":
                assigned_worker = ev.get("worker_id")
                fill_type = ev.get("fill_type")

        # Prefer top-level fields if present
        assigned_worker = raw.get("assigned_worker_id") or assigned_worker
        fill_type = raw.get("fill_type") or fill_type

        fields = dict(
            site_id=str(raw.get("site_id") or "") or None,
            shift_date=_parse_date(raw.get("shift_date")),
            shift=str(raw.get("shift") or "") or None,
            start_local=str(raw.get("start_local") or "") or None,
            reason=str(raw.get("reason") or "") or None,
            absent_employee_text=str(raw.get("absent_employee_text") or "") or None,
            status=str(raw.get("status") or "") or None,
            assigned_worker_id=str(assigned_worker) if assigned_worker else None,
            fill_type=str(fill_type) if fill_type else None,
            created_at_src=_parse_dt(raw.get("created_at")),
            updated_at_src=_parse_dt(raw.get("updated_at")),
            events=events if isinstance(events, list) else None,
            raw=raw,
        )
        if existing:
            for k, v in fields.items():
                setattr(existing, k, v)
        else:
            session.add(DispatchPosting(posting_id=posting_id, **fields))

    session.flush()
    logger.info(
        "Dispatch: %s sites, %s workers, %s postings",
        len(sites),
        len(workers),
        len(postings),
    )
    return {"sites": sites, "workers": workers, "postings": len(postings)}


# ---------------------------------------------------------------------------
# Finance reference + reports
# ---------------------------------------------------------------------------


def ingest_finance_reference(session: Session, settings: Settings) -> dict[str, Any]:
    entities: list[dict[str, Any]] = []
    customers: list[dict[str, Any]] = []
    sites: list[dict[str, Any]] = []
    try:
        with FinanceClient(settings=settings) as client:
            entities = client.list_entities()
            customers = client.iter_customers()
            sites = client.list_sites()
    except Exception as exc:  # noqa: BLE001
        add_exception(
            session,
            source="finance_api",
            record_ref="entities/customers/sites",
            issue=f"Failed to pull finance reference data: {exc}",
            action_taken="Continued without finance reference",
            confidence=0.3,
        )
        session.info["finance_entities"] = []
        session.info["finance_customers"] = []
        session.info["finance_sites"] = []
        return {"entities": [], "customers": [], "sites": []}

    session.info["finance_entities"] = entities
    session.info["finance_customers"] = customers
    session.info["finance_sites"] = sites

    for site in sites:
        dept_code = str(
            site.get("department_code")
            or site.get("department")
            or site.get("ledger_department")
            or ""
        ).strip()
        site_name = str(site.get("site_name") or site.get("name") or "").strip()
        entity_code = str(site.get("entity_code") or "").strip() or None
        if not dept_code:
            continue
        existing = session.query(Department).filter_by(code=dept_code).one_or_none()
        if existing is None:
            session.add(
                Department(
                    code=dept_code,
                    name=site_name or dept_code,
                    entity_code=entity_code,
                    meta={"finance_site": site},
                )
            )
        else:
            existing.name = site_name or existing.name
            existing.entity_code = entity_code or existing.entity_code
            meta = dict(existing.meta or {})
            meta["finance_site"] = site
            existing.meta = meta

    session.flush()
    logger.info(
        "Finance ref: %s entities, %s customers, %s sites",
        len(entities),
        len(customers),
        len(sites),
    )
    return {"entities": entities, "customers": customers, "sites": sites}


def ingest_finance_reports(
    session: Session,
    settings: Settings,
    *,
    period: str | None = None,
) -> dict[str, int]:
    period = period or settings.period
    entities = session.info.get("finance_entities") or []
    if not entities:
        # Try a fresh pull
        entities = ingest_finance_reference(session, settings).get("entities") or []

    entity_codes = []
    for e in entities:
        code = str(e.get("entity_code") or e.get("code") or "").strip()
        if code:
            entity_codes.append(code)
    if not entity_codes:
        add_exception(
            session,
            source="finance_reports",
            record_ref=period,
            issue="No finance entity codes available; cannot run reports",
            action_taken="Skipped INV_REG / AR_OPEN / GL_LABOR",
            confidence=0.2,
        )
        return {"inv_lines": 0, "ar_rows": 0, "gl_rows": 0}

    counts = {"inv_lines": 0, "ar_rows": 0, "gl_rows": 0}
    try:
        client = FinanceClient(settings=settings)
    except Exception as exc:  # noqa: BLE001
        add_exception(
            session,
            source="finance_reports",
            record_ref=period,
            issue=f"Could not construct finance client: {exc}",
            action_taken="Skipped reports",
            confidence=0.2,
        )
        return counts

    try:
        for entity_code in entity_codes:
            for report_code in REPORT_CODES:
                try:
                    _job, content = client.run_report(
                        report_code=report_code,
                        entity_code=entity_code,
                        period=period,
                    )
                except Exception as exc:  # noqa: BLE001
                    add_exception(
                        session,
                        source="finance_reports",
                        record_ref=f"{report_code}:{entity_code}:{period}",
                        issue=f"Report failed: {exc}",
                        action_taken="Skipped this entity/report combination",
                        confidence=0.4,
                    )
                    continue

                text = content.decode("utf-8", errors="replace")
                if report_code == "INV_REG":
                    rows = parse_inv_reg(text)
                    for row in rows:
                        session.add(
                            FinanceInvoiceLine(
                                entity_code=entity_code,
                                invoice_no=row.get("invoice_no"),
                                invoice_date=_parse_date(row.get("invoice_date")),
                                customer_code=row.get("customer_code"),
                                customer_name=row.get("customer_name"),
                                site_name=row.get("site_name"),
                                line_type=row.get("line_type"),
                                description=row.get("description"),
                                service_period=row.get("service_period") or period,
                                amount=_dec(row.get("amount")),
                                raw=row,
                            )
                        )
                    counts["inv_lines"] += len(rows)
                elif report_code == "AR_OPEN":
                    rows = parse_ar_open(text)
                    as_of = _parse_date(period + "-31") or date(2026, 10, 31)
                    for row in rows:
                        open_bal = _dec(row.get("open_balance"))
                        if open_bal is None:
                            amt = _dec(row.get("amount")) or Decimal("0")
                            paid = _dec(row.get("payments_applied")) or Decimal("0")
                            open_bal = amt - paid
                        due = _parse_date(row.get("due_date"))
                        bucket = _aging_bucket(due, as_of, open_bal, row)
                        session.add(
                            FinanceAROpen(
                                entity_code=entity_code,
                                document_no=row.get("document_no"),
                                document_type=row.get("document_type"),
                                document_date=_parse_date(row.get("document_date")),
                                due_date=due,
                                customer_code=row.get("customer_code"),
                                customer_name=row.get("customer_name"),
                                amount=_dec(row.get("amount")),
                                payments_applied=_dec(row.get("payments_applied")),
                                open_balance=open_bal,
                                aging_bucket=bucket,
                                as_of_date=as_of,
                                raw=row,
                            )
                        )
                    counts["ar_rows"] += len(rows)
                elif report_code == "GL_LABOR":
                    rows = parse_gl_labor(text)
                    for row in rows:
                        session.add(
                            FinanceLaborCost(
                                entity_code=entity_code,
                                period=period,
                                department_code=row.get("department_code"),
                                account=row.get("account"),
                                account_name=row.get("account_name"),
                                amount=_dec(row.get("amount")),
                                raw=row,
                            )
                        )
                    counts["gl_rows"] += len(rows)
    finally:
        client.close()

    session.flush()
    logger.info("Finance reports loaded: %s", counts)
    return counts


def _aging_bucket(
    due: Optional[date],
    as_of: date,
    open_balance: Optional[Decimal],
    row: dict[str, Any],
) -> Optional[str]:
    explicit = str(row.get("aging_bucket") or row.get("bucket") or "").strip().lower()
    if explicit in {"current", "1_30", "31_60", "61_90", "over_90"}:
        return explicit
    # Credit memos age from document date per KPI defs — still use due_date when present
    basis = due or _parse_date(row.get("document_date"))
    if basis is None:
        return None
    days = (as_of - basis).days
    if days <= 0:
        return "current"
    if days <= 30:
        return "1_30"
    if days <= 60:
        return "31_60"
    if days <= 90:
        return "61_90"
    return "over_90"


# ---------------------------------------------------------------------------
# Report parsers
# ---------------------------------------------------------------------------


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", h.strip().lower()).strip("_")


_INV_HEADER_ALIASES = {
    "invoice_no": {"invoice_no", "invoice_number", "inv_no", "invoice"},
    "invoice_date": {"invoice_date", "inv_date", "date"},
    "customer_code": {"customer_code", "cust_code", "customer_id"},
    "customer_name": {"customer_name", "cust_name", "customer"},
    "site_name": {"site_name", "site", "building", "location"},
    "line_type": {"line_type", "type", "line_type_code"},
    "description": {"description", "desc", "line_description"},
    "service_period": {"service_period", "period", "svc_period"},
    "amount": {"amount", "amt", "line_amount", "extended_amount"},
}

_AR_HEADER_ALIASES = {
    "document_no": {"document_no", "doc_no", "document", "invoice_no"},
    "document_type": {"document_type", "doc_type", "type"},
    "document_date": {"document_date", "doc_date", "date"},
    "due_date": {"due_date", "due"},
    "customer_code": {"customer_code", "cust_code", "customer_id"},
    "customer_name": {"customer_name", "cust_name", "customer"},
    "amount": {"amount", "amt", "original_amount"},
    "payments_applied": {"payments_applied", "applied", "payments", "paid"},
    "open_balance": {"open_balance", "balance", "open_amt", "remaining"},
    "aging_bucket": {"aging_bucket", "bucket", "aging"},
}


def _map_headers(headers: list[str], aliases: dict[str, set[str]]) -> dict[str, int]:
    normed = [_norm_header(h) for h in headers]
    mapping: dict[str, int] = {}
    for field, names in aliases.items():
        for i, h in enumerate(normed):
            if h in names:
                mapping[field] = i
                break
    return mapping


def parse_inv_reg(text: str) -> list[dict[str, Any]]:
    """
    INV_REG: CSV with banner lines above the column header.

    Skip leading non-header lines until a row looks like a CSV header containing
    an invoice-related column.
    """
    lines = text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        lower = line.lower()
        if "," not in line:
            continue
        if "invoice" in lower and ("amount" in lower or "customer" in lower or "site" in lower):
            header_idx = i
            break
        if "inv" in lower and "date" in lower and "amount" in lower:
            header_idx = i
            break
    if header_idx is None:
        # Last resort: first comma-separated line with >= 5 columns
        for i, line in enumerate(lines):
            if line.count(",") >= 4 and not line.strip().startswith("-"):
                header_idx = i
                break
    if header_idx is None:
        return []

    blob = "\n".join(lines[header_idx:])
    reader = csv.reader(io.StringIO(blob))
    rows_list = list(reader)
    if not rows_list:
        return []
    headers = rows_list[0]
    colmap = _map_headers(headers, _INV_HEADER_ALIASES)
    out: list[dict[str, Any]] = []
    for cells in rows_list[1:]:
        if not cells or all(not str(c).strip() for c in cells):
            continue
        # Skip totals / banners that slipped through
        joined = " ".join(str(c) for c in cells).lower()
        if joined.startswith("total") or "end of report" in joined:
            continue
        row: dict[str, Any] = {"_cells": cells}
        for field, idx in colmap.items():
            row[field] = cells[idx].strip() if idx < len(cells) else None
        # If mapping missed invoice_no, take first cell
        if not row.get("invoice_no") and cells:
            row["invoice_no"] = cells[0].strip()
        out.append(row)
    return out


def parse_ar_open(text: str) -> list[dict[str, Any]]:
    """AR_OPEN: pipe-delimited, optional banner above header."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    header_idx = None
    for i, line in enumerate(lines):
        if "|" not in line:
            continue
        lower = line.lower()
        if "due" in lower or "document" in lower or "open" in lower or "customer" in lower:
            header_idx = i
            break
    if header_idx is None:
        for i, line in enumerate(lines):
            if line.count("|") >= 4:
                header_idx = i
                break
    if header_idx is None:
        return []

    headers = [h.strip() for h in lines[header_idx].split("|")]
    colmap = _map_headers(headers, _AR_HEADER_ALIASES)
    out: list[dict[str, Any]] = []
    for line in lines[header_idx + 1 :]:
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.split("|")]
        joined = " ".join(cells).lower()
        if joined.startswith("total") or "end of report" in joined:
            continue
        row: dict[str, Any] = {"_cells": cells}
        for field, idx in colmap.items():
            row[field] = cells[idx] if idx < len(cells) else None
        out.append(row)
    return out


def parse_gl_labor(text: str) -> list[dict[str, Any]]:
    """
    GL_LABOR: fixed-width print layout.

    Detect a header line with Department / Account / Amount, then slice columns
    by header positions. Falls back to whitespace-split when positions unclear.
    """
    lines = text.splitlines()
    header_idx = None
    header_line = ""
    for i, line in enumerate(lines):
        lower = line.lower()
        if "department" in lower and "account" in lower and "amount" in lower:
            header_idx = i
            header_line = line
            break
        if "dept" in lower and "account" in lower and "amount" in lower:
            header_idx = i
            header_line = line
            break
    if header_idx is None:
        return []

    # Column starts from header token positions
    tokens = list(re.finditer(r"\S+", header_line))
    col_starts = [m.start() for m in tokens]
    col_names = [_norm_header(m.group(0)) for m in tokens]

    def slice_row(line: str) -> dict[str, str]:
        values: dict[str, str] = {}
        for i, name in enumerate(col_names):
            start = col_starts[i]
            end = col_starts[i + 1] if i + 1 < len(col_starts) else len(line)
            values[name] = line[start:end].strip() if start < len(line) else ""
        return values

    out: list[dict[str, Any]] = []
    for line in lines[header_idx + 1 :]:
        if not line.strip():
            continue
        lower = line.lower()
        if "end of report" in lower or lower.strip().startswith("total"):
            continue
        # Skip page headers / rules
        if set(line.strip()) <= {"-", "="}:
            continue

        raw = slice_row(line)
        # Map to canonical fields
        dept = (
            raw.get("department")
            or raw.get("department_code")
            or raw.get("dept")
            or raw.get("dept_code")
            or ""
        )
        account = raw.get("account") or raw.get("acct") or ""
        account_name = (
            raw.get("account_name")
            or raw.get("description")
            or raw.get("acct_name")
            or raw.get("name")
            or ""
        )
        amount_raw = raw.get("amount") or raw.get("amt") or ""
        # If amount empty, try last whitespace token
        if not amount_raw:
            parts = line.split()
            if parts:
                amount_raw = parts[-1]
                if not dept and len(parts) >= 1:
                    dept = parts[0]
                if not account and len(parts) >= 2:
                    account = parts[1]

        if not dept and not account:
            continue
        # Skip pure header repeats
        if _norm_header(dept) in {"department", "dept"}:
            continue

        out.append(
            {
                "department_code": dept,
                "account": account,
                "account_name": account_name or None,
                "amount": amount_raw,
                "_raw_line": line,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def ingest_apis(session: Session, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    summary: dict[str, Any] = {}
    summary["payroll_employees"] = len(ingest_payroll_employees(session, settings))
    summary["dispatch"] = ingest_dispatch(session, settings)
    summary["finance_ref"] = {
        k: len(v) if isinstance(v, list) else v
        for k, v in ingest_finance_reference(session, settings).items()
    }
    summary["finance_reports"] = ingest_finance_reports(session, settings)
    session.commit()
    return summary
