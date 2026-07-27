"""Live Claude extraction pipeline for loan documents.

Chunks long documents, extracts loan/covenant terms per chunk via
structured outputs (guarantees schema-conforming JSON, no manual parsing),
and reconciles multi-chunk results into one extraction. Output is always
advisory — CovenantTerm rows land PENDING_VERIFICATION regardless of
extraction confidence; nothing here activates a covenant.
"""
from __future__ import annotations

import json

import anthropic

from .prompts import EXTRACTION_SYSTEM_PROMPT, build_extraction_messages

MODEL = "claude-opus-5"
CHUNK_SIZE = 15_000
CHUNK_OVERLAP = 1_000

_client = anthropic.Anthropic()

_RATE_CAP_REQUIREMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "required": {"type": "boolean"},
        "strike": {"type": ["number", "null"]},
        "expiry": {"type": ["string", "null"]},
        "replacement_deadline_days": {"type": ["integer", "null"]},
        "source_pages": {"type": "string"},
    },
    "required": ["required", "strike", "expiry", "replacement_deadline_days",
                 "source_pages"],
    "additionalProperties": False,
}

_EXTENSION_OPTION_SCHEMA = {
    "type": "object",
    "properties": {
        "conditions": {"type": "string"},
        "test_deadline_days_before_maturity": {"type": "integer"},
        "source_pages": {"type": "string"},
    },
    "required": ["conditions", "test_deadline_days_before_maturity",
                 "source_pages"],
    "additionalProperties": False,
}

_NOI_DEFINITION_SCHEMA = {
    "type": "object",
    "properties": {
        "basis": {"type": "string",
                  "enum": ["trailing_12", "annualized_3", "annualized_1"]},
        "exclude_categories": {"type": "array", "items": {"type": "string"}},
        "replacement_reserve_per_unit": {"type": ["number", "null"]},
        "management_fee_floor_pct": {"type": ["number", "null"]},
    },
    "required": ["basis", "exclude_categories",
                 "replacement_reserve_per_unit", "management_fee_floor_pct"],
    "additionalProperties": False,
}

_COVENANT_SCHEMA = {
    "type": "object",
    "properties": {
        "covenant_type": {"type": "string",
                          "enum": ["dscr", "debt_yield", "ltv", "occupancy",
                                    "liquidity"]},
        "threshold": {"type": "number"},
        "direction": {"type": "string", "enum": ["min", "max"]},
        "test_frequency": {"type": "string",
                           "enum": ["monthly", "quarterly", "annual"]},
        "noi_definition": _NOI_DEFINITION_SCHEMA,
        "cure_provision": {"type": ["string", "null"]},
        "trigger_consequence": {"type": ["string", "null"]},
        "source_pages": {"type": "string"},
        "source_quote_summary": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "notes": {"type": ["string", "null"]},
    },
    "required": ["covenant_type", "threshold", "direction", "test_frequency",
                 "noi_definition", "cure_provision", "trigger_consequence",
                 "source_pages", "source_quote_summary", "confidence",
                 "notes"],
    "additionalProperties": False,
}

_REPORTING_DELIVERABLE_SCHEMA = {
    "type": "object",
    "properties": {
        "item": {"type": "string"},
        "frequency": {"type": "string"},
        "due_days_after_period_end": {"type": "integer"},
        "source_pages": {"type": "string"},
    },
    "required": ["item", "frequency", "due_days_after_period_end",
                 "source_pages"],
    "additionalProperties": False,
}

_LOAN_SCHEMA = {
    "type": "object",
    "properties": {
        "lender": {"type": "string"},
        "original_balance": {"type": "number"},
        "rate_type": {"type": "string", "enum": ["fixed", "floating"]},
        "fixed_rate": {"type": ["number", "null"]},
        "floating_spread": {"type": ["number", "null"]},
        "index_name": {"type": ["string", "null"]},
        "io_period_end": {"type": ["string", "null"]},
        "amortization_months": {"type": ["integer", "null"]},
        "origination_date": {"type": "string"},
        "maturity_date": {"type": "string"},
        "rate_cap_requirement": _RATE_CAP_REQUIREMENT_SCHEMA,
        "extension_options": {"type": "array",
                              "items": _EXTENSION_OPTION_SCHEMA},
    },
    "required": ["lender", "original_balance", "rate_type", "fixed_rate",
                 "floating_spread", "index_name", "io_period_end",
                 "amortization_months", "origination_date", "maturity_date",
                 "rate_cap_requirement", "extension_options"],
    "additionalProperties": False,
}

EXTRACTION_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "loan": _LOAN_SCHEMA,
        "covenants": {"type": "array", "items": _COVENANT_SCHEMA},
        "reporting_deliverables": {"type": "array",
                                   "items": _REPORTING_DELIVERABLE_SCHEMA},
    },
    "required": ["loan", "covenants", "reporting_deliverables"],
    "additionalProperties": False,
}

_MERGE_SYSTEM_PROMPT = """You are reconciling multiple partial extractions \
of the SAME loan document, each produced from a different chunk of the \
text. Merge them into a single extraction:
- Loan-level fields: keep the most complete non-null value across chunks;
  if chunks disagree on a stated value, prefer the one with more specific
  source_pages.
- Covenants: deduplicate by covenant_type. If the same covenant_type
  appears in multiple chunks with different thresholds, keep both as
  separate entries, set confidence to "low", and explain the conflict in
  notes so a human can resolve it.
- Reporting deliverables: deduplicate by item + frequency.
Return ONLY valid JSON conforming to the provided schema. No prose."""


def chunk_document(text: str, chunk_size: int = CHUNK_SIZE,
                   overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split long document text into overlapping chunks."""
    if len(text) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = end - overlap
    return chunks


def _extract_json(text_chunk: str) -> dict:
    response = _client.messages.create(
        model=MODEL,
        max_tokens=8000,
        system=EXTRACTION_SYSTEM_PROMPT,
        output_config={"format": {"type": "json_schema",
                                  "schema": EXTRACTION_JSON_SCHEMA}},
        messages=build_extraction_messages([text_chunk]),
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Extraction refused by model safety classifiers")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def _merge_chunk_extractions(chunk_results: list[dict]) -> dict:
    if len(chunk_results) == 1:
        return chunk_results[0]
    response = _client.messages.create(
        model=MODEL,
        max_tokens=8000,
        system=_MERGE_SYSTEM_PROMPT,
        output_config={"format": {"type": "json_schema",
                                  "schema": EXTRACTION_JSON_SCHEMA}},
        messages=[{
            "role": "user",
            "content": ("Reconcile these partial extractions into one:\n\n"
                       + json.dumps(chunk_results, indent=1)),
        }],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Merge refused by model safety classifiers")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def extract_loan_document(document_text: str) -> dict:
    """Run the full extraction pipeline on a loan document's text.

    Chunks the document if long, extracts each chunk independently via
    structured outputs, then reconciles multi-chunk results into a single
    schema-conforming dict matching EXTRACTION_JSON_SCHEMA.
    """
    chunks = chunk_document(document_text)
    chunk_results = [_extract_json(c) for c in chunks]
    return _merge_chunk_extractions(chunk_results)
