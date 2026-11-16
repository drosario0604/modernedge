"""CSV writers with decimal formatting for assessment outputs."""

from __future__ import annotations

import csv
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

DOLLAR_QUANT = Decimal("0.01")
RATE_QUANT = Decimal("0.000001")

KPI_COLUMNS: list[str] = [
    "building",
    "contracted_headcount",
    "active_headcount",
    "headcount_variance",
    "scheduled_shifts",
    "callout_shifts",
    "callout_rate",
    "fill_rate",
    "overtime_hours",
    "overtime_dollars",
    "revenue",
    "labor_cost",
    "gross_margin",
    "gross_margin_pct",
    "unbilled_coverage_count",
    "unbilled_coverage_dollars",
]

AR_COLUMNS: list[str] = ["bucket", "open_amount"]

AR_BUCKET_ORDER: list[str] = ["current", "1_30", "31_60", "61_90", "over_90"]

# Fields rendered as dollars (exactly two decimals, no symbols/separators).
_DOLLAR_FIELDS = frozenset(
    {
        "overtime_dollars",
        "revenue",
        "labor_cost",
        "gross_margin",
        "unbilled_coverage_dollars",
        "open_amount",
    }
)

# Rates / percentages as decimals (not percent).
_RATE_FIELDS = frozenset({"callout_rate", "fill_rate", "gross_margin_pct"})

# Hours with two decimals for consistency with dollar style where fractional.
_HOUR_FIELDS = frozenset({"overtime_hours"})


def round_money(value: Any) -> Decimal:
    """Round a value to the nearest cent (half up)."""
    if value is None:
        return Decimal("0.00")
    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    return dec.quantize(DOLLAR_QUANT, rounding=ROUND_HALF_UP)


def format_money(value: Any) -> str:
    return f"{round_money(value):.2f}"


def format_rate(value: Any, *, places: int = 6) -> str:
    """Format a rate/percentage as a plain decimal string."""
    if value is None:
        return "0"
    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    quant = Decimal(10) ** -places
    normalized = dec.quantize(quant, rounding=ROUND_HALF_UP)
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def format_hours(value: Any) -> str:
    if value is None:
        return "0.00"
    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    return f"{dec.quantize(DOLLAR_QUANT, rounding=ROUND_HALF_UP):.2f}"


def _format_cell(column: str, value: Any) -> str:
    if column in _DOLLAR_FIELDS:
        return format_money(value)
    if column in _RATE_FIELDS:
        return format_rate(value)
    if column in _HOUR_FIELDS:
        return format_hours(value)
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def write_csv(
    path: Path | str,
    columns: Sequence[str],
    rows: Iterable[Mapping[str, Any]],
) -> Path:
    """Write rows with the exact column order and assessment formatting."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: _format_cell(col, row.get(col)) for col in columns})
    return out


def write_kpis_csv(path: Path | str, rows: Iterable[Mapping[str, Any]]) -> Path:
    return write_csv(path, KPI_COLUMNS, rows)


def write_ar_aging_csv(path: Path | str, rows: Iterable[Mapping[str, Any]]) -> Path:
    # Ensure canonical bucket order even if caller passes a dict-like source.
    by_bucket = {str(r.get("bucket")): r for r in rows}
    ordered = [
        by_bucket.get(bucket, {"bucket": bucket, "open_amount": Decimal("0.00")})
        for bucket in AR_BUCKET_ORDER
    ]
    return write_csv(path, AR_COLUMNS, ordered)
