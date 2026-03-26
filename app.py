import json
import os
import tempfile
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        return False

import requests
from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from pypdf import PdfReader
from sqlalchemy import desc, select, text
from docx import Document as DocxDocument

from database import get_db_session, init_db
from models import (
    AnalysisJob,
    IngestRun,
    Profile,
    ProfileIssue,
    TenderCache,
    TenderDocumentCache,
)
from services.etenders_ingest import ingest_tenders

load_dotenv()

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "tenderai-dev-fallback-secret")
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024

init_db()

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")


# -------------------------------------------------------------------
# General helpers
# -------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@app.template_filter("days_left")
def days_left_filter(value):
    if not value:
        return "n/a"

    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value).date()
        except Exception:
            return "n/a"

    if isinstance(value, datetime):
        value = value.date()

    if not isinstance(value, date):
        return "n/a"

    delta = (value - date.today()).days
    if delta < 0:
        return f"Closed {abs(delta)} day(s) ago"
    if delta == 0:
        return "Closes today"
    return f"{delta} day(s)"


def require_admin(req):
    configured = (os.getenv("ADMIN_TOKEN") or "").strip()
    provided = (
        req.args.get("token")
        or req.headers.get("X-Admin-Token")
        or req.headers.get("x-admin-token")
        or ""
    ).strip()

    if configured and provided != configured:
        abort(403)


def get_openai_client():
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key or OpenAI is None:
        return None
    try:
        return OpenAI(api_key=api_key)
    except Exception:
        return None


def safe_json_loads(value: Any, fallback=None):
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


def get_active_profile(session) -> Optional[Profile]:
    return session.execute(
        select(Profile).where(Profile.is_active.is_(True)).order_by(desc(Profile.updated_at)).limit(1)
    ).scalar_one_or_none()


def serialize_profile(profile: Profile, include_issues: bool = False) -> Dict[str, Any]:
    data = {
        "id": profile.id,
        "filename": getattr(profile, "filename", None),
        "industry": getattr(profile, "industry", None),
        "is_active": getattr(profile, "is_active", False),
        "updated_at": profile.updated_at.isoformat() if getattr(profile, "updated_at", None) else None,
        "parsed_json": safe_json_loads(getattr(profile, "parsed_json", None), {}),
        "has_parsed_json": bool(getattr(profile, "parsed_json", None)),
        "text_len": len((getattr(profile, "extracted_text", None) or "")),
    }
    if include_issues:
        issues = getattr(profile, "issues", None) or []
        data["issues"] = [
            {
                "id": issue.id,
                "message": getattr(issue, "message", None),
                "severity": getattr(issue, "severity", None),
            }
            for issue in issues
        ]
    return data


def tender_to_view_model(tender: TenderCache) -> Dict[str, Any]:
    return {
        "id": tender.id,
        "tender_uid": getattr(tender, "tender_uid", None),
        "ocid": getattr(tender, "ocid", None),
        "title": tender.title,
        "description": tender.description,
        "buyer_name": getattr(tender, "buyer_name", None),
        "province": getattr(tender, "province", None),
        "tender_type": getattr(tender, "tender_type", None),
        "industry": getattr(tender, "industry", None),
        "status": getattr(tender, "status", None),
        "issued_date": tender.issued_date.isoformat() if getattr(tender, "issued_date", None) else None,
        "closing_date": tender.closing_date.isoformat() if getattr(tender, "closing_date", None) else None,
        "document_url": getattr(tender, "document_url", None),
        "source_url": getattr(tender, "source_url", None),
        "is_live": getattr(tender, "is_live", None),
        "updated_at": tender.updated_at.isoformat() if getattr(tender, "updated_at", None) else None,
    }


def extract_pdf_text(path: str) -> str:
    try:
        reader = PdfReader(path)
        pages = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(pages).strip()
    except Exception:
        return ""


def extract_docx_text(path: str) -> str:
    try:
        doc = DocxDocument(path)
        return "\n".join([p.text for p in doc.paragraphs if p.text]).strip()
    except Exception:
        return ""


def extract_uploaded_text(path: str, filename: str) -> str:
    lower = (filename or "").lower()
    if lower.endswith(".pdf"):
        return extract_pdf_text(path)
    if lower.endswith(".docx"):
        return extract_docx_text(path)
    return ""


