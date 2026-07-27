"""Loan-document extraction: prompt + schema for Claude API.

Every extracted field MUST carry source page citations. Anything the model
cannot ground in the document is returned as null with a note — never guessed.
Extractions land as PENDING_VERIFICATION and require human sign-off.
"""

EXTRACTION_SYSTEM_PROMPT = """You are a commercial real estate loan-document \
analyst. Extract covenant and structural terms from the loan document with \
absolute fidelity to the text. Rules:

1. NEVER infer or assume a value that is not stated. If a term is absent or \
ambiguous, return null and explain in `notes`.
2. EVERY extracted field must include `source_pages` (the PDF page numbers \
where the term is defined) and `source_quote_summary` (a paraphrase under 15 \
words of the defining language — do not quote verbatim).
3. Pay special attention to how the document defines Net Operating Income for \
covenant testing: exclusions (e.g., asset management fees), imposed \
replacement reserves per unit, management fee floors as % of EGI, and the \
measurement basis (trailing 12 months vs annualized recent months).
4. Distinguish covenant TESTS (measured on test dates with consequences) from \
mere reporting definitions.
5. Capture consequences precisely: cash sweep triggers, default interest \
rates, cure rights and cure periods, extension conditions.

Return ONLY valid JSON conforming to the provided schema. No prose."""

EXTRACTION_SCHEMA = {
    "loan": {
        "lender": "string", "original_balance": "number",
        "rate_type": "fixed|floating",
        "fixed_rate": "number|null", "floating_spread": "number|null",
        "index_name": "string|null", "io_period_end": "date|null",
        "amortization_months": "int|null",
        "origination_date": "date", "maturity_date": "date",
        "rate_cap_requirement": {
            "required": "bool", "strike": "number|null",
            "expiry": "date|null", "replacement_deadline_days": "int|null",
            "source_pages": "string"},
        "extension_options": [{
            "conditions": "string", "test_deadline_days_before_maturity": "int",
            "source_pages": "string"}],
    },
    "covenants": [{
        "covenant_type": "dscr|debt_yield|ltv|occupancy|liquidity",
        "threshold": "number", "direction": "min|max",
        "test_frequency": "monthly|quarterly|annual",
        "noi_definition": {
            "basis": "trailing_12|annualized_3|annualized_1",
            "exclude_categories": ["string"],
            "replacement_reserve_per_unit": "number|null",
            "management_fee_floor_pct": "number|null"},
        "cure_provision": "string|null",
        "trigger_consequence": "string|null",
        "source_pages": "string",
        "source_quote_summary": "string",
        "confidence": "high|medium|low",
        "notes": "string|null"}],
    "reporting_deliverables": [{
        "item": "string", "frequency": "string",
        "due_days_after_period_end": "int", "source_pages": "string"}],
}


def build_extraction_messages(document_text_chunks: list[str]) -> list[dict]:
    """Assemble messages for a chunked extraction pass.

    For long documents, run per-chunk extraction then a merge pass that
    reconciles duplicates and flags conflicts for human review.
    """
    import json
    return [{
        "role": "user",
        "content": (
            "Extract loan terms and covenants from the following document "
            f"text.\n\nSCHEMA:\n{json.dumps(EXTRACTION_SCHEMA, indent=1)}\n\n"
            "DOCUMENT:\n" + "\n\n".join(document_text_chunks)
        ),
    }]
