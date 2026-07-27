"""Seed a realistic demo portfolio and run the covenant engine end-to-end.

Portfolio: 'Pacific Crest Capital' — 3 multifamily assets, 3 loans:
  1. Sunset Ridge (Freddie fixed, DSCR 1.25x quarterly, trailing-12 NOI)
  2. Marina Vista (bridge, SOFR+325 floating, DSCR 1.10x monthly on
     annualized T-3, lender-imposed $250/unit reserves — expenses trending up)
  3. Cedar Grove  (bank loan, debt yield 8.0% + occupancy 85%)

Marina Vista is engineered to show shrinking headroom → projected breach.
Run:  python seed_demo.py
"""
from __future__ import annotations

import json
from datetime import date

from dateutil.relativedelta import relativedelta
from fastapi.testclient import TestClient

from app.main import SessionLocal, app
from app.models import (CovenantTerm, CovenantType, FinancialPeriod, Loan,
                        Property, RateType, Tenant, TermStatus)

client = TestClient(app)


def month(i: int) -> date:  # i months before Jul 2026
    return date(2026, 7, 1) - relativedelta(months=i)


def seed() -> str:
    s = SessionLocal()
    t = Tenant(name="Pacific Crest Capital")
    s.add(t); s.flush()

    p1 = Property(tenant_id=t.id, name="Sunset Ridge Apartments",
                  entity_name="PCC Sunset LLC", units=180)
    p2 = Property(tenant_id=t.id, name="Marina Vista",
                  entity_name="PCC Marina LLC", units=120)
    p3 = Property(tenant_id=t.id, name="Cedar Grove",
                  entity_name="PCC Cedar LLC", units=96)
    s.add_all([p1, p2, p3]); s.flush()

    l1 = Loan(property_id=p1.id, lender="Freddie Mac (Berkadia)",
              original_balance=24_000_000, current_balance=23_400_000,
              rate_type=RateType.FIXED, fixed_rate=0.0485,
              amortization_months=360, origination_date=date(2022, 3, 1),
              maturity_date=date(2032, 3, 1))
    l2 = Loan(property_id=p2.id, lender="Ridgeline Debt Fund (Bridge)",
              original_balance=18_500_000, current_balance=18_500_000,
              rate_type=RateType.FLOATING, floating_spread=0.0325,
              index_name="SOFR_1M", io_period_end=date(2027, 6, 1),
              origination_date=date(2024, 6, 1),
              maturity_date=date(2027, 6, 1),
              rate_cap_expiry=date(2026, 12, 1),
              extension_test_deadline=date(2027, 3, 1))
    l3 = Loan(property_id=p3.id, lender="First Coastal Bank",
              original_balance=9_800_000, current_balance=9_350_000,
              rate_type=RateType.FIXED, fixed_rate=0.0560,
              amortization_months=300, origination_date=date(2021, 9, 1),
              maturity_date=date(2028, 9, 1))
    s.add_all([l1, l2, l3]); s.flush()

    terms = [
        CovenantTerm(loan_id=l1.id, covenant_type=CovenantType.DSCR,
                     threshold=1.25, test_frequency="quarterly",
                     noi_definition={"basis": "trailing_12",
                                     "management_fee_floor_pct": 0.03},
                     trigger_consequence="Cash management sweep until 2 "
                     "consecutive passing quarters",
                     source_document="Sunset_LoanAgmt.pdf",
                     source_pages="pp. 41-44"),
        CovenantTerm(loan_id=l2.id, covenant_type=CovenantType.DSCR,
                     threshold=1.10, test_frequency="monthly",
                     noi_definition={"basis": "annualized_3",
                                     "exclude_categories":
                                         ["asset_management_fee"],
                                     "replacement_reserve_per_unit": 250},
                     cure_provision="Cure by principal paydown to restore "
                     "compliance within 15 business days",
                     trigger_consequence="Default interest +400bps; "
                     "distributions blocked",
                     source_document="Marina_BridgeAgmt.pdf",
                     source_pages="pp. 55-58, 102"),
        CovenantTerm(loan_id=l3.id, covenant_type=CovenantType.DEBT_YIELD,
                     threshold=0.08, test_frequency="quarterly",
                     noi_definition={"basis": "trailing_12"},
                     source_document="Cedar_LoanAgmt.pdf",
                     source_pages="pp. 37-39"),
        CovenantTerm(loan_id=l3.id, covenant_type=CovenantType.OCCUPANCY,
                     threshold=0.85, test_frequency="monthly",
                     source_document="Cedar_LoanAgmt.pdf",
                     source_pages="p. 40"),
    ]
    for tm in terms:  # all human-verified in this demo
        tm.status = TermStatus.ACTIVE
        tm.verified_by, tm.verified_at = "controller@pcc.com", None
    s.add_all(terms)

    # 14 months of financials. Marina Vista: taxes reassessed + insurance
    # climbing ⇒ expenses drift up ~1.8%/mo while income is flat.
    for i in range(14, 0, -1):
        per = month(i)
        drift = (14 - i)
        s.add(FinancialPeriod(property_id=p1.id, period=per, line_items={
            "gross_potential_rent": 310_000, "vacancy_loss": 15_500,
            "other_income": 12_000, "repairs_maintenance": 28_000,
            "payroll": 34_000, "utilities": 21_000, "taxes": 31_000,
            "insurance": 14_000, "management_fee": 8_500, "gna": 6_000},
            occupancy_pct=0.95))
        s.add(FinancialPeriod(property_id=p2.id, period=per, line_items={
            "gross_potential_rent": 205_000, "vacancy_loss": 12_300,
            "other_income": 7_000,
            "repairs_maintenance": 19_000 * (1 + 0.018 * drift),
            "payroll": 24_000, "utilities": 15_000,
            "taxes": 26_000 * (1 + 0.022 * drift),
            "insurance": 11_000 * (1 + 0.025 * drift),
            "management_fee": 6_400, "asset_management_fee": 4_000,
            "gna": 4_500}, occupancy_pct=0.93))
        s.add(FinancialPeriod(property_id=p3.id, period=per, line_items={
            "gross_potential_rent": 138_000, "vacancy_loss": 9_600,
            "other_income": 4_200, "repairs_maintenance": 13_500,
            "payroll": 15_000, "utilities": 9_800, "taxes": 13_200,
            "insurance": 6_900, "management_fee": 4_000, "gna": 2_800},
            occupancy_pct=0.90 - 0.002 * drift))
    s.commit()
    tid = t.id
    s.close()
    return tid


