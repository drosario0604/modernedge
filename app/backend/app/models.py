"""SQLAlchemy ORM models for the ModernEdge assessment data spine."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Client(Base, TimestampMixin):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    entity_code: Mapped[Optional[str]] = mapped_column(String(32))
    billing_type: Mapped[Optional[str]] = mapped_column(String(32))  # fixed | cost_plus
    cost_plus_markup: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4))
    backup_note_template: Mapped[Optional[str]] = mapped_column(String(64))
    contract_path: Mapped[Optional[str]] = mapped_column(String(512))
    meta: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)

    buildings: Mapped[list["Building"]] = relationship(back_populates="client")


class Building(Base, TimestampMixin):
    __tablename__ = "buildings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    client_id: Mapped[Optional[int]] = mapped_column(ForeignKey("clients.id"))
    hr_location_code: Mapped[Optional[str]] = mapped_column(String(64))
    dispatch_site_id: Mapped[Optional[str]] = mapped_column(String(64))
    finance_site_name: Mapped[Optional[str]] = mapped_column(String(255))
    department_code: Mapped[Optional[str]] = mapped_column(String(32))
    timezone: Mapped[Optional[str]] = mapped_column(String(64), default="America/New_York")
    contracted_day_posts: Mapped[Optional[int]] = mapped_column(Integer)
    contracted_night_posts: Mapped[Optional[int]] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    meta: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)

    client: Mapped[Optional[Client]] = relationship(back_populates="buildings")
    departments: Mapped[list["Department"]] = relationship(back_populates="building")


class Department(Base, TimestampMixin):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(255))
    building_id: Mapped[Optional[int]] = mapped_column(ForeignKey("buildings.id"))
    entity_code: Mapped[Optional[str]] = mapped_column(String(32))
    meta: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)

    building: Mapped[Optional[Building]] = relationship(back_populates="departments")


class Employee(Base, TimestampMixin):
    __tablename__ = "employees"
    __table_args__ = (
        UniqueConstraint("hr_employee_id", name="uq_employees_hr_employee_id"),
        Index("ix_employees_payroll_employee_id", "payroll_employee_id"),
        Index("ix_employees_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hr_employee_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payroll_employee_id: Mapped[Optional[int]] = mapped_column(Integer)
    dispatch_worker_id: Mapped[Optional[str]] = mapped_column(String(64))
    badge_no: Mapped[Optional[str]] = mapped_column(String(64))

    last_name: Mapped[str] = mapped_column(String(128), nullable=False)
    first_name: Mapped[str] = mapped_column(String(128), nullable=False)
    middle_initial: Mapped[Optional[str]] = mapped_column(String(8))
    display_name: Mapped[Optional[str]] = mapped_column(String(255))

    job_class: Mapped[Optional[str]] = mapped_column(String(64))
    union_member: Mapped[Optional[bool]] = mapped_column(Boolean)
    local_member_no: Mapped[Optional[str]] = mapped_column(String(64))
    home_location: Mapped[Optional[str]] = mapped_column(String(64))
    building_id: Mapped[Optional[int]] = mapped_column(ForeignKey("buildings.id"))
    department_code: Mapped[Optional[str]] = mapped_column(String(32))
    shift: Mapped[Optional[str]] = mapped_column(String(16))
    employment_type: Mapped[Optional[str]] = mapped_column(String(32))
    hire_date: Mapped[Optional[date]] = mapped_column(Date)
    termination_date: Mapped[Optional[date]] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", nullable=False)

    sick_days_remaining: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2))
    clinic_day_used: Mapped[Optional[bool]] = mapped_column(Boolean)
    primary_language: Mapped[Optional[str]] = mapped_column(String(64))
    base_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    resolution_notes: Mapped[Optional[str]] = mapped_column(Text)
    meta: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)

    building: Mapped[Optional[Building]] = relationship()


class Punch(Base, TimestampMixin):
    __tablename__ = "punches"
    __table_args__ = (
        Index("ix_punches_employee_id", "employee_id"),
        Index("ix_punches_punch_time", "punch_time"),
        Index("ix_punches_site", "site"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employees.id"))
    badge_no: Mapped[Optional[str]] = mapped_column(String(64))
    employee_name: Mapped[Optional[str]] = mapped_column(String(255))
    site: Mapped[Optional[str]] = mapped_column(String(128))
    building_id: Mapped[Optional[int]] = mapped_column(ForeignKey("buildings.id"))
    punch_type: Mapped[Optional[str]] = mapped_column(String(32))
    punch_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    device_id: Mapped[Optional[str]] = mapped_column(String(64))
    source_file: Mapped[Optional[str]] = mapped_column(String(255))
    raw: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)


class CalloutEvent(Base, TimestampMixin):
    __tablename__ = "callout_events"
    __table_args__ = (
        Index("ix_callout_events_source", "source"),
        Index("ix_callout_events_employee_date", "employee_id", "event_date"),
        UniqueConstraint(
            "source", "source_ref", name="uq_callout_events_source_ref"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)  # reception_log | area_manager_chat
    source_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    raw_text: Mapped[Optional[str]] = mapped_column(Text)
    employee_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employees.id"))
    employee_name_raw: Mapped[Optional[str]] = mapped_column(String(255))
    event_date: Mapped[Optional[date]] = mapped_column(Date)
    shift: Mapped[Optional[str]] = mapped_column(String(16))
    building_id: Mapped[Optional[int]] = mapped_column(ForeignKey("buildings.id"))
    building_raw: Mapped[Optional[str]] = mapped_column(String(255))
    reason_code: Mapped[Optional[str]] = mapped_column(String(32))
    reason_text: Mapped[Optional[str]] = mapped_column(Text)
    hours: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2))
    noticed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    is_absence: Mapped[Optional[bool]] = mapped_column(Boolean)
    confidence: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 4))
    status: Mapped[str] = mapped_column(String(32), default="parsed", nullable=False)
    structured: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    meta: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)


class DispatchPosting(Base, TimestampMixin):
    __tablename__ = "dispatch_postings"
    __table_args__ = (
        UniqueConstraint("posting_id", name="uq_dispatch_postings_posting_id"),
        Index("ix_dispatch_postings_site_date", "site_id", "shift_date"),
        Index("ix_dispatch_postings_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    posting_id: Mapped[str] = mapped_column(String(64), nullable=False)
    site_id: Mapped[Optional[str]] = mapped_column(String(64))
    building_id: Mapped[Optional[int]] = mapped_column(ForeignKey("buildings.id"))
    shift_date: Mapped[Optional[date]] = mapped_column(Date)
    shift: Mapped[Optional[str]] = mapped_column(String(8))  # D | N
    start_local: Mapped[Optional[str]] = mapped_column(String(32))
    reason: Mapped[Optional[str]] = mapped_column(String(32))  # CALLOUT | VACATION
    absent_employee_text: Mapped[Optional[str]] = mapped_column(String(255))
    absent_employee_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employees.id"))
    status: Mapped[Optional[str]] = mapped_column(String(32))
    assigned_worker_id: Mapped[Optional[str]] = mapped_column(String(64))
    covering_employee_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employees.id"))
    fill_type: Mapped[Optional[str]] = mapped_column(String(32))  # STANDARD | OT
    created_at_src: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    updated_at_src: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    events: Mapped[Optional[list[dict[str, Any]]]] = mapped_column(JSON)
    raw: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)


class FinanceInvoiceLine(Base, TimestampMixin):
    __tablename__ = "finance_invoice_lines"
    __table_args__ = (
        Index("ix_finance_invoice_lines_period", "service_period"),
        Index("ix_finance_invoice_lines_building", "building_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_code: Mapped[str] = mapped_column(String(32), nullable=False)
    invoice_no: Mapped[Optional[str]] = mapped_column(String(64))
    invoice_date: Mapped[Optional[date]] = mapped_column(Date)
    customer_code: Mapped[Optional[str]] = mapped_column(String(64))
    customer_name: Mapped[Optional[str]] = mapped_column(String(255))
    site_name: Mapped[Optional[str]] = mapped_column(String(255))
    building_id: Mapped[Optional[int]] = mapped_column(ForeignKey("buildings.id"))
    line_type: Mapped[Optional[str]] = mapped_column(String(64))
    description: Mapped[Optional[str]] = mapped_column(Text)
    service_period: Mapped[Optional[str]] = mapped_column(String(16))  # YYYY-MM
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2))
    raw: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)


class FinanceAROpen(Base, TimestampMixin):
    __tablename__ = "finance_ar_open"
    __table_args__ = (
        Index("ix_finance_ar_open_due_date", "due_date"),
        Index("ix_finance_ar_open_bucket", "aging_bucket"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_code: Mapped[str] = mapped_column(String(32), nullable=False)
    document_no: Mapped[Optional[str]] = mapped_column(String(64))
    document_type: Mapped[Optional[str]] = mapped_column(String(32))
    document_date: Mapped[Optional[date]] = mapped_column(Date)
    due_date: Mapped[Optional[date]] = mapped_column(Date)
    customer_code: Mapped[Optional[str]] = mapped_column(String(64))
    customer_name: Mapped[Optional[str]] = mapped_column(String(255))
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2))
    payments_applied: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2))
    open_balance: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2))
    aging_bucket: Mapped[Optional[str]] = mapped_column(String(16))
    as_of_date: Mapped[Optional[date]] = mapped_column(Date)
    raw: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)


class FinanceLaborCost(Base, TimestampMixin):
    __tablename__ = "finance_labor_costs"
    __table_args__ = (
        Index("ix_finance_labor_costs_dept_period", "department_code", "period"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_code: Mapped[str] = mapped_column(String(32), nullable=False)
    period: Mapped[str] = mapped_column(String(16), nullable=False)  # YYYY-MM
    department_code: Mapped[Optional[str]] = mapped_column(String(32))
    building_id: Mapped[Optional[int]] = mapped_column(ForeignKey("buildings.id"))
    account: Mapped[Optional[str]] = mapped_column(String(64))
    account_name: Mapped[Optional[str]] = mapped_column(String(255))
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2))
    raw: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)


class ExceptionRecord(Base, TimestampMixin):
    __tablename__ = "exception_records"
    __table_args__ = (
        Index("ix_exception_records_source", "source"),
        Index("ix_exception_records_confidence", "confidence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    record_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    issue: Mapped[str] = mapped_column(Text, nullable=False)
    action_taken: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 4))
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    meta: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)


class PayrollSubmission(Base, TimestampMixin):
    __tablename__ = "payroll_submissions"
    __table_args__ = (
        Index("ix_payroll_submissions_employee_date", "employee_id", "absence_date"),
        Index("ix_payroll_submissions_submission_id", "submission_id"),
        UniqueConstraint(
            "source",
            "source_ref",
            name="uq_payroll_submissions_source_ref",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    employee_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employees.id"))
    payroll_employee_id: Mapped[Optional[int]] = mapped_column(Integer)
    absence_date: Mapped[Optional[date]] = mapped_column(Date)
    code: Mapped[Optional[str]] = mapped_column(String(16))
    hours: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2))
    note: Mapped[Optional[str]] = mapped_column(Text)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)  # written | held_for_review
    submission_id: Mapped[Optional[str]] = mapped_column(String(64))
    final_status: Mapped[Optional[str]] = mapped_column(String(32))
    reason: Mapped[Optional[str]] = mapped_column(Text)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    callout_event_id: Mapped[Optional[int]] = mapped_column(ForeignKey("callout_events.id"))
    request_payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    response_payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)


class AuditLog(Base, TimestampMixin):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_action", "action"),
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[Optional[str]] = mapped_column(String(64))
    entity_id: Mapped[Optional[str]] = mapped_column(String(128))
    actor: Mapped[str] = mapped_column(String(64), default="system", nullable=False)
    detail: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    success: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ReviewItem(Base, TimestampMixin):
    __tablename__ = "review_items"
    __table_args__ = (
        Index("ix_review_items_status", "status"),
        Index("ix_review_items_kind", "kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(255))
    reason: Mapped[Optional[str]] = mapped_column(Text)
    confidence: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 4))
    source: Mapped[Optional[str]] = mapped_column(String(128))
    source_ref: Mapped[Optional[str]] = mapped_column(String(255))
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    resolution: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[Optional[str]] = mapped_column(String(64))


class CoverageBackupNote(Base, TimestampMixin):
    __tablename__ = "coverage_backup_notes"
    __table_args__ = (
        UniqueConstraint("posting_id", name="uq_coverage_backup_notes_posting_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    posting_id: Mapped[str] = mapped_column(String(64), nullable=False)
    dispatch_posting_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("dispatch_postings.id")
    )
    building_id: Mapped[Optional[int]] = mapped_column(ForeignKey("buildings.id"))
    client_id: Mapped[Optional[int]] = mapped_column(ForeignKey("clients.id"))
    covering_employee_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employees.id"))
    shift_date: Mapped[Optional[date]] = mapped_column(Date)
    fill_type: Mapped[Optional[str]] = mapped_column(String(32))
    billable_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2))
    note_text: Mapped[Optional[str]] = mapped_column(Text)
    file_path: Mapped[Optional[str]] = mapped_column(String(512))
    template_used: Mapped[Optional[str]] = mapped_column(String(64))
    meta: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)


class KPISnapshot(Base, TimestampMixin):
    __tablename__ = "kpi_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "building_id", "period", name="uq_kpi_snapshots_building_period"
        ),
        Index("ix_kpi_snapshots_period", "period"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    building_id: Mapped[Optional[int]] = mapped_column(ForeignKey("buildings.id"))
    building: Mapped[str] = mapped_column(String(255), nullable=False)
    period: Mapped[str] = mapped_column(String(16), default="2026-10", nullable=False)

    contracted_headcount: Mapped[Optional[int]] = mapped_column(Integer)
    active_headcount: Mapped[Optional[int]] = mapped_column(Integer)
    headcount_variance: Mapped[Optional[int]] = mapped_column(Integer)
    scheduled_shifts: Mapped[Optional[int]] = mapped_column(Integer)
    callout_shifts: Mapped[Optional[int]] = mapped_column(Integer)
    callout_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6))
    fill_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6))
    overtime_hours: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    overtime_dollars: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2))
    revenue: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2))
    labor_cost: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2))
    gross_margin: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2))
    gross_margin_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6))
    unbilled_coverage_count: Mapped[Optional[int]] = mapped_column(Integer)
    unbilled_coverage_dollars: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2))
    detail: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