def upsert_profile_issues(session, profile: Profile, parsed: Dict[str, Any]):
    session.execute(
        text("DELETE FROM profile_issues WHERE profile_id = :profile_id"),
        {"profile_id": profile.id},
    )

    issues: List[Tuple[str, str]] = []

    capabilities = parsed.get("capabilities") or []
    locations = parsed.get("locations") or []
    industry = parsed.get("industry") or ""

    if not capabilities:
        issues.append(("warning", "No clear capabilities/services were extracted from the profile."))
    if not locations:
        issues.append(("info", "No business locations or operating provinces were extracted."))
    if not industry:
        issues.append(("info", "No primary industry was extracted from the profile."))

    for severity, message in issues:
        issue = ProfileIssue()
        if hasattr(issue, "profile_id"):
            issue.profile_id = profile.id
        if hasattr(issue, "severity"):
            issue.severity = severity
        if hasattr(issue, "message"):
            issue.message = message
        session.add(issue)


# -------------------------------------------------------------------
# OpenAI helpers
# -------------------------------------------------------------------

def parse_supplier_profile_text(text_value: str) -> Dict[str, Any]:
    client = get_openai_client()
    if not client or not (text_value or "").strip():
        return {}

    prompt = f"""
You are extracting structured business profile data for tender matching.
Return JSON only.

Schema:
{{
  "company_name": "string",
  "industry": "string",
  "capabilities": ["string"],
  "locations": ["string"],
  "certifications": ["string"],
  "documents_mentioned": ["string"],
  "years_experience": "string",
  "summary": "string"
}}

Business profile text:
{text_value[:25000]}
""".strip()

    try:
        response = client.responses.create(
            model=OPENAI_MODEL,
            input=prompt,
        )
        raw = response.output_text
        parsed = json.loads(raw)
        parsed["_parse_mode"] = "openai"
        parsed["_parsed_at"] = utcnow().isoformat()
        return parsed
    except Exception:
        return {}


def heuristic_profile_parse(text_value: str, filename: str = "") -> Dict[str, Any]:
    lower = (text_value or "").lower()

    capabilities = []
    for phrase in [
        "construction",
        "cleaning",
        "maintenance",
        "security",
        "it support",
        "software",
        "consulting",
        "training",
        "transport",
        "logistics",
        "catering",
        "engineering",
        "electrical",
        "civil",
        "supply",
    ]:
        if phrase in lower:
            capabilities.append(phrase)

    locations = []
    for province in [
        "gauteng",
        "kwazulu-natal",
        "western cape",
        "eastern cape",
        "limpopo",
        "mpumalanga",
        "free state",
        "north west",
        "northern cape",
    ]:
        if province in lower:
            locations.append(province.title())

    industry = ""
    if "construction" in lower or "civil" in lower or "electrical" in lower:
        industry = "Construction"
    elif "it" in lower or "software" in lower or "technology" in lower:
        industry = "Technology"
    elif "cleaning" in lower or "facilities" in lower:
        industry = "Facilities"
    elif "transport" in lower or "logistics" in lower:
        industry = "Transport"

    return {
        "company_name": os.path.splitext(filename)[0] if filename else "Uploaded Profile",
        "industry": industry,
        "capabilities": sorted(list(set(capabilities))),
        "locations": sorted(list(set(locations))),
        "certifications": [],
        "documents_mentioned": [],
        "years_experience": "",
        "summary": "Heuristic extraction was used because OpenAI parsing was unavailable.",
        "_parse_mode": "heuristic",
        "_parsed_at": utcnow().isoformat(),
    }


def parse_profile_text(text_value: str, filename: str = "") -> Dict[str, Any]:
    parsed = parse_supplier_profile_text(text_value)
    if parsed:
        return parsed
    return heuristic_profile_parse(text_value, filename)


