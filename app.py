
from __future__ import annotations

import io
import json
import os
import re
import tempfile
from datetime import date, datetime, timezone
from typing import Any

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from sqlalchemy import desc, func, or_, select

from database import get_db_session, init_db
from models import AnalysisJob, IngestRun, Profile, ProfileIssue, TenderCache, TenderDocumentCache
from services.etenders_ingest import ingest_tenders

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None

try:
    from docx import Document
except Exception:  # pragma: no cover
    Document = None

try:
    from pypdf import PdfReader
except Exception:  # pragma: no cover
    try:
        from PyPDF2 import PdfReader
    except Exception:  # pragma: no cover
        PdfReader = None


APP_VERSION = os.getenv("APP_VERSION", "v7.1")
DEFAULT_OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "tenderai-dev-secret")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024


def safe_init_db() -> None:
    try:
        init_db()
    except Exception as exc:
        message = str(exc).lower()
        tolerated = (
            "already exists" in message
            or "duplicate" in message
            or "duplicatetable" in message
            or "relation" in message and "exists" in message
        )
        if not tolerated:
            raise


safe_init_db()


def get_openai_client():
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key or OpenAI is None:
        return None
    try:
        return OpenAI(api_key=api_key)
    except Exception:
        return None


def parse_jsonish(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return default
        try:
            return json.loads(raw)
        except Exception:
            return default
    return default


def split_csvish(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        cleaned = []
        seen = set()
        for item in value:
            item_str = str(item).strip()
            if item_str and item_str.lower() not in seen:
                seen.add(item_str.lower())
                cleaned.append(item_str)
        return cleaned
    text = str(value)
    parts = re.split(r"[,;\n|/]+", text)
    cleaned = []
    seen = set()
    for part in parts:
        part = " ".join(part.split()).strip()
        if part and part.lower() not in seen:
            seen.add(part.lower())
            cleaned.append(part)
    return cleaned


def normalize_keywords(text: str) -> list[str]:
    if not text:
        return []
    cleaned = re.sub(r"[^a-zA-Z0-9,\-/& ]+", " ", text.lower())
    parts = re.split(r"[,/\n;|]+", cleaned)
    words = []
    seen = set()
    for part in parts:
        part = " ".join(part.split()).strip()
        if len(part) > 2 and part not in seen:
            seen.add(part)
            words.append(part)
    return words[:50]


def extract_pdf_text(file_storage) -> str:
    if PdfReader is None:
        return ""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp_path = tmp.name
            file_storage.save(tmp.name)

        reader = PdfReader(tmp_path)
        pages = []
        for page in getattr(reader, "pages", []):
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(pages).strip()
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def heuristic_profile_parse(text: str, filename: str | None = None) -> dict[str, Any]:
    lower = text.lower()

    industry = None
    industry_rules = [
        ("Construction", ["construction", "contractor", "civil", "infrastructure"]),
        ("ICT", ["software", "ict", "technology", "systems", "digital", "it services", "website", "web development"]),
        ("Transport", ["transport", "shuttle", "fleet", "vehicle", "logistics"]),
        ("Professional Services", ["consulting", "advisory", "facilitation", "professional services"]),
        ("Tourism", ["tourism", "travel", "adventure", "hospitality", "destination"]),
        ("Security", ["security services", "guarding", "surveillance"]),
        ("Education", ["training provider", "education", "learnership", "skills development"]),
    ]
    for label, words in industry_rules:
        if any(word in lower for word in words):
            industry = label
            break

    capabilities = []
    capability_patterns = [
        r"services\s*[:\-]\s*(.+)",
        r"capabilities\s*[:\-]\s*(.+)",
        r"core services\s*[:\-]\s*(.+)",
        r"scope\s*[:\-]\s*(.+)",
        r"specialisation\s*[:\-]\s*(.+)",
    ]
    for pattern in capability_patterns:
        matches = re.findall(pattern, text, flags=re.IGNORECASE)
        for match in matches:
            capabilities.extend(normalize_keywords(match))

    if not capabilities:
        capabilities = normalize_keywords(text)[:20]

    provinces = [
        "Gauteng",
        "KwaZulu-Natal",
        "Western Cape",
        "Eastern Cape",
        "Free State",
        "Mpumalanga",
        "Limpopo",
        "North West",
        "Northern Cape",
    ]
    locations = [province for province in provinces if province.lower() in lower]

    company_name = None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines:
        company_name = lines[0][:255]
    if filename and not company_name:
        company_name = os.path.splitext(filename)[0]

    issues = []
    if not capabilities:
        issues.append(
            {
                "title": "Capabilities are not clearly structured",
                "detail": "The uploaded document does not clearly list service capabilities.",
                "penalty_weight": 6,
            }
        )
    if not locations:
        issues.append(
            {
                "title": "Operational footprint is unclear",
                "detail": "No clear province or service geography was confidently detected.",
                "penalty_weight": 4,
            }
        )

    return {
        "company_name": company_name,
        "industry": industry,
        "capabilities": capabilities[:20],
        "locations": list(dict.fromkeys(locations)),
        "issues": issues,
        "bbbee_information": {"verification_status": "Not clearly detected"},
        "industry_main_groups": [industry] if industry else [],
        "industry_divisions": capabilities[:5],
        "accreditations": [],
    }


def openai_parse_profile(text: str) -> dict[str, Any] | None:
    client = get_openai_client()
    if not client:
        return None

    prompt = f"""
Extract supplier profile information from the following document text.

Return strict JSON with keys:
company_name, industry, capabilities, locations, issues, bbbee_information, industry_main_groups, industry_divisions, accreditations

Rules:
- capabilities, locations, industry_main_groups, industry_divisions, and accreditations must be arrays
- issues must be an array of objects with keys: title, detail, penalty_weight
- bbbee_information must be an object with key verification_status
- infer conservatively
- no markdown
- no prose outside JSON

TEXT:
{text[:18000]}
""".strip()

    try:
        response = client.responses.create(
            model=DEFAULT_OPENAI_MODEL,
            input=prompt,
            temperature=0.2,
        )
        return json.loads((response.output_text or "").strip())
    except Exception:
        return None


def get_latest_ingest(session):
    return (
        session.execute(
            select(IngestRun)
            .order_by(desc(IngestRun.started_at), desc(IngestRun.id))
            .limit(1)
        )
        .scalars()
        .first()
    )


def get_active_profile(session):
    return (
        session.execute(
            select(Profile)
            .where(Profile.is_active.is_(True))
            .order_by(desc(Profile.updated_at), desc(Profile.id))
            .limit(1)
        )
        .scalars()
        .first()
    )


def get_selected_profile(session, raw_profile_id: str | None):
    if raw_profile_id:
        try:
            profile = session.get(Profile, int(raw_profile_id))
            if profile:
                return profile
        except Exception:
            pass
    return get_active_profile(session)


def pending_penalty(profile: Profile | None) -> float:
    if not profile:
        return 0.0
    penalty = 0.0
    for issue in getattr(profile, "issues", []) or []:
        status = (getattr(issue, "status", "") or "").lower()
        weight = float(getattr(issue, "penalty_weight", 0) or 0)
        if status == "pending":
            penalty += weight
        elif status == "fixed":
            penalty -= min(weight, 2.0)
    return penalty


def keyword_overlap_score(profile: Profile | None, tender: TenderCache) -> float | None:
    if not profile:
        return None

    score = 30.0
    tender_blob = " ".join(
        [
            getattr(tender, "title", "") or "",
            getattr(tender, "description", "") or "",
            getattr(tender, "industry", "") or "",
            getattr(tender, "tender_type", "") or "",
            getattr(tender, "province", "") or "",
            getattr(tender, "buyer_name", "") or "",
            getattr(tender, "main_procurement_category", "") or "",
        ]
    ).lower()

    profile_industry = getattr(profile, "industry", None)
    tender_industry = getattr(tender, "industry", None)
    if profile_industry and tender_industry and profile_industry.lower() == tender_industry.lower():
        score += 25.0
    elif profile_industry and profile_industry.lower() in tender_blob:
        score += 15.0

    capabilities = []
    if hasattr(profile, "capability_list"):
        try:
            capabilities = profile.capability_list()
        except Exception:
            capabilities = split_csvish(getattr(profile, "capabilities_text", ""))
    else:
        capabilities = split_csvish(getattr(profile, "capabilities_text", ""))

    matches = 0
    for capability in capabilities:
        if capability.lower() in tender_blob:
            matches += 1
    score += min(matches * 6.0, 30.0)

    if hasattr(profile, "location_list"):
        try:
            locations = profile.location_list()
        except Exception:
            locations = split_csvish(getattr(profile, "locations_text", ""))
    else:
        locations = split_csvish(getattr(profile, "locations_text", ""))

    tender_province = getattr(tender, "province", None)
    if tender_province and any(tender_province.lower() == loc.lower() for loc in locations):
        score += 8.0

    closing_date = getattr(tender, "closing_date", None)
    if closing_date:
        try:
            days = (closing_date - date.today()).days
            if days >= 0:
                score += min(days / 2.0, 7.0)
        except Exception:
            pass

    score -= pending_penalty(profile)
    return max(0.0, min(score, 100.0))


def fit_band(score: float | None) -> str:
    if score is None:
        return "Unscored"
    if score >= 75:
        return "High Fit"
    if score >= 50:
        return "Medium Fit"
    return "Low Fit"


def competitiveness_label(score: float | None) -> str:
    if score is None:
        return "Not enough profile data"
    if score >= 75:
        return "Potentially competitive if compliance is complete."
    if score >= 50:
        return "Moderate competitiveness; needs sharper proof of relevance."
    return "Likely challenging unless the bid is very tightly tailored."


def execution_investment_label(score: float | None) -> str:
    if score is None:
        return "Undetermined"
    if score >= 75:
        return "Moderate effort; alignment appears naturally strong."
    if score >= 50:
        return "Meaningful tailoring needed across methodology and compliance."
    return "High effort for a lower-probability fit."


def parse_multiline_text(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [line.strip() for line in str(value).splitlines() if line.strip()]


def get_latest_analysis_job(session, profile: Profile | None, tender: TenderCache):
    if not profile:
        return None
    return (
        session.execute(
            select(AnalysisJob)
            .where(AnalysisJob.profile_id == profile.id, AnalysisJob.tender_id == tender.id)
            .order_by(desc(AnalysisJob.updated_at), desc(AnalysisJob.id))
            .limit(1)
        )
        .scalars()
        .first()
    )


def serialize_ingest(ingest: IngestRun | None) -> dict[str, Any] | None:
    if not ingest:
        return None
    return {
        "id": ingest.id,
        "status": getattr(ingest, "status", None),
        "started_at": getattr(ingest, "started_at", None),
        "finished_at": getattr(ingest, "finished_at", None),
        "failure_message": getattr(ingest, "failure_message", None),
    }


def serialize_profile(profile: Profile) -> dict[str, Any]:
    parsed = parse_jsonish(getattr(profile, "parsed_json", None), {})
    company_name = getattr(profile, "company_name", None) or getattr(profile, "name", None)
    file_name = (
        getattr(profile, "original_filename", None)
        or getattr(profile, "file_name", None)
        or f"profile_{profile.id}.pdf"
    )

    capabilities = split_csvish(
        parsed.get("capabilities") if isinstance(parsed, dict) else None
    ) or split_csvish(getattr(profile, "capabilities_text", ""))

    locations = split_csvish(
        parsed.get("locations") if isinstance(parsed, dict) else None
    ) or split_csvish(getattr(profile, "locations_text", ""))

    accreditations = parsed.get("accreditations") if isinstance(parsed, dict) else []
    if not isinstance(accreditations, list):
        accreditations = []

    issues_payload = []
    for issue in getattr(profile, "issues", []) or []:
        issues_payload.append(
            {
                "id": getattr(issue, "id", None),
                "title": getattr(issue, "title", None),
                "detail": getattr(issue, "detail", None),
                "penalty_weight": getattr(issue, "penalty_weight", None),
                "status": getattr(issue, "status", None),
            }
        )

    summary = {
        "company_name": company_name,
        "bbbee_information": (
            parsed.get("bbbee_information")
            if isinstance(parsed, dict) and isinstance(parsed.get("bbbee_information"), dict)
            else {"verification_status": "Not clearly detected"}
        ),
        "industry_main_groups": (
            parsed.get("industry_main_groups")
            if isinstance(parsed, dict) and isinstance(parsed.get("industry_main_groups"), list)
            else ([getattr(profile, "industry", None)] if getattr(profile, "industry", None) else [])
        ),
        "industry_divisions": (
            parsed.get("industry_divisions")
            if isinstance(parsed, dict) and isinstance(parsed.get("industry_divisions"), list)
            else capabilities[:5]
        ),
        "accreditations": accreditations,
        "capabilities": capabilities,
        "locations": locations,
    }

    return {
        "id": profile.id,
        "company_name": company_name,
        "name": getattr(profile, "name", None),
        "file_name": file_name,
        "original_filename": file_name,
        "industry": getattr(profile, "industry", None),
        "capabilities": capabilities,
        "locations": locations,
        "is_active": bool(getattr(profile, "is_active", False)),
        "uploaded_at": getattr(profile, "updated_at", None) or getattr(profile, "created_at", None),
        "updated_at": getattr(profile, "updated_at", None),
        "summary": summary,
        "issues": issues_payload,
    }


def default_parsed_document() -> dict[str, list[str]]:
    return {
        "mandatory_requirements": [],
        "specifications_scope": [],
        "evaluation_cues": [],
        "briefing_details": [],
    }


def load_document_cache_payload(session, tender: TenderCache) -> dict[str, Any]:
    try:
        stmt = select(TenderDocumentCache)
        if hasattr(TenderDocumentCache, "tender_id"):
            stmt = stmt.where(TenderDocumentCache.tender_id == tender.id)
        if hasattr(TenderDocumentCache, "updated_at"):
            stmt = stmt.order_by(desc(TenderDocumentCache.updated_at), desc(TenderDocumentCache.id))
        elif hasattr(TenderDocumentCache, "id"):
            stmt = stmt.order_by(desc(TenderDocumentCache.id))
        stmt = stmt.limit(1)
        row = session.execute(stmt).scalars().first()
    except Exception:
        row = None

    if not row:
        return {}

    for attr_name in ("parsed_json", "structured_json", "raw_result_json", "document_json"):
        parsed = parse_jsonish(getattr(row, attr_name, None), None)
        if isinstance(parsed, dict):
            return parsed
    return {}


def build_analysis_payload(profile: Profile | None, tender: TenderCache, job: AnalysisJob | None) -> dict[str, Any]:
    base_score = keyword_overlap_score(profile, tender)
    payload = {
        "fit_score": round(base_score or 0, 1) if base_score is not None else None,
        "fit_band": fit_band(base_score),
        "competitiveness": competitiveness_label(base_score),
        "execution_investment": execution_investment_label(base_score),
        "fit_reasons": [],
        "risk_flags": [],
        "strategic_readiness": [],
        "summary": None,
    }

    if profile:
        if getattr(profile, "industry", None) and getattr(tender, "industry", None):
            if profile.industry.lower() == tender.industry.lower():
                payload["fit_reasons"].append("Industry alignment is strong.")
        if getattr(tender, "province", None):
            locations = split_csvish(getattr(profile, "locations_text", ""))
            if any((tender.province or "").lower() == item.lower() for item in locations):
                payload["fit_reasons"].append("Tender geography matches the supplier footprint.")
        capabilities = split_csvish(getattr(profile, "capabilities_text", ""))
        tender_blob = " ".join(
            [
                getattr(tender, "title", "") or "",
                getattr(tender, "description", "") or "",
                getattr(tender, "industry", "") or "",
                getattr(tender, "tender_type", "") or "",
            ]
        ).lower()
        capability_hits = [cap for cap in capabilities if cap.lower() in tender_blob]
        if capability_hits:
            payload["fit_reasons"].append(
                f"Capability overlap detected: {', '.join(capability_hits[:4])}."
            )
        pending_issues = [issue for issue in getattr(profile, "issues", []) or [] if (getattr(issue, "status", "") or "").lower() == "pending"]
        if pending_issues:
            payload["risk_flags"].append("Pending supplier profile issues may weaken compliance readiness.")
            payload["strategic_readiness"].append("Resolve pending profile issues before submission.")
    else:
        payload["risk_flags"].append("No active supplier profile is selected.")

    if getattr(tender, "closing_date", None):
        try:
            days_left = (tender.closing_date - date.today()).days
            if days_left <= 3:
                payload["risk_flags"].append("Closing date is near; response turnaround may be tight.")
            elif days_left <= 7:
                payload["strategic_readiness"].append("Bid window is still workable, but prioritise document checks now.")
        except Exception:
            pass

    if not payload["strategic_readiness"]:
        payload["strategic_readiness"].append("Confirm mandatory documents, scoring thresholds, and briefing requirements early.")

    if job:
        payload["fit_score"] = round(float(getattr(job, "score", 0) or 0), 1)
        payload["fit_band"] = fit_band(payload["fit_score"])
        payload["summary"] = getattr(job, "summary", None)
        strengths = parse_multiline_text(getattr(job, "strengths_text", None))
        risks = parse_multiline_text(getattr(job, "risks_text", None))
        recs = parse_multiline_text(getattr(job, "recommendations_text", None))
        if strengths:
            payload["fit_reasons"] = strengths
        if risks:
            payload["risk_flags"] = risks
        if recs:
            payload["strategic_readiness"] = recs
        payload["competitiveness"] = competitiveness_label(payload["fit_score"])
        payload["execution_investment"] = execution_investment_label(payload["fit_score"])

    if not payload["fit_reasons"]:
        payload["fit_reasons"].append("Some baseline keyword overlap exists between the supplier profile and the tender scope.")
    if not payload["risk_flags"]:
        payload["risk_flags"].append("No major risk flags were surfaced from the current extraction.")
    return payload


def serialize_tender(session, tender: TenderCache, profile: Profile | None = None) -> dict[str, Any]:
    parsed_document = default_parsed_document()
    doc_payload = load_document_cache_payload(session, tender)
    for key in parsed_document:
        value = doc_payload.get(key)
        if isinstance(value, list):
            parsed_document[key] = [str(item).strip() for item in value if str(item).strip()]
        elif isinstance(value, str) and value.strip():
            parsed_document[key] = [value.strip()]

    job = get_latest_analysis_job(session, profile, tender)
    analysis = build_analysis_payload(profile, tender, job)

    return {
        "id": getattr(tender, "id", None),
        "tender_id": getattr(tender, "tender_id", None),
        "ocid": getattr(tender, "ocid", None),
        "title": getattr(tender, "title", None),
        "description": getattr(tender, "description", None),
        "industry": getattr(tender, "industry", None),
        "tender_type": getattr(tender, "tender_type", None),
        "province": getattr(tender, "province", None),
        "buyer_name": getattr(tender, "buyer_name", None),
        "status": getattr(tender, "status", None),
        "closing_date": getattr(tender, "closing_date", None),
        "issued_date": getattr(tender, "issued_date", None),
        "updated_at": getattr(tender, "updated_at", None),
        "main_procurement_category": getattr(tender, "main_procurement_category", None),
        "eligibility_criteria": getattr(tender, "eligibility_criteria", None),
        "special_conditions": getattr(tender, "special_conditions", None),
        "contact_person": getattr(tender, "contact_person", None),
        "delivery_location": getattr(tender, "delivery_location", None),
        "document_url": getattr(tender, "document_url", None),
        "document_title": getattr(tender, "document_title", None),
        "analysis": analysis,
        "parsed_document": parsed_document,
        "analysis_job_id": getattr(job, "id", None) if job else None,
    }


def find_tender(session, tender_id: str):
    conditions = []
    if tender_id.isdigit():
        conditions.append(TenderCache.id == int(tender_id))
    if hasattr(TenderCache, "tender_id"):
        conditions.append(TenderCache.tender_id == tender_id)
    if hasattr(TenderCache, "ocid"):
        conditions.append(TenderCache.ocid == tender_id)

    if not conditions:
        return None

    return (
        session.execute(select(TenderCache).where(or_(*conditions)).limit(1))
        .scalars()
        .first()
    )


def openai_analyze(profile: Profile, tender: TenderCache) -> dict[str, Any] | None:
    client = get_openai_client()
    if not client:
        return None

    prompt = f"""
Compare this supplier profile to this tender and return strict JSON.

JSON keys:
score, summary, strengths, risks, recommendations

Rules:
- score is 0 to 100
- strengths, risks, recommendations are arrays of short strings
- be practical and procurement-aware
- no markdown
- no prose outside JSON

SUPPLIER PROFILE:
Company: {getattr(profile, 'company_name', None)}
Industry: {getattr(profile, 'industry', None)}
Capabilities: {getattr(profile, 'capabilities_text', None)}
Locations: {getattr(profile, 'locations_text', None)}
Issues:
{chr(10).join([f"- {getattr(i, 'title', '')} ({getattr(i, 'status', '')})" for i in getattr(profile, 'issues', []) or []])}

TENDER:
Title: {getattr(tender, 'title', None)}
Buyer: {getattr(tender, 'buyer_name', None)}
Province: {getattr(tender, 'province', None)}
Type: {getattr(tender, 'tender_type', None)}
Industry: {getattr(tender, 'industry', None)}
Issued: {getattr(tender, 'issued_date', None)}
Closing: {getattr(tender, 'closing_date', None)}
Description:
{(getattr(tender, 'description', '') or '')[:7000]}
""".strip()

    try:
        response = client.responses.create(
            model=DEFAULT_OPENAI_MODEL,
            input=prompt,
            temperature=0.2,
        )
        return json.loads((response.output_text or "").strip())
    except Exception:
        return None


def fallback_analyze(profile: Profile, tender: TenderCache) -> dict[str, Any]:
    score = keyword_overlap_score(profile, tender) or 0.0
    strengths = []
    risks = []
    recommendations = []

    if getattr(profile, "industry", None) and getattr(tender, "industry", None):
        if profile.industry.lower() == tender.industry.lower():
            strengths.append("Industry alignment is strong.")
    if getattr(tender, "province", None):
        locations = split_csvish(getattr(profile, "locations_text", ""))
        if any((tender.province or "").lower() == item.lower() for item in locations):
            strengths.append("Geographic relevance matches the active supplier footprint.")
    if score >= 70:
        strengths.append("Capability overlap suggests a competitive fit.")

    pending = [issue for issue in getattr(profile, "issues", []) or [] if (getattr(issue, "status", "") or "").lower() == "pending"]
    if pending:
        risks.append("Pending supplier profile issues may weaken compliance readiness.")
        recommendations.append("Resolve pending profile issues before submission.")

    if score < 60:
        risks.append("Tender requirements appear broader than the current profile strength.")
        recommendations.append("Sharpen capability evidence and supporting credentials in the bid.")

    if not strengths:
        strengths.append("Some baseline overlap exists between profile keywords and tender requirements.")
    if not recommendations:
        recommendations.append("Emphasise exact capability match, delivery history, and compliance readiness.")

    return {
        "score": round(score, 1),
        "summary": (
            f"This opportunity scored {round(score, 1)}/100 using fallback scoring based on "
            "industry, capability overlap, location relevance, and issue penalties."
        ),
        "strengths": strengths,
        "risks": risks,
        "recommendations": recommendations,
    }


def get_or_create_analysis(session, profile: Profile, tender: TenderCache) -> AnalysisJob:
    existing = get_latest_analysis_job(session, profile, tender)
    if existing:
        return existing

    job = AnalysisJob(profile_id=profile.id, tender_id=tender.id, status="queued")
    session.add(job)
    session.flush()
    return job


def run_analysis(session, profile: Profile, tender: TenderCache) -> AnalysisJob:
    job = get_or_create_analysis(session, profile, tender)
    job.status = "running"
    session.flush()

    result = openai_analyze(profile, tender) or fallback_analyze(profile, tender)

    job.status = "completed"
    job.score = float(result.get("score") or 0)
    job.summary = result.get("summary")
    job.strengths_text = "\n".join(result.get("strengths") or [])
    job.risks_text = "\n".join(result.get("risks") or [])
    job.recommendations_text = "\n".join(result.get("recommendations") or [])
    job.raw_result_json = json.dumps(result, ensure_ascii=False)
    session.flush()
    return job


def generate_proposal_text(profile: Profile, tender: TenderCache, job: AnalysisJob) -> str:
    client = get_openai_client()
    if client:
        prompt = f"""
Write a concise tender proposal draft for the supplier below.

Style:
- professional
- direct
- practical
- tailored to the tender
- no fake claims
- use short headings
- highlight fit, delivery approach, and readiness

SUPPLIER:
Company: {getattr(profile, 'company_name', None)}
Industry: {getattr(profile, 'industry', None)}
Capabilities: {getattr(profile, 'capabilities_text', None)}
Locations: {getattr(profile, 'locations_text', None)}

TENDER:
Title: {getattr(tender, 'title', None)}
Buyer: {getattr(tender, 'buyer_name', None)}
Province: {getattr(tender, 'province', None)}
Type: {getattr(tender, 'tender_type', None)}
Industry: {getattr(tender, 'industry', None)}
Issued: {getattr(tender, 'issued_date', None)}
Closing: {getattr(tender, 'closing_date', None)}
Description:
{(getattr(tender, 'description', '') or '')[:7000]}

ANALYSIS:
Summary: {getattr(job, 'summary', None)}
Strengths: {getattr(job, 'strengths_text', None)}
Risks: {getattr(job, 'risks_text', None)}
Recommendations: {getattr(job, 'recommendations_text', None)}
""".strip()
        try:
            response = client.responses.create(
                model=DEFAULT_OPENAI_MODEL,
                input=prompt,
                temperature=0.4,
            )
            proposal = (response.output_text or "").strip()
            if proposal:
                return proposal
        except Exception:
            pass

    return f"""
Tender Proposal Draft

Supplier
{getattr(profile, "company_name", None) or getattr(profile, "name", None) or "Supplier"}

Tender
{getattr(tender, "title", None) or "Tender opportunity"}

Introduction
We submit this response in relation to the above tender opportunity. Based on the current TenderAI assessment, there is practical alignment between the tender scope and our operating profile.

Why We Fit
{getattr(job, "strengths_text", None) or "Our capability profile shows relevant overlap with the tender scope."}

Delivery Approach
We would structure delivery around the exact specifications, reporting obligations, timelines, and compliance expectations stated in the tender documents.

Risk Management
{getattr(job, "risks_text", None) or "We will manage submission and delivery risk through documented controls and early compliance checks."}

Next Steps
{getattr(job, "recommendations_text", None) or "Tailor the bid response to the scope, confirm mandatory documents, and strengthen proof of past performance."}
""".strip()


def build_proposal_docx(profile: Profile, tender: TenderCache, job: AnalysisJob) -> io.BytesIO:
    if not getattr(job, "proposal_draft_text", None):
        job.proposal_draft_text = generate_proposal_text(profile, tender, job)

    if Document is None:
        buffer = io.BytesIO()
        buffer.write((job.proposal_draft_text or "").encode("utf-8"))
        buffer.seek(0)
        return buffer

    doc = Document()
    doc.add_heading("Tender Proposal Draft", 0)
    doc.add_paragraph(f"Supplier: {getattr(profile, 'company_name', None) or getattr(profile, 'name', None) or 'Supplier'}")
    doc.add_paragraph(f"Tender: {getattr(tender, 'title', None) or 'Tender opportunity'}")
    doc.add_paragraph(f"Buyer: {getattr(tender, 'buyer_name', None) or 'N/A'}")
    doc.add_paragraph(f"Province: {getattr(tender, 'province', None) or 'N/A'}")
    doc.add_paragraph(f"Closing Date: {getattr(tender, 'closing_date', None) or 'N/A'}")
    doc.add_paragraph(f"Alignment Score: {round(float(getattr(job, 'score', 0) or 0), 1)}/100")

    doc.add_heading("Draft", level=1)
    for paragraph in (job.proposal_draft_text or "").split("\n\n"):
        text = paragraph.strip()
        if text:
            doc.add_paragraph(text)

    buffer = io.BytesIO()
    doc.save(buffer)
    buffer.seek(0)
    return buffer


@app.context_processor
def inject_globals():
    with get_db_session() as session:
        active_profile = get_active_profile(session)
        latest_ingest = get_latest_ingest(session)
        return {
            "app_version": APP_VERSION,
            "active_profile": serialize_profile(active_profile) if active_profile else None,
            "latest_ingest": serialize_ingest(latest_ingest),
            "today": date.today(),
        }


@app.template_filter("days_left")
def days_left_filter(closing_date):
    if not closing_date:
        return None
    try:
        return (closing_date - date.today()).days
    except Exception:
        return None


@app.get("/health")
def health():
    with get_db_session() as session:
        count = session.execute(select(func.count()).select_from(TenderCache)).scalar_one()
        return jsonify(
            {
                "ok": True,
                "cached_tenders": count,
                "app_version": APP_VERSION,
                "time": utcnow().isoformat(),
            }
        )


@app.get("/")
def home():
    with get_db_session() as session:
        profile = get_selected_profile(session, request.args.get("profile_id"))

        total_live = session.execute(
            select(func.count()).select_from(TenderCache).where(TenderCache.is_live.is_(True))
        ).scalar_one()

        tenders = (
            session.execute(
                select(TenderCache)
                .where(TenderCache.is_live.is_(True))
                .order_by(TenderCache.closing_date.asc().nulls_last(), desc(TenderCache.updated_at))
                .limit(24)
            )
            .scalars()
            .all()
        )

        tender_payloads = [serialize_tender(session, tender, profile) for tender in tenders]
        if profile:
            tender_payloads.sort(key=lambda item: item.get("analysis", {}).get("fit_score") or 0, reverse=True)

        profiles_list = (
            session.execute(select(Profile).order_by(desc(Profile.updated_at), desc(Profile.id)))
            .scalars()
            .all()
        )
        profile_payloads = [serialize_profile(item) for item in profiles_list]

        return render_template(
            "home.html",
            total_live=total_live,
            tenders=tender_payloads[:12],
            featured=tender_payloads[:12],
            profiles=profile_payloads,
        )


@app.get("/profiles")
def profiles_page():
    with get_db_session() as session:
        profiles_list = (
            session.execute(select(Profile).order_by(desc(Profile.updated_at), desc(Profile.id)))
            .scalars()
            .all()
        )
        payloads = [serialize_profile(item) for item in profiles_list]
        return render_template("profiles.html", profiles=payloads)


@app.get("/tenders")
def tenders_page():
    with get_db_session() as session:
        profile = get_selected_profile(session, request.args.get("profile_id"))
        prompt = (request.args.get("prompt") or "").strip()

        province = (request.args.get("province") or "").strip()
        tender_type = (request.args.get("tender_type") or "").strip()
        industry = (request.args.get("industry") or "").strip()
        issued_from = (request.args.get("issued_from") or "").strip()
        search_text = (request.args.get("q") or "").strip()

        query = select(TenderCache).where(TenderCache.is_live.is_(True))

        if province:
            query = query.where(TenderCache.province == province)
        if tender_type:
            query = query.where(TenderCache.tender_type == tender_type)
        if industry:
            query = query.where(TenderCache.industry == industry)
        if issued_from:
            try:
                issued_from_date = datetime.strptime(issued_from, "%Y-%m-%d").date()
                query = query.where(TenderCache.issued_date >= issued_from_date)
            except ValueError:
                pass
        if search_text:
            like_term = f"%{search_text.lower()}%"
            query = query.where(
                or_(
                    func.lower(func.coalesce(TenderCache.title, "")).like(like_term),
                    func.lower(func.coalesce(TenderCache.description, "")).like(like_term),
                    func.lower(func.coalesce(TenderCache.buyer_name, "")).like(like_term),
                    func.lower(func.coalesce(TenderCache.industry, "")).like(like_term),
                )
            )

        items = (
            session.execute(
                query.order_by(TenderCache.closing_date.asc().nulls_last(), desc(TenderCache.updated_at)).limit(200)
            )
            .scalars()
            .all()
        )
        tender_payloads = [serialize_tender(session, item, profile) for item in items]
        if profile:
            tender_payloads.sort(key=lambda item: item.get("analysis", {}).get("fit_score") or 0, reverse=True)

        provinces = (
            session.execute(
                select(TenderCache.province)
                .where(TenderCache.is_live.is_(True), TenderCache.province.is_not(None))
                .distinct()
                .order_by(TenderCache.province)
            )
            .scalars()
            .all()
        )
        tender_types = (
            session.execute(
                select(TenderCache.tender_type)
                .where(TenderCache.is_live.is_(True), TenderCache.tender_type.is_not(None))
                .distinct()
                .order_by(TenderCache.tender_type)
            )
            .scalars()
            .all()
        )
        industries = (
            session.execute(
                select(TenderCache.industry)
                .where(TenderCache.is_live.is_(True), TenderCache.industry.is_not(None))
                .distinct()
                .order_by(TenderCache.industry)
            )
            .scalars()
            .all()
        )

        return render_template(
            "feed.html",
            prompt=prompt,
            tenders=tender_payloads,
            ranked_tenders=tender_payloads,
            provinces=provinces,
            tender_types=tender_types,
            industries=industries,
            filters={
                "province": province,
                "tender_type": tender_type,
                "industry": industry,
                "issued_from": issued_from,
                "q": search_text,
            },
            error_message=None,
        )


@app.get("/tender/<path:tender_id>")
def tender_detail_page(tender_id: str):
    with get_db_session() as session:
        tender = find_tender(session, tender_id)
        if not tender:
            abort(404)

        profile = get_selected_profile(session, request.args.get("profile_id"))
        payload = serialize_tender(session, tender, profile)
        return render_template("tender_detail.html", tender=payload)


app.add_url_rule("/tenders/<path:tender_id>", endpoint="tender_detail_compat", view_func=tender_detail_page)


@app.get("/api/profiles")
def api_profiles():
    with get_db_session() as session:
        profiles_list = (
            session.execute(select(Profile).order_by(desc(Profile.updated_at), desc(Profile.id)))
            .scalars()
            .all()
        )
        return jsonify([serialize_profile(item) for item in profiles_list])


@app.post("/api/profiles")
def api_upload_profile():
    uploaded = request.files.get("file")
    if not uploaded:
        return jsonify({"error": "No file uploaded."}), 400

    filename = uploaded.filename or "profile.pdf"
    text = extract_pdf_text(uploaded)
    if not text.strip():
        parsed = heuristic_profile_parse("", filename)
    else:
        parsed = openai_parse_profile(text) or heuristic_profile_parse(text, filename)

    with get_db_session() as session:
        session.execute(Profile.__table__.update().values(is_active=False))

        profile = Profile(
            name=parsed.get("company_name") or os.path.splitext(filename)[0],
            company_name=parsed.get("company_name") or os.path.splitext(filename)[0],
            original_filename=filename,
            industry=parsed.get("industry"),
            capabilities_text=", ".join(split_csvish(parsed.get("capabilities"))),
            locations_text=", ".join(split_csvish(parsed.get("locations"))),
            extracted_text=(text or "")[:200000],
            parsed_json=json.dumps(parsed, ensure_ascii=False),
            is_active=True,
        )
        session.add(profile)
        session.flush()

        issues = parsed.get("issues") or []
        for issue in issues:
            session.add(
                ProfileIssue(
                    profile_id=profile.id,
                    issue_type="profile_gap",
                    title=issue.get("title") or "Profile issue",
                    detail=issue.get("detail"),
                    penalty_weight=float(issue.get("penalty_weight") or 5),
                    status="pending",
                )
            )
        session.flush()
        payload = serialize_profile(profile)

    return jsonify(payload), 201


@app.post("/api/profiles/<int:profile_id>/activate")
def api_activate_profile(profile_id: int):
    with get_db_session() as session:
        profile = session.get(Profile, profile_id)
        if not profile:
            return jsonify({"error": "Profile not found."}), 404

        session.execute(Profile.__table__.update().values(is_active=False))
        profile.is_active = True
        session.flush()

        return jsonify({"ok": True, "active_profile_id": profile.id})


@app.delete("/api/profiles/<int:profile_id>")
def api_delete_profile(profile_id: int):
    with get_db_session() as session:
        profile = session.get(Profile, profile_id)
        if not profile:
            return jsonify({"error": "Profile not found."}), 404

        was_active = bool(getattr(profile, "is_active", False))
        session.delete(profile)
        session.flush()

        if was_active:
            replacement = (
                session.execute(
                    select(Profile)
                    .order_by(desc(Profile.updated_at), desc(Profile.id))
                    .limit(1)
                )
                .scalars()
                .first()
            )
            if replacement:
                replacement.is_active = True
                session.flush()
                return jsonify({"ok": True, "new_active_profile_id": replacement.id})
        return jsonify({"ok": True})


@app.post("/tender/<path:tender_id>/analyze")
def analyze_tender(tender_id: str):
    with get_db_session() as session:
        tender = find_tender(session, tender_id)
        profile = get_selected_profile(session, request.form.get("profile_id") or request.args.get("profile_id"))

        if not tender:
            flash("Tender not found.", "error")
            return redirect(url_for("tenders_page"))
        if not profile:
            flash("Upload and activate a supplier profile first.", "error")
            return redirect(url_for("profiles_page"))

        job = run_analysis(session, profile, tender)
        flash(f"Tender analyzed. Score: {round(float(job.score or 0), 1)}/100", "success")
        return redirect(url_for("tender_detail_page", tender_id=tender.tender_id or tender.ocid or tender.id))


@app.get("/analysis/<int:job_id>/proposal.docx")
def download_proposal(job_id: int):
    with get_db_session() as session:
        job = session.get(AnalysisJob, job_id)
        if not job or not getattr(job, "profile", None) or not getattr(job, "tender", None):
            flash("Proposal source not found.", "error")
            return redirect(url_for("home"))

        buffer = build_proposal_docx(job.profile, job.tender, job)
        filename = f"proposal_{getattr(job.tender, 'id', 'tender')}_{getattr(job.profile, 'id', 'profile')}"
        if Document is None:
            return send_file(
                buffer,
                as_attachment=True,
                download_name=f"{filename}.txt",
                mimetype="text/plain; charset=utf-8",
            )
        return send_file(
            buffer,
            as_attachment=True,
            download_name=f"{filename}.docx",
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )


def admin_allowed() -> bool:
    token = (os.getenv("ADMIN_TOKEN") or "").strip()
    if not token:
        return True
    supplied = request.headers.get("X-Admin-Token", "") or request.args.get("token", "")
    return supplied == token


@app.route("/api/admin/run-ingest", methods=["GET", "POST"])
def api_run_ingest():
    if not admin_allowed():
        return jsonify({"ok": False, "error": "unauthorized"}), 403

    try:
        with get_db_session() as session:
            result = ingest_tenders(
                session=session,
                max_pages=int(os.getenv("INGEST_MAX_PAGES", "3")),
            )
        return jsonify(result)
    except Exception as exc:
        return jsonify({"status": "failed", "failure_message": str(exc)}), 500


@app.get("/admin/run-ingest")
def admin_run_ingest():
    if not admin_allowed():
        flash("Unauthorized.", "error")
        return redirect(url_for("home"))

    try:
        with get_db_session() as session:
            result = ingest_tenders(
                session=session,
                max_pages=int(os.getenv("INGEST_MAX_PAGES", "3")),
            )
        if result.get("status") in {"completed", "partial_success"}:
            flash(
                f"Ingest finished: {result.get('tenders_seen', 0)} tenders processed.",
                "success",
            )
        else:
            flash(f"Ingest failed: {result.get('failure_message', 'Unknown error')}", "error")
    except Exception as exc:
        flash(f"Ingest failed: {exc}", "error")
    return redirect(url_for("home"))


@app.post("/api/advise")
def api_advise():
    payload = request.get_json(silent=True) or {}
    tender = payload.get("tender") or {}
    profile = payload.get("profile") or {}
    parsed_doc = tender.get("parsed_document") or {}

    return jsonify(
        {
            "summary": "Prioritize compliance completeness, capability proof, and evaluation-fit evidence.",
            "actions": [
                "Validate all mandatory submission items against the tender document.",
                "Prepare a response structure aligned to specifications and scope headings.",
                "Surface tax, CSD, and B-BBEE evidence early in the submission pack.",
                "Address functionality thresholds and scoring cues explicitly where detected.",
                "Confirm briefing attendance requirements and date constraints before bid/no-bid.",
            ],
            "profile_signals": (profile.get("issues") or []),
            "tender_signals": parsed_doc.get("evaluation_cues", []),
        }
    )


@app.post("/api/service-request")
def api_service_request():
    payload = request.get_json(silent=True) or {}
    return jsonify(
        {
            "status": "received",
            "message": "Service request stub recorded",
            "payload": payload,
        }
    ), 202


@app.errorhandler(404)
def not_found(_):
    try:
        return render_template("404.html"), 404
    except Exception:
        return "Not found", 404


@app.errorhandler(500)
def internal_error(_):
    try:
        return render_template("500.html"), 500
    except Exception:
        return "Internal server error", 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
