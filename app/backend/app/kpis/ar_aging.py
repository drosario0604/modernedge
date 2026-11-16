"""AR aging as of 2026-10-31 across both legal entities."""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.kpis.writers import AR_BUCKET_ORDER, round_money, write_ar_aging_csv
from app.models import FinanceAROpen

logger = logging.getLogger(__name__)

AS_OF = date(2026, 10, 31)


def aging_bucket(due_date: date | None, *, as_of: date = AS_OF) -> str:
    """Map days past due to the assessment AR bucket names."""
    if due_date is None:
        # Age from missing due date as current (not yet due) — conservative.
        return "current"
    days_past = (as_of - due_date).days
    if days_past <= 0:
        return "current"
    if days_past <= 30:
        return "1_30"
    if days_past <= 60:
        return "31_60"
    if days_past <= 90:
        return "61_90"
    return "over_90"


def _open_balance(row: FinanceAROpen) -> Decimal:
    if row.open_balance is not None:
        return Decimal(str(row.open_balance))
    amount = Decimal(str(row.amount or 0))
    paid = Decimal(str(row.payments_applied or 0))
    # Credit memos: open at their own amount (often negative or credit type)
    doc_type = (row.document_type or "").upper()
    if doc_type in {"CREDIT", "CREDIT_MEMO", "CM"}:
        # Open at own amount; if amount already signed, use as-is.
        return amount if row.payments_applied is None else amount - paid
    return amount - paid


def compute_ar_aging(
    session: Session,
    *,
    as_of: date = AS_OF,
    refresh_buckets: bool = True,
) -> list[dict[str, Any]]:
    """
    Aggregate open AR into aging buckets.

    Uses FinanceAROpen rows (both entities). Open balance = amount − payments
    applied; credit memos age from document date / due date at their amount.
    """
    totals = {bucket: Decimal("0.00") for bucket in AR_BUCKET_ORDER}
    rows = list(session.scalars(select(FinanceAROpen)).all())

    for row in rows:
        # Prefer due_date; for credit memos without due_date, use document_date
        due = row.due_date
        doc_type = (row.document_type or "").upper()
        if due is None and doc_type in {"CREDIT", "CREDIT_MEMO", "CM"}:
            due = row.document_date
        if due is None:
            due = row.document_date

        bucket = row.aging_bucket if row.aging_bucket in totals and not refresh_buckets else aging_bucket(due, as_of=as_of)
        if bucket not in totals:
            bucket = aging_bucket(due, as_of=as_of)

        balance = _open_balance(row)
        # Skip fully settled
        if balance == 0:
            continue

        totals[bucket] += balance

        if refresh_buckets:
            row.aging_bucket = bucket
            row.as_of_date = as_of
            if row.open_balance is None:
                row.open_balance = round_money(balance)

    if refresh_buckets:
        session.commit()

    result = [
        {"bucket": bucket, "open_amount": round_money(totals[bucket])}
        for bucket in AR_BUCKET_ORDER
    ]
    logger.info("AR aging as of %s: %s", as_of.isoformat(), result)
    return result


def write_ar_aging(
    session: Session,
    *,
    output_path: Path | str | None = None,
    as_of: date = AS_OF,
) -> Path:
    """Compute AR aging and write output/ar_aging.csv."""
    settings = get_settings()
    path = Path(output_path) if output_path else settings.output_dir / "ar_aging.csv"
    rows = compute_ar_aging(session, as_of=as_of)
    return write_ar_aging_csv(path, rows)