def analyze_tender_against_profile(
    tender: TenderCache,
    profile: Profile,
    document_text: str,
) -> Dict[str, Any]:
    profile_json = safe_json_loads(profile.parsed_json, {})
    client = get_openai_client()

    if client and (document_text or "").strip():
        prompt = f"""
You are TenderAI, a procurement intelligence assistant.
Analyze the tender document against the supplier profile.
Return JSON only.

Schema:
{{
  "score": 0,
  "summary": "string",
  "strengths": ["string"],
  "gaps": ["string"],
  "risks": ["string"],
  "recommendation": "string"
}}

Supplier profile JSON:
{json.dumps(profile_json, ensure_ascii=False)[:12000]}

Tender metadata:
{json.dumps(tender_to_view_model(tender), ensure_ascii=False)[:8000]}

Tender document text:
{document_text[:25000]}
""".strip()

        try:
            response = client.responses.create(
                model=OPENAI_MODEL,
                input=prompt,
            )
            raw = response.output_text
            parsed = json.loads(raw)
            parsed["_analysis_mode"] = "openai"
            parsed["_analyzed_at"] = utcnow().isoformat()
            return parsed
        except Exception:
            pass

    # Fallback heuristic comparison
    score, reasons, band = prequalify_tender_against_profile(tender, profile)
    recommendation = "Proceed with deeper review." if score >= 60 else "Review carefully before committing resources."

    return {
        "score": score,
        "summary": "This analysis used metadata and available extracted text with fallback logic.",
        "strengths": reasons[:4],
        "gaps": ["Detailed AI interpretation was unavailable."] if get_openai_client() is None else [],
        "risks": [] if score >= 60 else ["Profile alignment appears limited from the available metadata."],
        "recommendation": recommendation,
        "_analysis_mode": "heuristic",
        "_analyzed_at": utcnow().isoformat(),
        "_band": band,
    }


# -------------------------------------------------------------------
# Prequalification helpers
# -------------------------------------------------------------------

def keyword_overlap_score(profile: Optional[Profile], tender: TenderCache) -> float:
    if not profile:
        return 0.0

    parsed = safe_json_loads(getattr(profile, "parsed_json", None), {})
    capabilities = [str(x).strip().lower() for x in (parsed.get("capabilities") or []) if str(x).strip()]
    locations = [str(x).strip().lower() for x in (parsed.get("locations") or []) if str(x).strip()]
    industry = str(parsed.get("industry") or getattr(profile, "industry", "") or "").strip().lower()

    haystack = f"{tender.title or ''} {tender.description or ''}".lower()
    tender_province = (getattr(tender, "province", None) or "").lower()
    tender_industry = (getattr(tender, "industry", None) or "").lower()

    score = 0.0

    matched_caps = 0
    for cap in capabilities[:20]:
        if cap and cap in haystack:
            matched_caps += 1
    score += min(matched_caps * 12, 60)

    if industry and tender_industry and industry == tender_industry:
        score += 20

    if locations and tender_province and any(loc in tender_province or tender_province in loc for loc in locations):
        score += 15

    if getattr(tender, "closing_date", None):
        try:
            days = (tender.closing_date - date.today()).days
            if days >= 0:
                score += 5
        except Exception:
            pass

    return min(score, 100.0)


def prequalify_tender_against_profile(tender: TenderCache, profile: Optional[Profile]):
    score = keyword_overlap_score(profile, tender)
    reasons: List[str] = []

    if profile:
        parsed = safe_json_loads(profile.parsed_json, {})
        capabilities = [str(x).strip() for x in (parsed.get("capabilities") or []) if str(x).strip()]
        locations = [str(x).strip() for x in (parsed.get("locations") or []) if str(x).strip()]
        profile_industry = (parsed.get("industry") or getattr(profile, "industry", "") or "").strip().lower()

        title_desc = f"{tender.title or ''} {tender.description or ''}".lower()
        matched_caps = [cap for cap in capabilities if cap.lower() in title_desc]
        if matched_caps:
            reasons.append(f"Matches your service keywords: {', '.join(matched_caps[:4])}")

        tender_industry = (getattr(tender, "industry", None) or "").strip().lower()
        if profile_industry and tender_industry and profile_industry == tender_industry:
            reasons.append(f"Industry alignment with {tender.industry}")

        tender_province = (getattr(tender, "province", None) or "").strip().lower()
        if locations and tender_province and any(loc.lower() in tender_province or tender_province in loc.lower() for loc in locations):
            reasons.append(f"Location alignment in {tender.province}")

        if getattr(tender, "buyer_name", None):
            reasons.append(f"Issued by {tender.buyer_name}")

    if score >= 75:
        band = "high_potential"
    elif score >= 45:
        band = "possible_fit"
    else:
        band = "low_fit"

    return score, reasons, band


