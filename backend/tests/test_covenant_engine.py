"""Unit tests for the covenant engine's contractual math."""
from datetime import date

from dateutil.relativedelta import relativedelta

from app.covenant_engine import (annual_debt_service, compute_noi,
                                 project_breach)
from app.models import FinancialPeriod, Loan, RateType


def _periods(n: int, items: dict) -> list[FinancialPeriod]:
    return [FinancialPeriod(property_id="p", line_items=dict(items),
                            period=date(2026, 6, 1) - relativedelta(months=i))
            for i in range(n)]


BASE = {"gross_potential_rent": 100_000, "vacancy_loss": 5_000,
        "other_income": 2_000, "repairs_maintenance": 10_000,
        "payroll": 12_000, "taxes": 9_000, "insurance": 4_000,
        "management_fee": 2_000, "asset_management_fee": 3_000}


def test_noi_trailing_12_basic():
    r = compute_noi(_periods(12, BASE), {"basis": "trailing_12"})
    assert r.egi_annual == (100_000 - 5_000 + 2_000) * 12
    assert r.expenses_annual == (10_000 + 12_000 + 9_000 + 4_000 + 2_000 + 3_000) * 12
    assert r.noi_annual == r.egi_annual - r.expenses_annual


def test_noi_lender_exclusions_and_reserves():
    r = compute_noi(_periods(3, BASE), {
        "basis": "annualized_3",
        "exclude_categories": ["asset_management_fee"],
        "replacement_reserve_per_unit": 250, "_units": 100})
    # AM fee excluded (36k/yr), reserves imposed (25k/yr)
    plain = compute_noi(_periods(3, BASE), {"basis": "annualized_3"})
    assert r.noi_annual == plain.noi_annual + 36_000 - 25_000
    assert r.adjustments["replacement_reserve_imposed"] == 25_000


def test_noi_management_fee_floor():
    low_fee = dict(BASE, management_fee=500)  # below 3% of EGI
    r = compute_noi(_periods(12, low_fee),
                    {"basis": "trailing_12", "management_fee_floor_pct": 0.03})
    egi = (100_000 - 5_000 + 2_000) * 12
    assert abs(r.adjustments["management_fee_floor_addback"]
               - (egi * 0.03 - 500 * 12)) < 0.01


def test_debt_service_io_floating():
    loan = Loan(current_balance=10_000_000, rate_type=RateType.FLOATING,
                floating_spread=0.03, index_name="SOFR_1M",
                io_period_end=date(2027, 1, 1), amortization_months=None)
    ds = annual_debt_service(loan, date(2026, 6, 1), {"SOFR_1M": 0.045})
    assert ds["annual_debt_service"] == 10_000_000 * 0.075
    assert ds["method"] == "interest_only"


def test_debt_service_amortizing_fixed():
    loan = Loan(current_balance=10_000_000, rate_type=RateType.FIXED,
                fixed_rate=0.06, amortization_months=360, io_period_end=None)
    ds = annual_debt_service(loan, date(2026, 6, 1))
    assert abs(ds["annual_debt_service"] - 719_461) < 100  # 30yr @6% ≈ $59,955/mo


def test_projection_declining_min_covenant():
    hist = [(date(2026, 1, 1) + relativedelta(months=i), 1.40 - 0.03 * i)
            for i in range(6)]  # latest 1.25, falling 0.03/mo toward 1.10
    proj = project_breach(hist, threshold=1.10, direction="min")
    assert proj is not None and proj.year == 2026


def test_projection_stable_returns_none():
    hist = [(date(2026, 1, 1) + relativedelta(months=i), 1.30)
            for i in range(6)]
    assert project_breach(hist, 1.10, "min") is None
