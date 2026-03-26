from __future__ import annotations

import json
import os

from dotenv import load_dotenv
from typing import Any

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

load_dotenv()

DEFAULT_OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")


def get_openai_client():
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key or OpenAI is None:
        return None
    try:
        return OpenAI(api_key=api_key)
    except Exception:
        return None


def _responses_json_schema(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "format": {
            "type": "json_schema",
            "name": name,
            "schema": schema,
            "strict": True,
        }
    }


def _response_to_json(response) -> dict[str, Any] | None:
    try:
        if getattr(response, "output_text", None):
            return json.loads(response.output_text)
    except Exception:
        pass
    try:
        for item in response.output:
            if getattr(item, "type", None) != "message":
                continue
            for part in getattr(item, "content", []) or []:
                text = getattr(part, "text", None)
                if text:
                    return json.loads(text)
    except Exception:
        pass
    return None


def parse_supplier_profile_text(text: str, filename: str | None = None) -> dict[str, Any] | None:
    client = get_openai_client()
    if not client or not (text or "").strip():
        return None
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["company_name", "industry", "capabilities", "locations", "industry_keywords", "summary", "issues"],
        "properties": {
            "company_name": {"type": "string"},
            "industry": {"type": "string"},
            "capabilities": {"type": "array", "items": {"type": "string"}},
            "locations": {"type": "array", "items": {"type": "string"}},
            "industry_keywords": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": "string"},
            "issues": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["title", "detail", "penalty_weight"], "properties": {"title": {"type": "string"}, "detail": {"type": "string"}, "penalty_weight": {"type": "number"}}}},
        },
    }
    response = client.responses.create(
        model=DEFAULT_OPENAI_MODEL,
        input=[
            {"role": "system", "content": "Extract structured supplier profile data. Be conservative. Do not invent certifications, locations, or capabilities."},
            {"role": "user", "content": f"Filename: {filename or 'unknown'}\n\nDocument text:\n{text[:18000]}"},
        ],
        text=_responses_json_schema("supplier_profile_parse", schema),
    )
    return _response_to_json(response)


def parse_tender_document_text(metadata: dict[str, Any], text: str) -> dict[str, Any] | None:
    client = get_openai_client()
    if not client or not (text or "").strip():
        return None
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["scope_summary", "deliverables", "eligibility_requirements", "compulsory_documents", "evaluation_criteria", "functionality_criteria", "cidb_requirements", "briefing_session", "closing_rules", "contract_duration", "location", "industry_tags", "risk_flags", "confidence"],
        "properties": {
            "scope_summary": {"type": "string"},
            "deliverables": {"type": "array", "items": {"type": "string"}},
            "eligibility_requirements": {"type": "array", "items": {"type": "string"}},
            "compulsory_documents": {"type": "array", "items": {"type": "string"}},
            "evaluation_criteria": {"type": "array", "items": {"type": "string"}},
            "functionality_criteria": {"type": "array", "items": {"type": "string"}},
            "cidb_requirements": {"type": "array", "items": {"type": "string"}},
            "briefing_session": {"type": "object", "additionalProperties": False, "required": ["required", "date", "location", "notes"], "properties": {"required": {"type": "string"}, "date": {"type": "string"}, "location": {"type": "string"}, "notes": {"type": "string"}}},
            "closing_rules": {"type": "array", "items": {"type": "string"}},
            "contract_duration": {"type": "string"},
            "location": {"type": "string"},
            "industry_tags": {"type": "array", "items": {"type": "string"}},
            "risk_flags": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        },
    }
    response = client.responses.create(
        model=DEFAULT_OPENAI_MODEL,
        input=[
            {"role": "system", "content": "Extract structured procurement intelligence from the tender document text. Capture explicit requirements only and do not invent facts."},
            {"role": "user", "content": "Tender metadata:\n" + "\n".join([f"{k}: {metadata.get(k) or ''}" for k in ["title", "buyer_name", "province", "closing_date", "source_url"]]) + "\n\nTender document text:\n" + text[:24000]},
        ],
        text=_responses_json_schema("tender_document_parse", schema),
    )
    return _response_to_json(response)