def build_prequal_summary(score: float, reasons: List[str]) -> str:
    if reasons:
        return "This tender could be a good fit because " + "; ".join(reasons[:3]) + "."
    if score >= 60:
        return "This tender shows promising metadata alignment with your active profile."
    return "This tender has limited visible alignment from metadata alone and may require careful review."


# -------------------------------------------------------------------
# Document fetching
# -------------------------------------------------------------------

def fetch_tender_document(session, tender: TenderCache) -> Dict[str, Any]:
    url = getattr(tender, "document_url", None) or getattr(tender, "source_url", None)
    if not url:
        return {"ok": False, "error": "No document or source URL available."}

    existing = session.execute(
        select(TenderDocumentCache)
        .where(TenderDocumentCache.tender_id == tender.id)
        .order_by(desc(TenderDocumentCache.updated_at))
        .limit(1)
    ).scalar_one_or_none()

    try:
        response = requests.get(url, timeout=20, allow_redirects=True)
        response.raise_for_status()
        content_type = (response.headers.get("Content-Type") or "").lower()

        suffix = ".pdf"
        if "wordprocessingml" in content_type or url.lower().endswith(".docx"):
            suffix = ".docx"
        elif "pdf" not in content_type and not url.lower().endswith(".pdf"):
            suffix = ".bin"

        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
            temp_file.write(response.content)
            temp_path = temp_file.name

        extracted_text = ""
        if suffix == ".pdf":
            extracted_text = extract_pdf_text(temp_path)
        elif suffix == ".docx":
            extracted_text = extract_docx_text(temp_path)

        if existing:
            doc = existing
        else:
            doc = TenderDocumentCache()
            if hasattr(doc, "tender_id"):
                doc.tender_id = tender.id
            session.add(doc)

        if hasattr(doc, "fetch_status"):
            doc.fetch_status = "fetched"
        if hasattr(doc, "content_type"):
            doc.content_type = content_type[:255]
        if hasattr(doc, "extracted_text"):
            doc.extracted_text = extracted_text
        if hasattr(doc, "error_message"):
            doc.error_message = None
        if hasattr(doc, "updated_at"):
            doc.updated_at = utcnow()

        session.flush()

        try:
            os.unlink(temp_path)
        except Exception:
            pass

        return {"ok": True, "text_len": len(extracted_text), "content_type": content_type}

    except Exception as exc:
        if existing:
            doc = existing
        else:
            doc = TenderDocumentCache()
            if hasattr(doc, "tender_id"):
                doc.tender_id = tender.id
            session.add(doc)

        if hasattr(doc, "fetch_status"):
            doc.fetch_status = "fetch_failed"
        if hasattr(doc, "error_message"):
            doc.error_message = str(exc)
        if hasattr(doc, "updated_at"):
            doc.updated_at = utcnow()

        session.flush()
        return {"ok": False, "error": str(exc)}


# -------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------

@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "tenderai"})


@app.get("/")
def home():
    with get_db_session() as session:
        tenders = session.execute(
            select(TenderCache).order_by(desc(TenderCache.updated_at)).limit(20)
        ).scalars().all()
        active_profile = get_active_profile(session)

        tender_cards = []
        for tender in tenders:
            score, reasons, band = prequalify_tender_against_profile(tender, active_profile)
            card = tender_to_view_model(tender)
            card["alignment_score"] = score
            card["fit_band"] = band
            card["fit_reasons"] = reasons
            tender_cards.append(card)

        return render_template(
            "home.html",
            tenders=tender_cards,
            active_profile=serialize_profile(active_profile) if active_profile else None,
        )


