"""CovenantSentinel API.

Endpoints are grouped exactly as n8n consumes them:
  W1 → POST /financials/ingest
  W2 → GET  /monitoring/upcoming
  W5 → POST /covenants/run-tests  (emits alert payloads n8n escalates)
Plus alert acknowledgment and audit query.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from dateutil.relativedelta import relativedelta
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from . import covenant_engine as engine_mod
from .extraction import pipeline as extraction_pipeline
from .models import (Alert, AlertSeverity, AuditLog, Base, CovenantTerm,
                     CovenantTestResult, CovenantType, FinancialPeriod, Loan,
                     Property, RateType, Tenant, TermStatus, TestOutcome)

DATABASE_URL = "sqlite:///./covenantsentinel.db"  # prod: Postgres via env
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base.metadata.create_all(engine)

app = FastAPI(title="CovenantSentinel API", version="0.1.0")


def db() -> Session:
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def audit(s: Session, actor: str, action: str, detail: dict,
          tenant_id: str | None = None) -> None:
    s.add(AuditLog(actor=actor, action=action, detail=detail,
                   tenant_id=tenant_id))


# ── W1: financials ingestion ───────────────────────────────────────────────
class FinancialsIn(BaseModel):
    property_id: str
    period: date
    line_items: dict[str, float]
    occupancy_pct: float | None = None
    source: str = "email_ingest"


@app.post("/financials/ingest")
def ingest_financials(payload: FinancialsIn, s: Session = Depends(db)):
    prop = s.get(Property, payload.property_id)
    if not prop:
        raise HTTPException(404, "Unknown property")
    fp = FinancialPeriod(property_id=prop.id, period=payload.period,
                         line_items=payload.line_items,
                         occupancy_pct=payload.occupancy_pct,
                         source=payload.source)
    s.add(fp)
    audit(s, "system", "financials_ingested",
          {"property": prop.name, "period": str(payload.period),
           "source": payload.source}, prop.tenant_id)
    s.commit()
    return {"status": "ok", "financial_period_id": fp.id}


# ── Loan document extraction (Claude-powered) ──────────────────────────────
class ExtractLoanIn(BaseModel):
    property_id: str
    document_text: str
    source_document: str


@app.post("/loans/extract")
def extract_loan(payload: ExtractLoanIn, s: Session = Depends(db)):
    prop = s.get(Property, payload.property_id)
    if not prop:
        raise HTTPException(404, "Unknown property")

    try:
        extraction = extraction_pipeline.extract_loan_document(
            payload.document_text)
    except RuntimeError as e:
        raise HTTPException(422, str(e))

    loan_data = extraction["loan"]
    rate_cap = loan_data["rate_cap_requirement"]
    ext_options = loan_data["extension_options"]

    loan = Loan(
        property_id=prop.id,
        lender=loan_data["lender"],
        original_balance=loan_data["original_balance"],
        current_balance=loan_data["original_balance"],
        rate_type=RateType(loan_data["rate_type"]),
        fixed_rate=loan_data["fixed_rate"],
        floating_spread=loan_data["floating_spread"],
        index_name=loan_data["index_name"],
        io_period_end=(date.fromisoformat(loan_data["io_period_end"])
                       if loan_data["io_period_end"] else None),
        amortization_months=loan_data["amortization_months"],
        origination_date=date.fromisoformat(loan_data["origination_date"]),
        maturity_date=date.fromisoformat(loan_data["maturity_date"]),
        rate_cap_expiry=(date.fromisoformat(rate_cap["expiry"])
                         if rate_cap.get("expiry") else None),
        extension_test_deadline=(
            date.fromisoformat(loan_data["maturity_date"]) - relativedelta(
                days=ext_options[0]["test_deadline_days_before_maturity"])
            if ext_options else None),
    )
    s.add(loan)
    s.flush()

    terms = []
    for cov in extraction["covenants"]:
        noi_def = cov["noi_definition"] or {}
        term = CovenantTerm(
            loan_id=loan.id,
            covenant_type=CovenantType(cov["covenant_type"]),
            threshold=cov["threshold"],
            direction=cov["direction"],
            test_frequency=cov["test_frequency"],
            noi_definition={k: v for k, v in noi_def.items()
                           if v is not None},
            cure_provision=cov["cure_provision"],
            trigger_consequence=cov["trigger_consequence"],
            source_document=payload.source_document,
            source_pages=cov["source_pages"],
            status=TermStatus.PENDING_VERIFICATION,
        )
        s.add(term)
        terms.append(term)
    s.flush()

    audit(s, "system", "loan_extracted",
          {"property": prop.name, "source_document": payload.source_document,
           "covenant_count": len(terms)}, prop.tenant_id)
    s.commit()

    return {
        "loan_id": loan.id,
        "covenant_term_ids": [t.id for t in terms],
        "reporting_deliverables": extraction["reporting_deliverables"],
        "extraction": extraction,
    }


# ── W5: covenant test run ──────────────────────────────────────────────────
class RunTestsIn(BaseModel):
    tenant_id: str
    as_of: date
    index_rates: dict[str, float] = Field(default_factory=dict)


@app.post("/covenants/run-tests")
def run_tests(payload: RunTestsIn, s: Session = Depends(db)):
    results, alerts_out = [], []
    terms = s.execute(
        select(CovenantTerm, Loan, Property)
        .join(Loan, CovenantTerm.loan_id == Loan.id)
        .join(Property, Loan.property_id == Property.id)
        .where(Property.tenant_id == payload.tenant_id,
               CovenantTerm.status == TermStatus.ACTIVE)
    ).all()

    for term, loan, prop in terms:
        periods = list(s.execute(
            select(FinancialPeriod)
            .where(FinancialPeriod.property_id == prop.id,
                   FinancialPeriod.period <= payload.as_of)
        ).scalars())
        history = [(r.as_of_period, r.computed_value) for r in s.execute(
            select(CovenantTestResult)
            .where(CovenantTestResult.covenant_term_id == term.id)
            .order_by(CovenantTestResult.as_of_period)
        ).scalars()]
        try:
            comp = engine_mod.run_test(
                term, loan, periods, payload.as_of, prop.units,
                index_rates=payload.index_rates, history=history)
        except ValueError as e:
            alerts_out.append(_make_alert(
                s, prop.tenant_id, AlertSeverity.MEDIUM, "data_freshness",
                f"Cannot test {term.covenant_type.value.upper()} — {prop.name}",
                str(e), loan.id))
            continue

        s.add(CovenantTestResult(
            covenant_term_id=term.id, as_of_period=payload.as_of,
            computed_value=comp.computed_value, threshold=comp.threshold,
            headroom=comp.headroom, outcome=comp.outcome,
            projected_breach_period=comp.projected_breach_period,
            input_vector=comp.input_vector))
        audit(s, "system", "covenant_test_run",
              {"loan": loan.lender, "property": prop.name,
               "type": term.covenant_type.value, "value": comp.computed_value,
               "outcome": comp.outcome.value}, prop.tenant_id)

        sev = {TestOutcome.BREACH: AlertSeverity.CRITICAL,
               TestOutcome.BREACH_PROJECTED: AlertSeverity.HIGH,
               TestOutcome.WARN: AlertSeverity.MEDIUM}.get(comp.outcome)
        if sev:
            proj = (f" Projected breach ~{comp.projected_breach_period}."
                    if comp.projected_breach_period else "")
            alerts_out.append(_make_alert(
                s, prop.tenant_id, sev, "covenant",
                f"{term.covenant_type.value.upper()} {comp.outcome.value} — "
                f"{prop.name} ({loan.lender})",
                f"Computed {comp.computed_value} vs covenant {comp.threshold} "
                f"(headroom {comp.headroom:+}).{proj}", loan.id))

        results.append({
            "property": prop.name, "lender": loan.lender,
            "covenant": term.covenant_type.value,
            "value": comp.computed_value, "threshold": comp.threshold,
            "headroom": comp.headroom, "outcome": comp.outcome.value,
            "projected_breach": (str(comp.projected_breach_period)
                                 if comp.projected_breach_period else None)})
    s.commit()
    return {"results": results, "alerts": alerts_out}


def _make_alert(s: Session, tenant_id: str, sev: AlertSeverity, category: str,
                title: str, body: str, loan_id: str | None) -> dict:
    a = Alert(tenant_id=tenant_id, severity=sev, category=category,
              title=title, body=body, related_loan_id=loan_id)
    s.add(a)
    s.flush()
    return {"alert_id": a.id, "severity": sev.value, "title": title,
            "body": body}


# ── W2: monitoring feed (deadline clock) ───────────────────────────────────
@app.get("/monitoring/upcoming")
def upcoming(tenant_id: str, horizon_days: int = 180,
             s: Session = Depends(db)):
    today = date.today()
    limit = today + relativedelta(days=horizon_days)
    items = []
    rows = s.execute(
        select(Loan, Property).join(Property)
        .where(Property.tenant_id == tenant_id)).all()
    for loan, prop in rows:
        for label, d in (("maturity", loan.maturity_date),
                         ("rate_cap_expiry", loan.rate_cap_expiry),
                         ("extension_test_deadline",
                          loan.extension_test_deadline)):
            if d and today <= d <= limit:
                items.append({"property": prop.name, "lender": loan.lender,
                              "event": label, "date": str(d),
                              "days_remaining": (d - today).days})
        latest = s.execute(
            select(FinancialPeriod.period)
            .where(FinancialPeriod.property_id == prop.id)
            .order_by(FinancialPeriod.period.desc()).limit(1)
        ).scalar()
        stale_days = ((today - latest).days - 31) if latest else 9999
        if stale_days > 14:  # >45 days since period start ⇒ stale
            items.append({"property": prop.name, "event": "financials_stale",
                          "last_period": str(latest) if latest else None,
                          "days_overdue": stale_days})
    return {"as_of": str(today), "items": sorted(
        items, key=lambda i: i.get("days_remaining", -1))}


# ── Alert acknowledgment (W5 loop) & audit ─────────────────────────────────
@app.post("/alerts/{alert_id}/acknowledge")
def acknowledge(alert_id: str, user: str, s: Session = Depends(db)):
    a = s.get(Alert, alert_id)
    if not a:
        raise HTTPException(404)
    a.acknowledged_by, a.acknowledged_at = user, datetime.now(timezone.utc)
    audit(s, user, "alert_acknowledged", {"alert_id": alert_id}, a.tenant_id)
    s.commit()
    return {"status": "acknowledged"}


@app.get("/audit")
def audit_query(tenant_id: str, limit: int = 100, s: Session = Depends(db)):
    rows = s.execute(select(AuditLog).where(AuditLog.tenant_id == tenant_id)
                     .order_by(AuditLog.at.desc()).limit(limit)).scalars()
    return [{"at": str(r.at), "actor": r.actor, "action": r.action,
             "detail": r.detail} for r in rows]
