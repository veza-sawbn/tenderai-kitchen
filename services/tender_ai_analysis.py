from __future__ import annotations

import json
import os
from typing import Any, Dict, List

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

from models import Profile, TenderCache
from services.tender_document_parser import get_latest_parsed_document, openai_json, as_list

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
OPENAI_TIMEOUT_SECONDS = int(os.getenv("OPENAI_TIMEOUT_SECONDS", "35"))


def get_openai_client():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    if OpenAI is None:
        raise RuntimeError("The openai Python package is not installed or could not be imported.")
    return OpenAI(api_key=api_key, timeout=OPENAI_TIMEOUT_SECONDS)


def normalize_list_text(value: str | None) -> List[str]:
    if not value:
        return []
    out = []
    seen = set()
    for item in value.replace("\n", ",").replace(";", ",").replace("|", ",").split(","):
        item = " ".join(item.strip().split())
        if not item:
            continue
        key = item.lower()
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out[:30]


def analyze_tender_against_profile(session, tender: TenderCache, profile: Profile) -> Dict[str, Any]:
    parsed_doc = get_latest_parsed_document(session, tender.id)
    if not parsed_doc:
        raise RuntimeError("No parsed tender document found. Run document parsing before analysis.")

    parsed = parsed_doc["parsed_json"]
    client = get_openai_client()

    profile_payload = {
        "company_name": profile.company_name,
        "profile_name": profile.name,
        "industry": profile.industry,
        "capabilities": normalize_list_text(profile.capabilities_text),
        "locations": normalize_list_text(profile.locations_text),
        "profile_gaps": [
            {"title": issue.title, "detail": issue.detail, "status": issue.status}
            for issue in (profile.issues or [])
        ],
    }

    tender_payload = {
        "id": tender.id,
        "title": tender.title,
        "buyer_name": tender.buyer_name,
        "province": tender.province,
        "industry": tender.industry,
        "tender_type": tender.tender_type,
        "issued_date": tender.issued_date.isoformat() if tender.issued_date else None,
        "closing_date": tender.closing_date.isoformat() if tender.closing_date else None,
    }

    system_prompt = """
You are TenderAI, a South African tender strategy analyst.

You are NOT reading a large tender document now. The document has already been parsed into compact tender intelligence.
Use the parsed tender data and active CSD/supplier profile to produce a bid/no-bid opportunity assessment.

The assessment must determine:
1. possibility of acquiring/winning the opportunity
2. fit against the active CSD/supplier profile
3. measures to improve chances
4. estimated delivery cost
5. estimated revenue/contract value
6. pricing, margin and cash-flow risks

Be specific. Use the parsed requirements, scope, dates, compliance documents and commercial clues. If exact values are unavailable, estimate conservatively and state assumptions.

Return only valid JSON:
{
 "score": number,
 "probability_of_acquisition": number,
 "fit_band": "high_potential" | "possible_fit" | "low_fit",
 "bid_decision": "pursue" | "review_first" | "do_not_prioritise",
 "executive_assessment": string,
 "scope_summary": string,
 "profile_fit": {
   "matching_capabilities": [string],
   "weaknesses_or_gaps": [string],
   "geographic_fit": string,
   "capacity_fit": string,
   "track_record_fit": string
 },
 "measures_to_improve_chances": [string],
 "estimated_project_costs": {
   "currency": "ZAR",
   "low": number|null,
   "base": number|null,
   "high": number|null,
   "cost_breakdown": [{"category": string, "estimate": number|null, "basis": string}],
   "cost_assumptions": [string]
 },
 "estimated_revenue": {
   "currency": "ZAR",
   "low": number|null,
   "base": number|null,
   "high": number|null,
   "revenue_basis": string,
   "gross_margin_comment": string
 },
 "commercial_view": {
   "pricing_strategy": string,
   "margin_risks": [string],
   "cashflow_risks": [string]
 },
 "risks": [string],
 "questions_to_clarify": [string],
 "recommended_next_steps": [string],
 "confidence_level": "high" | "medium" | "low"
}
""".strip()

    payload = {
        "supplier_profile": profile_payload,
        "tender_metadata": tender_payload,
        "parsed_tender_document": parsed,
    }

    result = openai_json(client, system_prompt, payload, max_tokens=1200)

    score = max(0.0, min(float(result.get("score") or 0), 100.0))
    probability = max(0.0, min(float(result.get("probability_of_acquisition") or score), 100.0))

    profile_fit = result.get("profile_fit") if isinstance(result.get("profile_fit"), dict) else {}
    costs = result.get("estimated_project_costs") if isinstance(result.get("estimated_project_costs"), dict) else {}
    revenue = result.get("estimated_revenue") if isinstance(result.get("estimated_revenue"), dict) else {}
    commercial = result.get("commercial_view") if isinstance(result.get("commercial_view"), dict) else {}

    result["score"] = score
    result["probability_of_acquisition"] = probability
    result["fit_band"] = result.get("fit_band") or ("high_potential" if score >= 80 else "possible_fit" if score >= 55 else "low_fit")
    result["bid_decision"] = result.get("bid_decision") or ("pursue" if probability >= 70 else "review_first" if probability >= 45 else "do_not_prioritise")
    result["scope_summary"] = result.get("scope_summary") or parsed.get("scope_summary")

    result["profile_fit"] = {
        "matching_capabilities": as_list(profile_fit.get("matching_capabilities"), 10),
        "weaknesses_or_gaps": as_list(profile_fit.get("weaknesses_or_gaps"), 10),
        "geographic_fit": profile_fit.get("geographic_fit") or "unknown",
        "capacity_fit": profile_fit.get("capacity_fit") or "unknown",
        "track_record_fit": profile_fit.get("track_record_fit") or "unknown",
    }

    result["measures_to_improve_chances"] = as_list(result.get("measures_to_improve_chances"), 10)
    result["risks"] = as_list(result.get("risks"), 10)
    result["questions_to_clarify"] = as_list(result.get("questions_to_clarify"), 10)
    result["recommended_next_steps"] = as_list(result.get("recommended_next_steps"), 10)

    result["estimated_project_costs"] = {
        "currency": costs.get("currency") or "ZAR",
        "low": costs.get("low"),
        "base": costs.get("base"),
        "high": costs.get("high"),
        "cost_breakdown": costs.get("cost_breakdown") if isinstance(costs.get("cost_breakdown"), list) else [],
        "cost_assumptions": as_list(costs.get("cost_assumptions"), 10),
    }

    result["estimated_revenue"] = {
        "currency": revenue.get("currency") or "ZAR",
        "low": revenue.get("low"),
        "base": revenue.get("base"),
        "high": revenue.get("high"),
        "revenue_basis": revenue.get("revenue_basis") or "No clear contract value was found in the parsed tender record.",
        "gross_margin_comment": revenue.get("gross_margin_comment") or "Margin depends on final pricing, delivery method and supplier cost inputs.",
    }

    result["commercial_view"] = {
        "pricing_strategy": commercial.get("pricing_strategy") or "Confirm scope, quantities and pricing schedule before final bid pricing.",
        "margin_risks": as_list(commercial.get("margin_risks"), 8),
        "cashflow_risks": as_list(commercial.get("cashflow_risks"), 8),
    }

    # Compatibility fields expected by existing templates.
    result["summary"] = result.get("executive_assessment") or result.get("scope_summary")
    result["strengths"] = result["profile_fit"]["matching_capabilities"]
    result["gaps"] = result["profile_fit"]["weaknesses_or_gaps"]
    result["recommendations"] = result["recommended_next_steps"]
    result["document_match"] = True
    result["document_match_reason"] = "Analysis used the parsed tender document intelligence record, not the raw document text."
    result["analysis_source"] = "openai_from_parsed_document"
    result["parsed_document_id"] = parsed_doc["id"]
    result["parse_confidence"] = parsed.get("parse_confidence")
    result["mandatory_requirements"] = (parsed.get("requirements_and_criteria") or {}).get("mandatory_requirements") or []
    result["compliance_documents"] = (parsed.get("requirements_and_criteria") or {}).get("compliance_documents") or []
    result["key_dates"] = parsed.get("key_dates") or []
    result["contact_email"] = parsed.get("contact_email")
    result["contact_phone"] = parsed.get("contact_phone")
    result["proposal_required"] = bool(
        "proposal" in json.dumps(parsed, ensure_ascii=False).lower()
        or "rfp" in json.dumps(parsed, ensure_ascii=False).lower()
    )
    result["evidence_notes"] = parsed.get("evidence_notes") or []

    # Preserve parsed tender intelligence in the analysis payload so the report
    # always has useful information even if the model's analysis is brief.
    result["parsed_scope_summary"] = parsed.get("scope_summary")
    result["parsed_deliverables"] = parsed.get("deliverables") or []
    result["parsed_requirements_and_criteria"] = parsed.get("requirements_and_criteria") or {}
    result["parsed_commercial_clues"] = parsed.get("commercial_clues") or {}
    result["parsed_risks_or_disqualifiers"] = parsed.get("risks_or_disqualifiers") or []

    return result