@app.get("/tenders")
def tenders():
    province = (request.args.get("province") or "").strip()
    industry = (request.args.get("industry") or "").strip()
    q = (request.args.get("q") or "").strip().lower()

    with get_db_session() as session:
        query = select(TenderCache).order_by(desc(TenderCache.updated_at))
        records = session.execute(query).scalars().all()

        filtered = []
        for tender in records:
            if province and province.lower() not in ((tender.province or "").lower()):
                continue
            if industry and industry.lower() not in ((tender.industry or "").lower()):
                continue
            haystack = f"{tender.title or ''} {tender.description or ''} {(tender.buyer_name or '')}".lower()
            if q and q not in haystack:
                continue
            filtered.append(tender)

        active_profile = get_active_profile(session)

        tender_cards = []
        for tender in filtered:
            score, reasons, band = prequalify_tender_against_profile(tender, active_profile)
            card = tender_to_view_model(tender)
            card["alignment_score"] = score
            card["fit_band"] = band
            card["fit_reasons"] = reasons
            tender_cards.append(card)

        return render_template(
            "tenders.html",
            tenders=tender_cards,
            active_profile=serialize_profile(active_profile) if active_profile else None,
        )


@app.get("/tender/<int:tender_id>")
def tender_detail(tender_id: int):
    with get_db_session() as session:
        tender = session.get(TenderCache, tender_id)
        if not tender:
            abort(404)

        active_profile = get_active_profile(session)
        score, fit_reasons, fit_band = prequalify_tender_against_profile(tender, active_profile)
        fit_summary = build_prequal_summary(score, fit_reasons)

        latest_job = None
        if active_profile:
            latest_job = session.execute(
                select(AnalysisJob)
                .where(
                    AnalysisJob.tender_id == tender_id,
                    AnalysisJob.profile_id == active_profile.id,
                )
                .order_by(desc(AnalysisJob.updated_at))
                .limit(1)
            ).scalar_one_or_none()

        latest_analysis = None
        if latest_job:
            latest_analysis = {
                "id": latest_job.id,
                "status": getattr(latest_job, "status", None),
                "score": getattr(latest_job, "score", None),
                "summary": getattr(latest_job, "summary", None),
                "strengths": [x.strip() for x in (getattr(latest_job, "strengths_text", None) or "").split("\n") if x.strip()],
                "gaps": [x.strip() for x in (getattr(latest_job, "gaps_text", None) or "").split("\n") if x.strip()]
                if hasattr(latest_job, "gaps_text") else [],
                "risks": [x.strip() for x in (getattr(latest_job, "risks_text", None) or "").split("\n") if x.strip()],
                "recommendation": getattr(latest_job, "recommendations_text", None),
                "updated_at": latest_job.updated_at.isoformat() if getattr(latest_job, "updated_at", None) else None,
            }

        return render_template(
            "tender_detail.html",
            tender=tender_to_view_model(tender),
            alignment_score=score,
            fit_summary=fit_summary,
            fit_reasons=fit_reasons,
            fit_band=fit_band,
            can_analyze=bool(active_profile),
            analyze_action_url=url_for("analyze_tender_page", tender_id=tender_id),
            latest_analysis=latest_analysis,
        )


