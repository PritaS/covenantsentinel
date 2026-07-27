"""CovenantSentinel data models.

Design principles:
- Every covenant term is versioned and carries source-document citations.
- Every test result persists its full input vector (audit-reconstructable).
- NOI is defined PER LOAN, per the loan document — never globally.
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (JSON, Boolean, Date, DateTime, Enum, Float, ForeignKey,
                        Integer, String, Text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ── Tenancy ────────────────────────────────────────────────────────────────
class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    properties: Mapped[list["Property"]] = relationship(back_populates="tenant")


class Property(Base):
    __tablename__ = "properties"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    entity_name: Mapped[str] = mapped_column(String)  # owning LLC
    units: Mapped[int] = mapped_column(Integer, default=0)
    ingest_email_alias: Mapped[str | None] = mapped_column(String)
    tenant: Mapped[Tenant] = relationship(back_populates="properties")
    loans: Mapped[list["Loan"]] = relationship(back_populates="property")
    financial_periods: Mapped[list["FinancialPeriod"]] = relationship(
        back_populates="property")


# ── Loans & covenant terms ─────────────────────────────────────────────────
class RateType(str, enum.Enum):
    FIXED = "fixed"
    FLOATING = "floating"


class Loan(Base):
    __tablename__ = "loans"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    property_id: Mapped[str] = mapped_column(ForeignKey("properties.id"), index=True)
    lender: Mapped[str] = mapped_column(String)
    original_balance: Mapped[float] = mapped_column(Float)
    current_balance: Mapped[float] = mapped_column(Float)
    rate_type: Mapped[RateType] = mapped_column(Enum(RateType))
    fixed_rate: Mapped[float | None] = mapped_column(Float)      # e.g. 0.0525
    floating_spread: Mapped[float | None] = mapped_column(Float)  # over index
    index_name: Mapped[str | None] = mapped_column(String)        # "SOFR_1M"
    io_period_end: Mapped[date | None] = mapped_column(Date)      # interest-only until
    amortization_months: Mapped[int | None] = mapped_column(Integer)  # e.g. 360
    origination_date: Mapped[date] = mapped_column(Date)
    maturity_date: Mapped[date] = mapped_column(Date)
    rate_cap_expiry: Mapped[date | None] = mapped_column(Date)
    extension_test_deadline: Mapped[date | None] = mapped_column(Date)
    property: Mapped[Property] = relationship(back_populates="loans")
    covenant_terms: Mapped[list["CovenantTerm"]] = relationship(
        back_populates="loan")


class CovenantType(str, enum.Enum):
    DSCR = "dscr"
    DEBT_YIELD = "debt_yield"
    LTV = "ltv"
    OCCUPANCY = "occupancy"
    LIQUIDITY = "liquidity"


class TermStatus(str, enum.Enum):
    PENDING_VERIFICATION = "pending_verification"
    ACTIVE = "active"
    SUPERSEDED = "superseded"  # by amendment


class CovenantTerm(Base):
    """A single covenant as defined in a specific loan document.

    noi_definition (JSON) example — THIS is the per-lender nuance:
        {
          "basis": "trailing_12",              # or "annualized_3", "annualized_1"
          "exclude_categories": ["asset_management_fee"],
          "include_categories": [],
          "replacement_reserve_per_unit": 250,  # lender-imposed, annual $/unit
          "management_fee_floor_pct": 0.03      # min mgmt fee as % of EGI
        }
    """
    __tablename__ = "covenant_terms"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    loan_id: Mapped[str] = mapped_column(ForeignKey("loans.id"), index=True)
    covenant_type: Mapped[CovenantType] = mapped_column(Enum(CovenantType))
    threshold: Mapped[float] = mapped_column(Float)          # e.g. 1.25
    direction: Mapped[str] = mapped_column(String, default="min")  # min|max
    test_frequency: Mapped[str] = mapped_column(String)      # monthly|quarterly|annual
    noi_definition: Mapped[dict] = mapped_column(JSON, default=dict)
    cure_provision: Mapped[str | None] = mapped_column(Text)
    trigger_consequence: Mapped[str | None] = mapped_column(Text)  # cash sweep, etc.
    source_document: Mapped[str | None] = mapped_column(String)
    source_pages: Mapped[str | None] = mapped_column(String)   # "pp. 41-43, 87"
    status: Mapped[TermStatus] = mapped_column(
        Enum(TermStatus), default=TermStatus.PENDING_VERIFICATION)
    verified_by: Mapped[str | None] = mapped_column(String)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    loan: Mapped[Loan] = relationship(back_populates="covenant_terms")


# ── Financials ─────────────────────────────────────────────────────────────
class FinancialPeriod(Base):
    """One month of normalized property financials (canonical chart of accounts).

    line_items JSON: {"gross_potential_rent": ..., "vacancy_loss": ...,
                      "other_income": ..., "repairs_maintenance": ...,
                      "payroll": ..., "utilities": ..., "taxes": ...,
                      "insurance": ..., "management_fee": ...,
                      "asset_management_fee": ..., "gna": ...}
    """
    __tablename__ = "financial_periods"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    property_id: Mapped[str] = mapped_column(ForeignKey("properties.id"), index=True)
    period: Mapped[date] = mapped_column(Date, index=True)  # first of month
    line_items: Mapped[dict] = mapped_column(JSON)
    occupancy_pct: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String, default="manual_upload")
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now)
    property: Mapped[Property] = relationship(back_populates="financial_periods")


# ── Test results, alerts, audit ────────────────────────────────────────────
class TestOutcome(str, enum.Enum):
    PASS = "pass"
    WARN = "warn"                 # inside buffer of threshold
    BREACH_PROJECTED = "breach_projected"
    BREACH = "breach"


class CovenantTestResult(Base):
    __tablename__ = "covenant_test_results"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    covenant_term_id: Mapped[str] = mapped_column(
        ForeignKey("covenant_terms.id"), index=True)
    tested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now)
    as_of_period: Mapped[date] = mapped_column(Date)
    computed_value: Mapped[float] = mapped_column(Float)
    threshold: Mapped[float] = mapped_column(Float)
    headroom: Mapped[float] = mapped_column(Float)
    outcome: Mapped[TestOutcome] = mapped_column(Enum(TestOutcome))
    projected_breach_period: Mapped[date | None] = mapped_column(Date)
    input_vector: Mapped[dict] = mapped_column(JSON)  # full reconstructable inputs


class AlertSeverity(str, enum.Enum):
    INFO = "info"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    severity: Mapped[AlertSeverity] = mapped_column(Enum(AlertSeverity))
    category: Mapped[str] = mapped_column(String)  # covenant|deadline|data_freshness
    title: Mapped[str] = mapped_column(String)
    body: Mapped[str] = mapped_column(Text)
    related_loan_id: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now)
    acknowledged_by: Mapped[str | None] = mapped_column(String)
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True))
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)


class AuditLog(Base):
    """Append-only. No update/delete paths exist in the API by design."""
    __tablename__ = "audit_log"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    tenant_id: Mapped[str | None] = mapped_column(String, index=True)
    actor: Mapped[str] = mapped_column(String)      # user id or "system"
    action: Mapped[str] = mapped_column(String)     # e.g. covenant_test_run
    detail: Mapped[dict] = mapped_column(JSON)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
