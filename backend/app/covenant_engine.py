"""Covenant engine — the core IP of CovenantSentinel.

Computes lender-defined NOI, debt service, and covenant tests, and projects
time-to-breach from headroom trend. Every result returns its full input
vector so any number shown to a user is reconstructable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from dateutil.relativedelta import relativedelta

from .models import (CovenantTerm, CovenantType, FinancialPeriod, Loan,
                     RateType, TestOutcome)

WARN_BUFFER = 0.05      # within 5% of threshold => WARN
PROJECTION_LOOKBACK = 6  # months of headroom trend used for projection
PROJECTION_HORIZON = 12  # only report projected breach within N months

INCOME_CATEGORIES = {"gross_potential_rent", "other_income"}
CONTRA_INCOME = {"vacancy_loss", "concessions", "bad_debt"}


@dataclass
class NOIResult:
    noi_annual: float
    egi_annual: float
    expenses_annual: float
    adjustments: dict = field(default_factory=dict)
    periods_used: list[str] = field(default_factory=list)


@dataclass
class TestComputation:
    computed_value: float
    threshold: float
    headroom: float
    outcome: TestOutcome
    projected_breach_period: date | None
    input_vector: dict


# ── NOI per the loan document's definition ─────────────────────────────────
def compute_noi(periods: list[FinancialPeriod], noi_def: dict) -> NOIResult:
    """Compute annual NOI per a loan-specific definition.

    Supports: trailing_12 | annualized_3 | annualized_1 bases,
    category exclusions, lender-imposed replacement reserves per unit
    (requires 'units' in noi_def context via _units key), and a
    management-fee floor as % of EGI.
    """
    basis = noi_def.get("basis", "trailing_12")
    n = {"trailing_12": 12, "annualized_3": 3, "annualized_1": 1}[basis]
    periods = sorted(periods, key=lambda p: p.period)[-n:]
    if len(periods) < n:
        raise ValueError(
            f"Need {n} months for basis '{basis}', have {len(periods)}")
    factor = 12 / n

    exclude = set(noi_def.get("exclude_categories", []))
    egi = expenses = mgmt_fee_actual = 0.0
    for p in periods:
        for cat, amt in p.line_items.items():
            if cat in INCOME_CATEGORIES:
                egi += amt
            elif cat in CONTRA_INCOME:
                egi -= amt
            elif cat in exclude:
                continue
            else:
                expenses += amt
                if cat == "management_fee":
                    mgmt_fee_actual += amt
    egi *= factor
    expenses *= factor
    mgmt_fee_actual *= factor

    adjustments: dict = {"basis": basis, "excluded_categories": sorted(exclude)}

    floor_pct = noi_def.get("management_fee_floor_pct")
    if floor_pct:
        floor_amt = egi * floor_pct
        if mgmt_fee_actual < floor_amt:
            bump = floor_amt - mgmt_fee_actual
            expenses += bump
            adjustments["management_fee_floor_addback"] = round(bump, 2)

    rr_per_unit = noi_def.get("replacement_reserve_per_unit")
    units = noi_def.get("_units", 0)
    if rr_per_unit and units:
        rr = rr_per_unit * units
        expenses += rr
        adjustments["replacement_reserve_imposed"] = round(rr, 2)

    return NOIResult(
        noi_annual=round(egi - expenses, 2),
        egi_annual=round(egi, 2),
        expenses_annual=round(expenses, 2),
        adjustments=adjustments,
        periods_used=[p.period.isoformat() for p in periods],
    )


# ── Debt service ───────────────────────────────────────────────────────────
def annual_debt_service(loan: Loan, as_of: date,
                        index_rates: dict[str, float] | None = None) -> dict:
    """Annual debt service with IO/amortizing and fixed/floating support."""
    if loan.rate_type == RateType.FIXED:
        rate = loan.fixed_rate
        rate_source = "fixed"
    else:
        idx = (index_rates or {}).get(loan.index_name)
        if idx is None:
            raise ValueError(f"Missing index rate for {loan.index_name}")
        rate = idx + loan.floating_spread
        rate_source = f"{loan.index_name}={idx:.4f} + spread={loan.floating_spread:.4f}"

    interest_only = loan.io_period_end is not None and as_of <= loan.io_period_end
    if interest_only or not loan.amortization_months:
        annual = loan.current_balance * rate
        method = "interest_only"
    else:
        r = rate / 12
        nper = loan.amortization_months
        pmt = loan.current_balance * (r * (1 + r) ** nper) / ((1 + r) ** nper - 1)
        annual = pmt * 12
        method = f"amortizing_{nper}mo"
    return {"annual_debt_service": round(annual, 2), "rate": round(rate, 6),
            "rate_source": rate_source, "method": method,
            "balance": loan.current_balance}


# ── Tests ──────────────────────────────────────────────────────────────────
def _outcome(value: float, threshold: float, direction: str) -> TestOutcome:
    if direction == "min":
        if value < threshold:
            return TestOutcome.BREACH
        if value < threshold * (1 + WARN_BUFFER):
            return TestOutcome.WARN
    else:  # max
        if value > threshold:
            return TestOutcome.BREACH
        if value > threshold * (1 - WARN_BUFFER):
            return TestOutcome.WARN
    return TestOutcome.PASS


def project_breach(history: list[tuple[date, float]], threshold: float,
                   direction: str) -> date | None:
    """Linear trend on recent computed values → projected breach month."""
    pts = sorted(history)[-PROJECTION_LOOKBACK:]
    if len(pts) < 3:
        return None
    xs = list(range(len(pts)))
    ys = [v for _, v in pts]
    n = len(xs)
    xbar, ybar = sum(xs) / n, sum(ys) / n
    denom = sum((x - xbar) ** 2 for x in xs)
    if denom == 0:
        return None
    slope = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys)) / denom
    heading_down = direction == "min" and slope < 0
    heading_up = direction == "max" and slope > 0
    if not (heading_down or heading_up):
        return None
    latest = ys[-1]
    months = (threshold - latest) / slope
    if months <= 0 or months > PROJECTION_HORIZON:
        return None
    return pts[-1][0] + relativedelta(months=round(months))


def run_test(term: CovenantTerm, loan: Loan, periods: list[FinancialPeriod],
             as_of: date, units: int,
             index_rates: dict[str, float] | None = None,
             property_value: float | None = None,
             history: list[tuple[date, float]] | None = None
             ) -> TestComputation:
    noi_def = dict(term.noi_definition or {})
    noi_def["_units"] = units
    iv: dict = {"loan_id": loan.id, "covenant_term_id": term.id,
                "as_of": as_of.isoformat()}

    if term.covenant_type in (CovenantType.DSCR, CovenantType.DEBT_YIELD):
        noi = compute_noi(periods, noi_def)
        iv["noi"] = noi.__dict__
        if term.covenant_type == CovenantType.DSCR:
            ds = annual_debt_service(loan, as_of, index_rates)
            iv["debt_service"] = ds
            value = noi.noi_annual / ds["annual_debt_service"]
        else:
            value = noi.noi_annual / loan.current_balance
    elif term.covenant_type == CovenantType.LTV:
        if not property_value:
            raise ValueError("LTV test requires property_value")
        iv["property_value"] = property_value
        value = loan.current_balance / property_value
    elif term.covenant_type == CovenantType.OCCUPANCY:
        latest = sorted(periods, key=lambda p: p.period)[-1]
        iv["occupancy_period"] = latest.period.isoformat()
        value = latest.occupancy_pct or 0.0
    else:
        raise NotImplementedError(term.covenant_type)

    value = round(value, 4)
    outcome = _outcome(value, term.threshold, term.direction)
    projected = None
    if outcome in (TestOutcome.PASS, TestOutcome.WARN):
        projected = project_breach((history or []) + [(as_of, value)],
                                   term.threshold, term.direction)
        if projected:
            outcome = TestOutcome.BREACH_PROJECTED
    headroom = round(value - term.threshold if term.direction == "min"
                     else term.threshold - value, 4)
    return TestComputation(value, term.threshold, headroom, outcome,
                           projected, iv)