@app.post("/tender/<int:tender_id>/analyze")
def analyze_tender_page(tender_id: int):
    with get_db_session() as session:
        tender = session.get(TenderCache, tender_id)
        if not tender:
            flash("Tender not found.", "error")
            return redirect(url_for("tenders"))

        active_profile = get_active_profile(session)
        if not active_profile:
            flash("Please upload and activate a business profile first.", "error")
            return redirect(url_for("profiles"))

        doc = session.execute(
            select(TenderDocumentCache)
            .where(TenderDocumentCache.tender_id == tender_id)
            .order_by(desc(TenderDocumentCache.updated_at))
            .limit(1)
        ).scalar_one_or_none()

        if not doc or (getattr(doc, "fetch_status", None) != "fetched"):
            result = fetch_tender_document(session, tender)
            if not result.get("ok"):
                flash(f"Unable to fetch tender document: {result.get('error')}", "error")
                return redirect(url_for("tender_detail", tender_id=tender_id))
            doc = session.execute(
                select(TenderDocumentCache)
                .where(TenderDocumentCache.tender_id == tender_id)
                .order_by(desc(TenderDocumentCache.updated_at))
                .limit(1)
            ).scalar_one_or_none()

        extracted_text = (getattr(doc, "extracted_text", None) or "").strip()
        if not extracted_text:
            flash("Tender document was fetched, but no readable text was extracted.", "error")
            return redirect(url_for("tender_detail", tender_id=tender_id))

        analysis = analyze_tender_against_profile(tender, active_profile, extracted_text)

        job = AnalysisJob()
        if hasattr(job, "profile_id"):
            job.profile_id = active_profile.id
        if hasattr(job, "tender_id"):
            job.tender_id = tender_id
        if hasattr(job, "status"):
            job.status = "completed"
        if hasattr(job, "score"):
            try:
                job.score = float(analysis.get("score") or 0)
            except Exception:
                job.score = 0
        if hasattr(job, "summary"):
            job.summary = analysis.get("summary")
        if hasattr(job, "strengths_text"):
            job.strengths_text = "\n".join(analysis.get("strengths") or [])
        if hasattr(job, "risks_text"):
            job.risks_text = "\n".join(analysis.get("risks") or [])
        if hasattr(job, "recommendations_text"):
            job.recommendations_text = analysis.get("recommendation")
        if hasattr(job, "updated_at"):
            job.updated_at = utcnow()

        session.add(job)

        if hasattr(doc, "parsed_json"):
            doc.parsed_json = json.dumps(analysis, ensure_ascii=False)
        if hasattr(doc, "updated_at"):
            doc.updated_at = utcnow()

        session.flush()

    flash("Tender analysis completed.", "success")
    return redirect(url_for("tender_detail", tender_id=tender_id))


@app.get("/profiles")
def profiles():
    with get_db_session() as session:
        profiles_list = session.execute(
            select(Profile).order_by(desc(Profile.updated_at))
        ).scalars().all()

        return render_template(
            "profiles.html",
            profiles=[serialize_profile(profile, include_issues=True) for profile in profiles_list],
        )


@app.post("/profiles")
def upload_profile():
    uploaded = request.files.get("profile_file") or request.files.get("file")
    if not uploaded or not uploaded.filename:
        flash("Please choose a profile document to upload.", "error")
        return redirect(url_for("profiles"))

    filename = uploaded.filename

    with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(filename)[1] or ".pdf") as temp_file:
        uploaded.save(temp_file.name)
        temp_path = temp_file.name

    extracted_text = extract_uploaded_text(temp_path, filename)
    parsed = parse_profile_text(extracted_text, filename)

    try:
        with get_db_session() as session:
            profile = Profile()
            if hasattr(profile, "filename"):
                profile.filename = filename
            if hasattr(profile, "extracted_text"):
                profile.extracted_text = extracted_text
            if hasattr(profile, "parsed_json"):
                profile.parsed_json = json.dumps(parsed, ensure_ascii=False)
            if hasattr(profile, "industry"):
                profile.industry = parsed.get("industry") or None
            if hasattr(profile, "is_active"):
                current_active = get_active_profile(session)
                profile.is_active = current_active is None
            if hasattr(profile, "updated_at"):
                profile.updated_at = utcnow()

            session.add(profile)
            session.flush()

            upsert_profile_issues(session, profile, parsed)

            parse_mode = parsed.get("_parse_mode", "heuristic")
            flash(f"Profile uploaded successfully. Parse mode: {parse_mode}.", "success")
    finally:
        try:
            os.unlink(temp_path)
        except Exception:
            pass

    return redirect(url_for("profiles"))


@app.post("/profiles/<int:profile_id>/activate")
def activate_profile(profile_id: int):
    with get_db_session() as session:
        target = session.get(Profile, profile_id)
        if not target:
            flash("Profile not found.", "error")
            return redirect(url_for("profiles"))

        profiles_list = session.execute(select(Profile)).scalars().all()
        for profile in profiles_list:
            if hasattr(profile, "is_active"):
                profile.is_active = profile.id == profile_id

        session.flush()

    flash("Profile activated.", "success")
    return redirect(url_for("profiles"))


@app.post("/profiles/<int:profile_id>/delete")
def delete_profile(profile_id: int):
    with get_db_session() as session:
        target = session.get(Profile, profile_id)
        if not target:
            flash("Profile not found.", "error")
            return redirect(url_for("profiles"))

        session.delete(target)
        session.flush()

    flash("Profile deleted.", "success")
    return redirect(url_for("profiles"))


