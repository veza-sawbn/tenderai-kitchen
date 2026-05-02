from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select

from models import AnalysisJob, Profile, TenderCache, TenderDocumentCache


EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
PHONE_RE = re.compile(r"(\+?\d[\d\s\-()]{7,}\d)")
BRIEFING_RE = re.compile(
    r"(?:brief(?:ing)?(?: session)?|compulsory briefing|non-?compulsory briefing)"
    r"[^0-9]{0,30}(\d{4}[-/]\d{2}[-/]\d{2}|\d{2}[-/]\d{2}[-/]\d{4})",
    re.I,
)


def safe_loads(value: Any, fallback=None):
    if fallback is None:
        fallback = {}
    if not value:
        return fallback
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except Exception:
        return fallback


def normalize_list_text(value: Optional[str]) -> List[str]:
    if not value:
        return []
    parts = []
    seen = set()
    for chunk in re.split(r"[,;\n|]+", value):
        item = " ".join(chunk.strip().split())
        if len(item) < 2:
            continue
        low = item.lower()
        if low in seen:
            continue
        seen.add(low)
        parts.append(item)
    return parts


def extract_scope_summary(tender: TenderCache, document_text: str = "") -> str:
    doc_text = re.sub(r"\s+", " ", (document_text or "")).strip()
    if doc_text:
        for marker in [
            "scope of work",
            "description of the work",
            "description",
            "terms of reference",
            "specification",
            "project scope",
        ]:
            idx = doc_text.lower().find(marker)
            if idx >= 0:
                snippet = doc_text[idx: idx + 420].strip()
                if len(snippet) > 80:
                    return snippet[:280]
        return doc_text[:280]
    text = re.sub(r"\s+", " ", (tender.description or tender.title or "")).strip()
    return text[:280] or "Scope summary not available."


def keyword_overlap_score(profile: Profile | None, tender: TenderCache, document_text: str = "") -> float:
    if not profile:
        return 0.0

    capabilities = normalize_list_text(profile.capabilities_text)
    locations = normalize_list_text(profile.locations_text)

    blob = " ".join([
        tender.title or "",
        tender.description or "",
        tender.industry or "",
        tender.tender_type or "",
        tender.province or "",
        tender.buyer_name or "",
        document_text or "",
    ]).lower()

    score = 18.0

    if profile.industry and tender.industry and profile.industry.lower() == (tender.industry or "").lower():
        score += 26.0
    elif profile.industry and profile.industry.lower() in blob:
        score += 12.0

    match_count = 0
    strong_matches = []
    for capability in capabilities:
        low = capability.lower()
        if low in blob:
            match_count += 1
            strong_matches.append(capability)
    score += min(match_count * 7.0, 36.0)

    if tender.province and any(loc.lower() == tender.province.lower() for loc in locations):
        score += 8.0

    try:
        if tender.closing_date:
            days = (tender.closing_date - date.today()).days
            if days < 0:
                score -= 20.0
            elif days <= 3:
                score += 1.0
            elif days <= 14:
                score += 4.0
            else:
                score += 6.0
    except Exception:
        pass

    proposal_required = bool(re.search(r"\b(proposal|rfp|request for proposal)\b", document_text or "", re.I))
    if proposal_required:
        score += 4.0

    return max(0.0, min(score, 100.0))


def fit_band_from_score(score: Optional[float]) -> Optional[str]:
    if score is None:
        return None
    if score >= 80:
        return "high_potential"
    if score >= 55:
        return "possible_fit"
    return "low_fit"


def get_latest_document(session, tender_id: int) -> Optional[TenderDocumentCache]:
    return session.execute(
        select(TenderDocumentCache)
        .where(TenderDocumentCache.tender_id == tender_id)
        .order_by(desc(TenderDocumentCache.fetched_at), desc(TenderDocumentCache.id))
        .limit(1)
    ).scalars().first()


def latest_analysis_for(session, user_id: int, tender_id: int):
    job = session.execute(
        select(AnalysisJob)
        .where(AnalysisJob.user_id == user_id, AnalysisJob.tender_id == tender_id)
        .order_by(desc(AnalysisJob.updated_at))
        .limit(1)
    ).scalars().first()
    if not job:
        return None
    raw = safe_loads(job.raw_result_json, {})
    return {
        "id": job.id,
        "status": job.status,
        "score": job.score,
        "summary": job.summary,
        "document_match": raw.get("document_match"),
        "document_match_reason": raw.get("document_match_reason"),
        "briefing_date": raw.get("briefing_date"),
        "contact_email": raw.get("contact_email"),
        "contact_phone": raw.get("contact_phone"),
        "proposal_required": raw.get("proposal_required"),
        "scope_summary": raw.get("scope_summary"),
        "strengths": raw.get("strengths") or [],
        "risks": raw.get("risks") or [],
        "recommendations": raw.get("recommendations") or [],
        "document_fetch_status": raw.get("document_fetch_status"),
    }