def main() -> None:
    tenant_id = seed()
    print("=" * 72)
    print("COVENANT TEST RUN  —  Pacific Crest Capital  —  as of 2026-06-01")
    print("=" * 72)
    # Backfill monthly runs so trend projection has history
    for i in range(6, 0, -1):
        r = client.post("/covenants/run-tests", json={
            "tenant_id": tenant_id, "as_of": str(month(i)),
            "index_rates": {"SOFR_1M": 0.0430}})
        r.raise_for_status()
    final = r.json()
    for row in final["results"]:
        proj = f"  → projected breach {row['projected_breach']}" \
            if row["projected_breach"] else ""
        print(f"[{row['outcome'].upper():>16}] {row['property']:<24} "
              f"{row['covenant']:>10}: {row['value']}"
              f" vs {row['threshold']} (headroom {row['headroom']:+}){proj}")
    print("\nALERTS EMITTED (would fan out via n8n W5):")
    for a in final["alerts"]:
        print(f"  [{a['severity'].upper()}] {a['title']}\n"
              f"      {a['body']}")
    print("\nDEADLINE CLOCK (n8n W2 feed, 240-day horizon):")
    up = client.get("/monitoring/upcoming",
                    params={"tenant_id": tenant_id,
                            "horizon_days": 240}).json()
    for item in up["items"]:
        print(f"  {json.dumps(item)}")


if __name__ == "__main__":
    main()
