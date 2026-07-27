"""Smoke test for the live Claude extraction pipeline.

Seeds a minimal tenant/property, then runs a sample loan document through
POST /loans/extract to verify the Claude call, structured-output parsing,
and DB persistence all work end-to-end against the real API.

Requires ANTHROPIC_API_KEY. Run:  python test_extraction.py
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.main import SessionLocal, app
from app.models import Property, Tenant

client = TestClient(app)

SAMPLE_LOAN_DOCUMENT = """
LOAN AGREEMENT
Lender: First Coastal Bank
Borrower: PCC Test LLC

Section 2.1 Principal and Rate. The original principal balance of the Loan
is $12,500,000.00. The Loan bears interest at a fixed rate of 5.75% per
annum. The Loan is interest-only through origination and amortizes over
360 months thereafter.

Section 2.2 Term. The Loan was originated on January 15, 2024 and matures
on January 15, 2031.

Section 4.3 Debt Service Coverage Ratio. Borrower shall maintain a minimum
Debt Service Coverage Ratio of 1.30x, tested quarterly, based on Net
Operating Income calculated on a trailing twelve (12) month basis. Net
Operating Income shall exclude any Asset Management Fee paid to an
affiliate of Borrower. Failure to maintain the required ratio for two (2)
consecutive test periods shall constitute a Cash Sweep Trigger Event,
whereupon all excess cash flow shall be swept to a lender-controlled
reserve account until such time as Borrower demonstrates two (2)
consecutive quarters of compliance.

Section 4.5 Occupancy Covenant. Borrower shall maintain physical occupancy
of not less than 88% of the Property's rentable units, tested monthly.

Section 6.1 Reporting. Borrower shall deliver monthly operating statements
within twenty (20) days after the end of each calendar month, and annual
audited financial statements within ninety (90) days after fiscal year
end.

(Terms above appear on pp. 12-15, 22, and 31 of the executed agreement.)
"""


def main() -> None:
    s = SessionLocal()
    t = Tenant(name="Extraction Test Tenant")
    s.add(t)
    s.flush()
    p = Property(tenant_id=t.id, name="Test Property", entity_name="Test LLC",
                units=100)
    s.add(p)
    s.commit()
    property_id, tenant_id = p.id, t.id
    s.close()

    print(f"Seeded property_id={property_id}\n")
    print("Calling POST /loans/extract (this hits the real Claude API)...\n")

    r = client.post("/loans/extract", json={
        "property_id": property_id,
        "document_text": SAMPLE_LOAN_DOCUMENT,
        "source_document": "sample_loan_agmt.pdf",
    })
    r.raise_for_status()
    result = r.json()

    print(f"Created loan_id={result['loan_id']}")
    print(f"Created {len(result['covenant_term_ids'])} covenant term(s)\n")
    print("Full extraction (as persisted, pending_verification):")
    print(json.dumps(result["extraction"], indent=2))


if __name__ == "__main__":
    main()