def analyze_tender_for_profile(session, tender: TenderCache, profile: Profile) -> Dict[str, Any]:
    doc = get_latest_document(session, tender.id)
    document_text = (doc.extracted_text or "").strip() if doc else ""

    score = keyword_overlap_score(profile, tender, document_text)
    fit_band = fit_band_from_score(score)

    capabilities = normalize_list_text(profile.capabilities_text)
    locations = normalize_list_text(profile.locations_text)
    tender_blob = " ".join([
        tender.title or "",
        tender.description or "",
        tender.industry or "",
        tender.tender_type or "",
        tender.province or "",
        tender.buyer_name or "",
        document_text or "",
    ]).lower()

    matched_capabilities = [cap for cap in capabilities if cap.lower() in tender_blob][:6]
    strengths: List[str] = []
    risks: List[str] = []
    recommendations: List[str] = []

    if profile.industry and (
        (tender.industry and profile.industry.lower() == (tender.industry or "").lower())
        or profile.industry.lower() in tender_blob
    ):
        strengths.append(f"Industry alignment detected around {profile.industry}.")
    if matched_capabilities:
        strengths.append("Capability overlap found: " + ", ".join(matched_capabilities[:4]) + ".")
    if tender.province and any(loc.lower() == tender.province.lower() for loc in locations):
        strengths.append(f"Location alignment found in {tender.province}.")
    if doc and doc.fetch_status == "fetched" and document_text:
        strengths.append("Tender document text is available for deeper review.")

    if not matched_capabilities:
        risks.append("No strong capability matches were detected from the active profile.")
    if not doc:
        risks.append("No tender document has been cached yet, so analysis relies on listing metadata.")
    elif doc.fetch_status not in {"fetched", "fetched_no_text"} and not document_text:
        risks.append(f"Document fetch status is {doc.fetch_status}, limiting confidence.")
    elif doc.fetch_status == "fetched_no_text":
        risks.append("A document was fetched but readable text extraction was limited.")
    if tender.closing_date:
        try:
            days_left = (tender.closing_date - date.today()).days
            if days_left < 0:
                risks.append("This tender appears to be closed already.")
            elif days_left <= 3:
                risks.append("Very little time remains before closing.")
        except Exception:
            pass

    if fit_band == "high_potential":
        recommendations.append("Proceed with a bid/no-bid review and assign an owner immediately.")
    elif fit_band == "possible_fit":
        recommendations.append("Review scope against your strongest delivery examples before deciding.")
    else:
        recommendations.append("Treat this as low priority unless strategic or relationship value exists.")

    if not doc:
        recommendations.append("Fetch the tender document before making a final pursuit decision.")
    if EMAIL_RE.search(document_text or "") or PHONE_RE.search(document_text or ""):
        recommendations.append("Use extracted contact details to clarify requirements if needed.")

    summary = (
        "Strong fit based on profile alignment and available tender information."
        if fit_band == "high_potential"
        else "Possible fit, but a focused commercial and capability check is recommended."
        if fit_band == "possible_fit"
        else "Low fit based on current profile alignment and available tender evidence."
    )

    email_match = EMAIL_RE.search(document_text or "")
    phone_match = PHONE_RE.search(document_text or "")
    briefing_match = BRIEFING_RE.search(document_text or "")

    return {
        "score": score,
        "fit_band": fit_band,
        "summary": summary,
        "document_match": bool(document_text),
        "document_match_reason": (
            "Readable tender document text was found and included in the analysis."
            if document_text
            else "No readable tender document text was available, so the analysis relied mostly on listing metadata."
        ),
        "scope_summary": extract_scope_summary(tender, document_text),
        "briefing_date": briefing_match.group(1).replace("/", "-") if briefing_match else None,
        "contact_email": email_match.group(1) if email_match else None,
        "contact_phone": phone_match.group(1) if phone_match else None,
        "proposal_required": bool(re.search(r"\b(proposal|rfp|request for proposal)\b", document_text or "", re.I)),
        "strengths": strengths[:5],
        "risks": risks[:5],
        "recommendations": recommendations[:5],
        "document_fetch_status": doc.fetch_status if doc else None,
    }