# -------------------------------------------------------------------
# JSON endpoints
# -------------------------------------------------------------------

@app.get("/api/tenders/prequalified")
def api_prequalified_tenders():
    band_filter = (request.args.get("band") or "").strip().lower()
    limit = max(1, min(int(request.args.get("limit", 20)), 100))

    with get_db_session() as session:
        active_profile = get_active_profile(session)
        tenders = session.execute(
            select(TenderCache).order_by(desc(TenderCache.updated_at)).limit(200)
        ).scalars().all()

        results = []
        for tender in tenders:
            score, reasons, band = prequalify_tender_against_profile(tender, active_profile)
            if band_filter and band != band_filter:
                continue

            item = tender_to_view_model(tender)
            item["alignment_score"] = score
            item["fit_reasons"] = reasons
            item["fit_band"] = band
            item["fit_summary"] = build_prequal_summary(score, reasons)
            results.append(item)

        results.sort(key=lambda x: x.get("alignment_score", 0), reverse=True)

        return jsonify(
            {
                "ok": True,
                "active_profile": serialize_profile(active_profile) if active_profile else None,
                "count": min(len(results), limit),
                "items": results[:limit],
            }
        )


@app.get("/api/tenders/<int:tender_id>/analyze")
def api_analyze_tender(tender_id: int):
    with get_db_session() as session:
        tender = session.get(TenderCache, tender_id)
        if not tender:
            return jsonify({"ok": False, "error": "Tender not found."}), 404

        active_profile = get_active_profile(session)
        if not active_profile:
            return jsonify({"ok": False, "error": "No active profile found."}), 400

        doc = session.execute(
            select(TenderDocumentCache)
            .where(TenderDocumentCache.tender_id == tender_id)
            .order_by(desc(TenderDocumentCache.updated_at))
            .limit(1)
        ).scalar_one_or_none()

        if not doc or (getattr(doc, "fetch_status", None) != "fetched"):
            fetch_result = fetch_tender_document(session, tender)
            if not fetch_result.get("ok"):
                return jsonify({"ok": False, "error": fetch_result.get("error")}), 400
            doc = session.execute(
                select(TenderDocumentCache)
                .where(TenderDocumentCache.tender_id == tender_id)
                .order_by(desc(TenderDocumentCache.updated_at))
                .limit(1)
            ).scalar_one_or_none()

        extracted_text = (getattr(doc, "extracted_text", None) or "").strip()
        if not extracted_text:
            return jsonify({"ok": False, "error": "No extracted document text available."}), 400

        analysis = analyze_tender_against_profile(tender, active_profile, extracted_text)
        return jsonify({"ok": True, "analysis": analysis})


@app.get("/api/admin/openai-status")
def admin_openai_status():
    require_admin(request)
    client = get_openai_client()
    return jsonify(
        {
            "ok": True,
            "api_key_present": bool((os.getenv("OPENAI_API_KEY") or "").strip()),
            "client_ready": client is not None,
            "model": OPENAI_MODEL,
        }
    )


@app.get("/api/admin/run-ingest")
def admin_run_ingest():
    require_admin(request)
    with get_db_session() as session:
        result = ingest_tenders(
            session=session,
            max_pages=int(os.getenv("INGEST_MAX_PAGES", "1")),
        )
    return jsonify(result)


@app.get("/api/admin/fetch-documents")
def admin_fetch_documents():
    require_admin(request)
    limit = max(1, min(int(request.args.get("limit", 20)), 100))

    fetched = 0
    errors = []

    with get_db_session() as session:
        tenders = session.execute(
            select(TenderCache).order_by(desc(TenderCache.updated_at)).limit(limit)
        ).scalars().all()

        for tender in tenders:
            result = fetch_tender_document(session, tender)
            if result.get("ok"):
                fetched += 1
            else:
                errors.append({"tender_id": tender.id, "error": result.get("error")})

    return jsonify({"ok": True, "requested": limit, "fetched": fetched, "errors": errors})


