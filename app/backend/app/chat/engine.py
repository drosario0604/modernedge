"""Simple RAG-ish chatbot that answers only from DB / KPI tables."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.kpis.writers import format_money, format_rate
from app.models import (
    Building,
    CalloutEvent,
    Employee,
    FinanceAROpen,
    KPISnapshot,
    PayrollSubmission,
    ReviewItem,
)

logger = logging.getLogger(__name__)

CANNOT_ANSWER = (
    "I can only answer from the loaded assessment data (KPI snapshots, buildings, "
    "employees, call-outs, payroll submissions, AR, and review items). "
    "I don't have enough supporting rows to answer that."
)


@dataclass
class Citation:
    source: str
    ref: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"source": self.source, "ref": self.ref, "detail": self.detail}


@dataclass
class ChatResponse:
    answer: str
    citations: list[Citation] = field(default_factory=list)
    can_answer: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "citations": [c.as_dict() for c in self.citations],
            "can_answer": self.can_answer,
        }


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _find_building(session: Session, question: str) -> Optional[Building]:
    buildings = list(session.scalars(select(Building)).all())
    q = _norm(question)
    # Longer names first to prefer "Meridian Plaza" over "Meridian"
    for b in sorted(buildings, key=lambda x: len(x.name), reverse=True):
        if _norm(b.name) in q:
            return b
        if b.hr_location_code and _norm(b.hr_location_code) in q:
            return b
    return None


def _kpi_for(session: Session, building: Building, period: str = "2026-10") -> Optional[KPISnapshot]:
    return session.scalar(
        select(KPISnapshot).where(
            KPISnapshot.building_id == building.id,
            KPISnapshot.period == period,
        )
    )


def _fmt_kpi_row(snap: KPISnapshot) -> str:
    parts = [
        f"Building: {snap.building}",
        f"Contracted headcount: {snap.contracted_headcount}",
        f"Active headcount: {snap.active_headcount}",
        f"Headcount variance: {snap.headcount_variance}",
        f"Scheduled shifts: {snap.scheduled_shifts}",
        f"Call-out shifts: {snap.callout_shifts}",
        f"Call-out rate: {format_rate(snap.callout_rate)}",
        f"Fill rate: {format_rate(snap.fill_rate)}",
        f"Overtime hours: {snap.overtime_hours}",
        f"Overtime dollars: {format_money(snap.overtime_dollars)}",
        f"Revenue: {format_money(snap.revenue)}",
        f"Labor cost: {format_money(snap.labor_cost)}",
        f"Gross margin: {format_money(snap.gross_margin)} ({format_rate(snap.gross_margin_pct)})",
        f"Unbilled coverage: {snap.unbilled_coverage_count} / {format_money(snap.unbilled_coverage_dollars)}",
    ]
    return "\n".join(parts)


class ChatEngine:
    """Rule + table lookup chatbot (no free-form hallucination)."""

    def __init__(self, session: Session):
        self.session = session

    def ask(self, question: str) -> ChatResponse:
        q = _norm(question)
        if not q:
            return ChatResponse(answer="Please ask a question about the assessment data.", can_answer=False)

        # AR aging
        if "ar" in q or "receivable" in q or "aging" in q:
            return self._ar_aging()

        # Review queue
        if "review" in q and ("queue" in q or "pending" in q or "held" in q):
            return self._review_queue()

        # Payroll submissions
        if "payroll" in q and ("submission" in q or "absence" in q or "written" in q):
            return self._payroll_summary()

        building = _find_building(self.session, question)

        # Headcount questions
        if any(k in q for k in ("headcount", "contracted", "active employee", "staffed")):
            if building:
                return self._building_kpi(building, focus="headcount")
            return self._all_headcounts()

        # Call-out rate / fill rate / OT / margin / revenue
        kpi_keys = (
            "callout",
            "call-out",
            "fill rate",
            "overtime",
            "revenue",
            "labor cost",
            "gross margin",
            "unbilled",
            "kpi",
            "margin",
        )
        if any(k in q for k in kpi_keys):
            if building:
                return self._building_kpi(building)
            return self._list_all_kpis()

        if building and any(k in q for k in ("building", "site", "about", "tell me", "how is", "summary")):
            return self._building_kpi(building)

        if "how many building" in q or "list building" in q or "which building" in q:
            return self._list_buildings()

        if "employee" in q and building:
            return self._employees_at(building)

        if "callout" in q or "call-out" in q:
            if building:
                return self._callouts_at(building)
            return self._callout_totals()

        return ChatResponse(answer=CANNOT_ANSWER, citations=[], can_answer=False)

    def _building_kpi(self, building: Building, focus: str | None = None) -> ChatResponse:
        snap = _kpi_for(self.session, building)
        if snap is None:
            return ChatResponse(
                answer=(
                    f"No KPI snapshot is loaded for {building.name}. "
                    "Run the KPI pipeline first."
                ),
                citations=[Citation("buildings", building.name, "row exists; kpi_snapshots missing")],
                can_answer=False,
            )
        if focus == "headcount":
            answer = (
                f"{building.name}: contracted_headcount={snap.contracted_headcount}, "
                f"active_headcount={snap.active_headcount}, "
                f"headcount_variance={snap.headcount_variance}."
            )
        else:
            answer = _fmt_kpi_row(snap)
        return ChatResponse(
            answer=answer,
            citations=[
                Citation("kpi_snapshots", f"{snap.building}:{snap.period}", f"id={snap.id}"),
            ],
        )

    def _all_headcounts(self) -> ChatResponse:
        snaps = list(self.session.scalars(select(KPISnapshot).order_by(KPISnapshot.building)).all())
        if not snaps:
            return ChatResponse(answer=CANNOT_ANSWER, can_answer=False)
        lines = [
            f"{s.building}: contracted={s.contracted_headcount}, active={s.active_headcount}, var={s.headcount_variance}"
            for s in snaps
        ]
        return ChatResponse(
            answer="\n".join(lines),
            citations=[Citation("kpi_snapshots", s.building, s.period) for s in snaps],
        )

    def _list_all_kpis(self) -> ChatResponse:
        snaps = list(self.session.scalars(select(KPISnapshot).order_by(KPISnapshot.building)).all())
        if not snaps:
            return ChatResponse(answer=CANNOT_ANSWER, can_answer=False)
        blocks = [_fmt_kpi_row(s) for s in snaps]
        return ChatResponse(
            answer="\n\n".join(blocks),
            citations=[Citation("kpi_snapshots", s.building, s.period) for s in snaps],
        )

    def _list_buildings(self) -> ChatResponse:
        buildings = list(self.session.scalars(select(Building).order_by(Building.name)).all())
        if not buildings:
            return ChatResponse(answer=CANNOT_ANSWER, can_answer=False)
        names = [b.name for b in buildings]
        return ChatResponse(
            answer="Buildings: " + ", ".join(names),
            citations=[Citation("buildings", b.name, f"id={b.id}") for b in buildings],
        )

    def _employees_at(self, building: Building) -> ChatResponse:
        emps = list(
            self.session.scalars(
                select(Employee).where(Employee.building_id == building.id)
            ).all()
        )
        if not emps:
            return ChatResponse(
                answer=f"No employees linked to {building.name} in the database.",
                citations=[Citation("buildings", building.name)],
                can_answer=False,
            )
        lines = [
            f"{e.hr_employee_id}: {e.last_name}, {e.first_name} ({e.employment_type}, {e.status})"
            for e in emps
        ]
        return ChatResponse(
            answer=f"{len(emps)} employees at {building.name}:\n" + "\n".join(lines),
            citations=[Citation("employees", e.hr_employee_id) for e in emps[:20]],
        )

    def _callouts_at(self, building: Building) -> ChatResponse:
        rows = list(
            self.session.scalars(
                select(CalloutEvent).where(CalloutEvent.building_id == building.id)
            ).all()
        )
        absences = [r for r in rows if r.is_absence]
        return ChatResponse(
            answer=f"{len(absences)} call-out absences recorded for {building.name}.",
            citations=[
                Citation("callout_events", f"{r.source}:{r.source_ref}", str(r.event_date))
                for r in absences[:15]
            ],
        )

    def _callout_totals(self) -> ChatResponse:
        snaps = list(self.session.scalars(select(KPISnapshot)).all())
        if not snaps:
            return ChatResponse(answer=CANNOT_ANSWER, can_answer=False)
        lines = [f"{s.building}: callout_shifts={s.callout_shifts}, rate={format_rate(s.callout_rate)}" for s in snaps]
        return ChatResponse(
            answer="\n".join(lines),
            citations=[Citation("kpi_snapshots", s.building, "callout_shifts") for s in snaps],
        )

    def _ar_aging(self) -> ChatResponse:
        rows = list(self.session.scalars(select(FinanceAROpen)).all())
        if not rows:
            return ChatResponse(answer=CANNOT_ANSWER, can_answer=False)
        totals: dict[str, Decimal] = {}
        for r in rows:
            bucket = r.aging_bucket or "unknown"
            bal = r.open_balance if r.open_balance is not None else (r.amount or Decimal("0")) - (
                r.payments_applied or Decimal("0")
            )
            totals[bucket] = totals.get(bucket, Decimal("0")) + Decimal(str(bal))
        lines = [f"{k}: {format_money(v)}" for k, v in sorted(totals.items())]
        return ChatResponse(
            answer="AR open by bucket:\n" + "\n".join(lines),
            citations=[Citation("finance_ar_open", "aggregate", f"rows={len(rows)}")],
        )

    def _review_queue(self) -> ChatResponse:
        items = list(
            self.session.scalars(
                select(ReviewItem).where(ReviewItem.status == "pending")
            ).all()
        )
        if not items:
            return ChatResponse(
                answer="No pending review items.",
                citations=[Citation("review_items", "pending", "count=0")],
            )
        lines = [f"#{i.id} [{i.kind}] {i.title or ''}: {i.reason or ''}" for i in items[:25]]
        return ChatResponse(
            answer=f"{len(items)} pending review item(s):\n" + "\n".join(lines),
            citations=[Citation("review_items", str(i.id), i.status) for i in items[:25]],
        )

    def _payroll_summary(self) -> ChatResponse:
        rows = list(self.session.scalars(select(PayrollSubmission)).all())
        if not rows:
            return ChatResponse(answer=CANNOT_ANSWER, can_answer=False)
        written = sum(1 for r in rows if r.decision == "written")
        held = sum(1 for r in rows if r.decision == "held_for_review")
        return ChatResponse(
            answer=f"Payroll submissions: {len(rows)} total, {written} written, {held} held_for_review.",
            citations=[Citation("payroll_submissions", "aggregate", f"n={len(rows)}")],
        )


def answer_question(session: Session, question: str) -> dict[str, Any]:
    return ChatEngine(session).ask(question).as_dict()