@app.get("/api/admin/parse-documents")
def admin_parse_documents():
    require_admin(request)

    tender_id = request.args.get("tender_id", type=int)
    if not tender_id:
        return jsonify(
            {
                "ok": False,
                "error": "Bulk parsing is disabled. Provide tender_id and use on-demand analysis only.",
            }
        ), 400

    with get_db_session() as session:
        tender = session.get(TenderCache, tender_id)
        if not tender:
            return jsonify({"ok": False, "error": "Tender not found."}), 404

        active_profile = get_active_profile(session)
        if not active_profile:
            return jsonify({"ok": False, "error": "No active profile found."}), 400

        doc = session.execute(
            select(TenderDocumentCache)
            .where(TenderDocumentCache.tender_id == tender_id)
            .order_by(desc(TenderDocumentCache.updated_at))
            .limit(1)
        ).scalar_one_or_none()

        if not doc:
            fetch_result = fetch_tender_document(session, tender)
            if not fetch_result.get("ok"):
                return jsonify({"ok": False, "error": fetch_result.get("error")}), 400
            doc = session.execute(
                select(TenderDocumentCache)
                .where(TenderDocumentCache.tender_id == tender_id)
                .order_by(desc(TenderDocumentCache.updated_at))
                .limit(1)
            ).scalar_one_or_none()

        extracted_text = (getattr(doc, "extracted_text", None) or "").strip()
        if not extracted_text:
            return jsonify({"ok": False, "error": "No extracted text available to parse."}), 400

        analysis = analyze_tender_against_profile(tender, active_profile, extracted_text)

        if hasattr(doc, "parsed_json"):
            doc.parsed_json = json.dumps(analysis, ensure_ascii=False)
        if hasattr(doc, "updated_at"):
            doc.updated_at = utcnow()

        job = AnalysisJob()
        if hasattr(job, "profile_id"):
            job.profile_id = active_profile.id
        if hasattr(job, "tender_id"):
            job.tender_id = tender_id
        if hasattr(job, "status"):
            job.status = "completed"
        if hasattr(job, "score"):
            try:
                job.score = float(analysis.get("score") or 0)
            except Exception:
                job.score = 0
        if hasattr(job, "summary"):
            job.summary = analysis.get("summary")
        if hasattr(job, "strengths_text"):
            job.strengths_text = "\n".join(analysis.get("strengths") or [])
        if hasattr(job, "risks_text"):
            job.risks_text = "\n".join(analysis.get("risks") or [])
        if hasattr(job, "recommendations_text"):
            job.recommendations_text = analysis.get("recommendation")
        if hasattr(job, "updated_at"):
            job.updated_at = utcnow()

        session.add(job)
        session.flush()

        return jsonify({"ok": True, "tender_id": tender_id, "analysis": analysis})


@app.get("/api/admin/reparse-profile/<int:profile_id>")
def admin_reparse_profile(profile_id: int):
    require_admin(request)

    with get_db_session() as session:
        profile = session.get(Profile, profile_id)
        if not profile:
            return jsonify({"ok": False, "error": "Profile not found."}), 404

        extracted_text = getattr(profile, "extracted_text", None) or ""
        parsed = parse_profile_text(extracted_text, getattr(profile, "filename", "") or "")

        if hasattr(profile, "parsed_json"):
            profile.parsed_json = json.dumps(parsed, ensure_ascii=False)
        if hasattr(profile, "industry"):
            profile.industry = parsed.get("industry") or None
        if hasattr(profile, "updated_at"):
            profile.updated_at = utcnow()

        upsert_profile_issues(session, profile, parsed)
        session.flush()

        return jsonify({"ok": True, "profile_id": profile_id, "parsed": parsed})


@app.get("/api/admin/document-cache-debug")
def admin_document_cache_debug():
    require_admin(request)
    limit = max(1, min(int(request.args.get("limit", 20)), 100))

    with get_db_session() as session:
        rows = session.execute(
            text(
                """
                SELECT id,
                       tender_id,
                       fetch_status,
                       content_type,
                       LENGTH(COALESCE(extracted_text, '')) AS text_len,
                       CASE WHEN parsed_json IS NOT NULL THEN true ELSE false END AS has_parsed_json,
                       error_message,
                       updated_at
                FROM tender_documents_cache
                ORDER BY updated_at DESC
                LIMIT :limit
                """
            ),
            {"limit": limit},
        ).mappings().all()

        return jsonify({"ok": True, "rows": [dict(r) for r in rows]})


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8080)
