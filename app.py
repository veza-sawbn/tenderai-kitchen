import json
import os
import re
import tempfile
from datetime import date, datetime, timezone
from functools import wraps

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from pypdf import PdfReader
from sqlalchemy import desc, func, or_, select
from werkzeug.security import check_password_hash, generate_password_hash

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        return False

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

from database import get_db_session, init_db
from models import (
    AnalysisJob,
    IngestRun,
    Profile,
    ProfileIssue,
    TenderCache,
    TenderDocumentCache,
    User,
    UserTenderDecision,
)
from services.document_fetcher import fetch_documents_for_tenders
from services.etenders_ingest import ingest_tenders

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "tenderai-dev-fallback-secret")

init_db()

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
OPENAI_TIMEOUT_SECONDS = int(os.getenv("OPENAI_TIMEOUT_SECONDS", "90"))


def utcnow():
    return datetime.now(timezone.utc)


def safe_loads(value, fallback=None):
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


def extract_pdf_text(file_or_path) -> str:
    temp_path = None
    try:
        if isinstance(file_or_path, str):
            path = file_or_path
        else:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                file_or_path.save(tmp.name)
                path = tmp.name
                temp_path = tmp.name

        reader = PdfReader(path)
        parts = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(parts).strip()
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except Exception:
                pass


def normalize_keywords(text: str):
    if not text:
        return []
    raw = text.replace("\n", ",").replace(";", ",").replace("|", ",").split(",")
    out = []
    seen = set()
    for item in raw:
        value = " ".join(item.strip().split())
        if len(value) >= 3 and value.lower() not in seen:
            seen.add(value.lower())
            out.append(value)
    return out[:20]


def parse_profile_text(text: str, filename: str | None = None) -> dict:
    lower = text.lower()
    industry = None
    rules = [
        ("Construction", ["construction", "contractor", "civil", "building"]),
        ("ICT", ["software", "ict", "technology", "digital", "it services"]),
        ("Transport", ["transport", "fleet", "vehicle", "logistics"]),
        ("Professional Services", ["consulting", "advisory", "professional services"]),
        ("Tourism", ["tourism", "travel", "adventure", "hospitality"]),
        ("Education", ["training provider", "education", "skills development"]),
    ]
    for label, words in rules:
        if any(word in lower for word in words):
            industry = label
            break

    capabilities = normalize_keywords(text[:3000])
    locations = []
    for province in [
        "Gauteng", "KwaZulu-Natal", "Western Cape", "Eastern Cape",
        "Free State", "Mpumalanga", "Limpopo", "North West", "Northern Cape",
    ]:
        if province.lower() in lower:
            locations.append(province)

    company_name = None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines:
        company_name = lines[0][:255]
    if not company_name and filename:
        company_name = os.path.splitext(filename)[0]

    issues = []
    if not industry:
        issues.append({
            "title": "Industry focus is unclear",
            "detail": "The profile does not strongly indicate its primary industry.",
            "penalty_weight": 5,
        })
    if not capabilities:
        issues.append({
            "title": "Capabilities are unclear",
            "detail": "The profile does not clearly list capabilities.",
            "penalty_weight": 6,
        })

    return {
        "company_name": company_name,
        "industry": industry,
        "capabilities": capabilities,
        "locations": locations,
        "issues": issues,
        "_parse_mode": "heuristic",
    }


def build_profile_gap_summary(profile: Profile | None) -> dict:
    if not profile:
        return {"pending_count": 0, "fixed_count": 0}
    pending = 0
    fixed = 0
    for issue in profile.issues or []:
        if issue.status == "fixed":
            fixed += 1
        else:
            pending += 1
    return {"pending_count": pending, "fixed_count": fixed}


def get_current_user(session_db):
    user_id = session.get("user_id")
    if not user_id:
        return None
    return session_db.get(User, user_id)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please sign in first.", "warning")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def get_active_profile(session_db, user_id: int):
    return session_db.execute(
        select(Profile)
        .where(Profile.user_id == user_id, Profile.is_active.is_(True))
        .order_by(desc(Profile.updated_at))
        .limit(1)
    ).scalars().first()


def serialize_profile(profile: Profile):
    return {
        "id": profile.id,
        "name": profile.name,
        "company_name": profile.company_name,
        "industry": profile.industry,
        "capabilities_text": profile.capabilities_text,
        "locations_text": profile.locations_text,
        "original_filename": profile.original_filename,
        "is_active": profile.is_active,
        "issues": [
            {
                "id": issue.id,
                "title": issue.title,
                "detail": issue.detail,
                "status": issue.status,
            }
            for issue in (profile.issues or [])
        ],
    }


def keyword_overlap_score(profile: Profile | None, tender: TenderCache):
    if not profile:
        return None

    capabilities = [c.strip() for c in (profile.capabilities_text or "").split(",") if c.strip()]
    locations = [c.strip() for c in (profile.locations_text or "").split(",") if c.strip()]
    score = 22.0

    blob = " ".join([
        tender.title or "",
        tender.description or "",
        tender.industry or "",
        tender.tender_type or "",
        tender.province or "",
        tender.buyer_name or "",
    ]).lower()

    if profile.industry and tender.industry and profile.industry.lower() == tender.industry.lower():
        score += 26.0
    elif profile.industry and profile.industry.lower() in blob:
        score += 12.0

    matches = 0
    for capability in capabilities:
        if capability.lower() in blob:
            matches += 1
    score += min(matches * 7.0, 35.0)

    if tender.province and any(loc.lower() == tender.province.lower() for loc in locations):
        score += 8.0

    try:
        if tender.closing_date:
            days = (tender.closing_date - date.today()).days
            if days >= 0:
                score += 2.0 if days <= 7 else 6.0
    except Exception:
        pass

    return max(0.0, min(score, 100.0))


def fit_band_from_score(score):
    if score is None:
        return None
    if score >= 80:
        return "high_potential"
    if score >= 55:
        return "possible_fit"
    return "low_fit"


def extract_scope_summary(tender: TenderCache):
    text = tender.description or tender.title or "Scope of work summary not available."
    text = re.sub(r"\s+", " ", text).strip()
    return text[:260]


def current_document_status(session_db, tender_id: int):
    doc = session_db.execute(
        select(TenderDocumentCache)
        .where(TenderDocumentCache.tender_id == tender_id)
        .order_by(desc(TenderDocumentCache.fetched_at), desc(TenderDocumentCache.id))
        .limit(1)
    ).scalars().first()
    if not doc:
        return None
    return {
        "fetch_status": doc.fetch_status,
        "filename": doc.filename,
        "content_type": doc.content_type,
        "fetched_at": doc.fetched_at.isoformat() if doc.fetched_at else None,
        "error_message": doc.error_message,
    }


def latest_analysis_for(session_db, user_id: int, tender_id: int):
    job = session_db.execute(
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
        "bid_decision": raw.get("bid_decision"),
        "mandatory_requirements": raw.get("mandatory_requirements") or [],
        "compliance_documents": raw.get("compliance_documents") or [],
        "key_dates": raw.get("key_dates") or [],
        "strengths": raw.get("strengths") or [],
        "gaps": raw.get("gaps") or [],
        "risks": raw.get("risks") or [],
        "recommendations": raw.get("recommendations") or [],
        "evidence_notes": raw.get("evidence_notes") or [],
        "analysis_source": raw.get("analysis_source"),
        "analysis_source_error": raw.get("analysis_source_error"),
        "document_fetch_status": raw.get("document_fetch_status"),
        "document_text_chars": raw.get("document_text_chars"),
    }


def get_user_decision(session_db, user_id: int, tender_id: int):
    return session_db.execute(
        select(UserTenderDecision)
        .where(UserTenderDecision.user_id == user_id, UserTenderDecision.tender_id == tender_id)
        .limit(1)
    ).scalars().first()


def find_running_analysis(session_db, user_id: int, tender_id: int):
    return session_db.execute(
        select(AnalysisJob)
        .where(
            AnalysisJob.user_id == user_id,
            AnalysisJob.tender_id == tender_id,
            AnalysisJob.status == "running",
        )
        .order_by(desc(AnalysisJob.updated_at))
        .limit(1)
    ).scalars().first()



def get_openai_client():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    if OpenAI is None:
        raise RuntimeError("The openai Python package is not available in this deployment.")
    return OpenAI(api_key=api_key, timeout=OPENAI_TIMEOUT_SECONDS)


def extract_json_object(text_value: str):
    if not text_value:
        raise ValueError("OpenAI returned an empty response.")
    text_value = text_value.strip()
    if text_value.startswith("```"):
        text_value = re.sub(r"^```(?:json)?", "", text_value, flags=re.I).strip()
        text_value = re.sub(r"```$", "", text_value).strip()
    try:
        return json.loads(text_value)
    except Exception:
        start = text_value.find("{")
        end = text_value.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text_value[start:end + 1])
        raise


def as_list(value, limit=6):
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        item = str(item).strip()
        if item:
            out.append(item)
    return out[:limit]


def get_cached_document_text(session_db, tender_id: int):
    doc = session_db.execute(
        select(TenderDocumentCache)
        .where(TenderDocumentCache.tender_id == tender_id)
        .order_by(desc(TenderDocumentCache.fetched_at), desc(TenderDocumentCache.id))
        .limit(1)
    ).scalars().first()

    text_value = (doc.extracted_text or "").strip() if doc else ""
    return doc, text_value


def compact_text(value: str, max_chars: int):
    value = re.sub(r"\\s+", " ", (value or "")).strip()
    return value[:max_chars]


def openai_tender_analysis(session_db, tender: TenderCache, profile: Profile):
    client = get_openai_client()
    doc, document_text = get_cached_document_text(session_db, tender.id)

    # Do not silently pretend to read documents.
    document_quality = {
        "has_cached_document": bool(doc),
        "fetch_status": doc.fetch_status if doc else None,
        "text_chars": len(document_text),
        "usable_text": len(document_text) >= 600,
    }

    profile_payload = {
        "company_name": profile.company_name,
        "profile_name": profile.name,
        "industry": profile.industry,
        "capabilities": normalize_keywords(profile.capabilities_text or ""),
        "locations": normalize_keywords(profile.locations_text or ""),
        "profile_text_excerpt": compact_text(profile.extracted_text or "", 6000),
        "profile_gaps": [
            {"title": issue.title, "detail": issue.detail, "status": issue.status}
            for issue in (profile.issues or [])
        ],
    }

    tender_payload = {
        "id": tender.id,
        "title": tender.title,
        "description": tender.description,
        "buyer_name": tender.buyer_name,
        "province": tender.province,
        "industry": tender.industry,
        "tender_type": tender.tender_type,
        "issued_date": tender.issued_date.isoformat() if tender.issued_date else None,
        "closing_date": tender.closing_date.isoformat() if tender.closing_date else None,
        "document_url": tender.document_url,
        "source_url": tender.source_url,
        "document_quality": document_quality,
    }

    system_prompt = """
You are TenderAI, a South African tender analyst for SMEs.

Produce a practical bid/no-bid analysis. This is not a generic summary.
Use the tender document text as primary evidence when available.
If the document text is weak or missing, clearly say so and only use tender metadata.

Rules:
- Be specific to this tender and this supplier profile.
- Do not say "review the requirements" unless you name the exact requirement.
- Identify concrete scope, mandatory requirements, compliance documents, risks, profile gaps, and next actions.
- Score realistically from 0 to 100.
- Return only valid JSON.

Return this JSON object:
{
  "score": number,
  "fit_band": "high_potential" | "possible_fit" | "low_fit",
  "bid_decision": "pursue" | "review_first" | "do_not_prioritise",
  "summary": "specific 2-4 sentence analyst view",
  "scope_summary": "specific scope of work",
  "document_match": boolean,
  "document_match_reason": "explain what document text was or was not used",
  "mandatory_requirements": ["specific requirement"],
  "compliance_documents": ["specific document/certificate/form"],
  "key_dates": ["specific date"],
  "briefing_date": string|null,
  "contact_email": string|null,
  "contact_phone": string|null,
  "proposal_required": boolean,
  "strengths": ["specific supplier advantage"],
  "gaps": ["specific missing proof/capability/compliance issue"],
  "risks": ["specific bid or delivery risk"],
  "recommendations": ["specific next action"],
  "evidence_notes": ["short evidence phrase from tender/profile"]
}
""".strip()

    user_payload = {
        "supplier_profile": profile_payload,
        "tender": tender_payload,
        "tender_document_text": compact_text(document_text, 30000),
    }

    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
    )

    content = response.choices[0].message.content or ""
    parsed = extract_json_object(content)

    score = float(parsed.get("score") or 0)
    score = max(0.0, min(score, 100.0))
    parsed["score"] = score
    parsed["fit_band"] = parsed.get("fit_band") or fit_band_from_score(score) or "low_fit"
    parsed["bid_decision"] = parsed.get("bid_decision") or ("pursue" if score >= 80 else "review_first" if score >= 55 else "do_not_prioritise")
    parsed["document_match"] = bool(parsed.get("document_match"))
    parsed["proposal_required"] = bool(parsed.get("proposal_required"))

    parsed["mandatory_requirements"] = as_list(parsed.get("mandatory_requirements"), 8)
    parsed["compliance_documents"] = as_list(parsed.get("compliance_documents"), 8)
    parsed["key_dates"] = as_list(parsed.get("key_dates"), 8)
    parsed["strengths"] = as_list(parsed.get("strengths"), 6)
    parsed["gaps"] = as_list(parsed.get("gaps"), 6)
    parsed["risks"] = as_list(parsed.get("risks"), 6)
    parsed["recommendations"] = as_list(parsed.get("recommendations"), 6)
    parsed["evidence_notes"] = as_list(parsed.get("evidence_notes"), 8)

    parsed["document_fetch_status"] = doc.fetch_status if doc else None
    parsed["document_text_chars"] = len(document_text)
    parsed["analysis_source"] = "openai"

    if not parsed.get("scope_summary"):
        parsed["scope_summary"] = extract_scope_summary(tender)
    if not parsed.get("summary"):
        parsed["summary"] = "TenderAI completed an OpenAI bid/no-bid analysis."

    return parsed



def build_minimal_analysis(tender: TenderCache, profile: Profile, extracted_text: str):
    email_match = re.search(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", extracted_text or "")
    phone_match = re.search(r"(\+?\d[\d\s\-()]{7,}\d)", extracted_text or "")
    briefing_match = re.search(r"(?:brief(?:ing)?(?: session)?)[^0-9]{0,20}(\d{4}[-/]\d{2}[-/]\d{2}|\d{2}[-/]\d{2}[-/]\d{4})", extracted_text or "", re.I)

    score = keyword_overlap_score(profile, tender) or 0
    return {
        "document_match": True if extracted_text.strip() else False,
        "document_match_reason": "Readable tender document text was found." if extracted_text.strip() else "No readable document text was found.",
        "score": score,
        "summary": "Tender analyzed successfully." if extracted_text.strip() else "Tender document could not be read well enough.",
        "scope_summary": extract_scope_summary(tender),
        "briefing_date": briefing_match.group(1).replace("/", "-") if briefing_match else None,
        "contact_email": email_match.group(1) if email_match else None,
        "contact_phone": phone_match.group(1) if phone_match else None,
        "proposal_required": bool(re.search(r"\bproposal\b", extracted_text or "", re.I)),
    }


@app.context_processor
def inject_globals():
    with get_db_session() as session_db:
        user = get_current_user(session_db)
        active_profile = get_active_profile(session_db, user.id) if user else None
        latest_ingest = None

        try:
            latest_ingest = session_db.execute(
                select(IngestRun).order_by(desc(IngestRun.started_at), desc(IngestRun.id)).limit(1)
            ).scalars().first()
        except Exception:
            latest_ingest = None

        current_user_data = None
        if user:
            current_user_data = {
                "id": user.id,
                "email": user.email,
                "full_name": user.full_name,
            }

        active_profile_data = serialize_profile(active_profile) if active_profile else None

        latest_ingest_data = None
        if latest_ingest:
            latest_ingest_data = {
                "status": latest_ingest.status,
                "started_at": latest_ingest.started_at.isoformat() if latest_ingest.started_at else None,
            }

        return {
            "current_user": current_user_data,
            "active_profile": active_profile_data,
            "latest_ingest": latest_ingest_data,
        }


@app.template_filter("days_left")
def days_left_filter(target_date):
    if not target_date:
        return None
    if isinstance(target_date, str):
        try:
            target_date = date.fromisoformat(target_date[:10])
        except Exception:
            return None
    return (target_date - date.today()).days


@app.get("/signup")
def signup():
    if session.get("user_id"):
        return redirect(url_for("home"))
    return render_template("signup.html")


@app.post("/signup")
def signup_post():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    full_name = (request.form.get("full_name") or "").strip()

    if not email or not password:
        flash("Email and password are required.", "error")
        return redirect(url_for("signup"))

    with get_db_session() as session_db:
        existing = session_db.execute(select(User).where(User.email == email).limit(1)).scalars().first()
        if existing:
            flash("That email is already registered.", "warning")
            return redirect(url_for("login"))

        user = User(
            email=email,
            password_hash=generate_password_hash(password),
            full_name=full_name or None,
        )
        session_db.add(user)
        session_db.flush()
        session["user_id"] = user.id

    flash("Account created successfully.", "success")
    return redirect(url_for("profiles"))


@app.get("/login")
def login():
    if session.get("user_id"):
        return redirect(url_for("home"))
    return render_template("login.html")


@app.post("/login")
def login_post():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""

    with get_db_session() as session_db:
        user = session_db.execute(select(User).where(User.email == email).limit(1)).scalars().first()
        if not user or not check_password_hash(user.password_hash, password):
            flash("Invalid email or password.", "error")
            return redirect(url_for("login"))

        session["user_id"] = user.id

    flash("Welcome back.", "success")
    next_url = request.args.get("next") or url_for("home")
    return redirect(next_url)


@app.post("/logout")
def logout():
    session.clear()
    flash("Signed out.", "success")
    return redirect(url_for("login"))


@app.get("/")
@login_required
def home():
    with get_db_session() as session_db:
        user = get_current_user(session_db)
        active_profile = get_active_profile(session_db, user.id)
        gap_summary = build_profile_gap_summary(active_profile)
        readiness_band = "ready" if active_profile and gap_summary["pending_count"] == 0 else "watchlist" if active_profile else "no_profile"
        readiness_note = (
            "Your active profile is ready for tender matching."
            if active_profile and gap_summary["pending_count"] == 0
            else "Upload and activate a profile to unlock matching."
            if not active_profile
            else "Your profile is active, but some readiness gaps remain."
        )

        total_live = session_db.execute(
            select(func.count()).select_from(TenderCache).where(TenderCache.is_live.is_(True))
        ).scalar_one()

        tenders = session_db.execute(
            select(TenderCache)
            .where(TenderCache.is_live.is_(True))
            .order_by(TenderCache.closing_date.asc().nulls_last(), desc(TenderCache.updated_at))
            .limit(100)
        ).scalars().all()

        featured = []
        for tender in tenders:
            score = keyword_overlap_score(active_profile, tender) if active_profile else None
            if score is None:
                continue
            featured.append({
                "tender": tender,
                "score": score,
                "fit_band": fit_band_from_score(score),
                "scope_summary": extract_scope_summary(tender),
            })

        featured.sort(key=lambda x: (x["score"] or 0), reverse=True)

        return render_template(
            "home.html",
            total_live=total_live,
            featured=featured[:8],
            readiness_band=readiness_band,
            readiness_note=readiness_note,
            profile_gap_summary=gap_summary,
        )


@app.get("/tenders")
@login_required
def tenders():
    with get_db_session() as session_db:
        user = get_current_user(session_db)
        active_profile = get_active_profile(session_db, user.id)
        gap_summary = build_profile_gap_summary(active_profile)
        readiness_band = "ready" if active_profile and gap_summary["pending_count"] == 0 else "watchlist" if active_profile else "no_profile"

        province = (request.args.get("province") or "").strip()
        tender_type = (request.args.get("tender_type") or "").strip()
        industry = (request.args.get("industry") or "").strip()
        issued_from = (request.args.get("issued_from") or "").strip()
        search_text = (request.args.get("q") or "").strip()
        fit_band_filter = (request.args.get("fit_band") or "").strip()

        query = select(TenderCache).where(TenderCache.is_live.is_(True))

        if province:
            query = query.where(TenderCache.province == province)
        if tender_type:
            query = query.where(TenderCache.tender_type == tender_type)
        if industry:
            query = query.where(TenderCache.industry == industry)
        if issued_from:
            try:
                dt = datetime.strptime(issued_from, "%Y-%m-%d").date()
                query = query.where(TenderCache.issued_date >= dt)
            except ValueError:
                pass
        if search_text:
            like_term = f"%{search_text.lower()}%"
            query = query.where(
                or_(
                    func.lower(TenderCache.title).like(like_term),
                    func.lower(func.coalesce(TenderCache.description, "")).like(like_term),
                    func.lower(func.coalesce(TenderCache.buyer_name, "")).like(like_term),
                    func.lower(func.coalesce(TenderCache.industry, "")).like(like_term),
                )
            )

        items = session_db.execute(
            query.order_by(TenderCache.closing_date.asc().nulls_last(), desc(TenderCache.updated_at)).limit(200)
        ).scalars().all()

        ranked = []
        for tender in items:
            score = keyword_overlap_score(active_profile, tender) if active_profile else None
            fit_band = fit_band_from_score(score) if score is not None else None
            if fit_band_filter and fit_band != fit_band_filter:
                continue
            ranked.append({
                "tender": tender,
                "score": score,
                "fit_band": fit_band,
                "scope_summary": extract_scope_summary(tender),
            })

        if active_profile:
            ranked.sort(key=lambda x: (x["score"] or 0), reverse=True)

        provinces = session_db.execute(
            select(TenderCache.province)
            .where(TenderCache.is_live.is_(True), TenderCache.province.is_not(None))
            .distinct()
            .order_by(TenderCache.province)
        ).scalars().all()

        tender_types = session_db.execute(
            select(TenderCache.tender_type)
            .where(TenderCache.is_live.is_(True), TenderCache.tender_type.is_not(None))
            .distinct()
            .order_by(TenderCache.tender_type)
        ).scalars().all()

        industries = session_db.execute(
            select(TenderCache.industry)
            .where(TenderCache.is_live.is_(True), TenderCache.industry.is_not(None))
            .distinct()
            .order_by(TenderCache.industry)
        ).scalars().all()

        band_counts = {"high_potential": 0, "possible_fit": 0, "low_fit": 0}
        for item in ranked:
            if item["fit_band"] in band_counts:
                band_counts[item["fit_band"]] += 1

        return render_template(
            "feed.html",
            ranked_tenders=ranked,
            provinces=provinces,
            tender_types=tender_types,
            industries=industries,
            filters={
                "province": province,
                "tender_type": tender_type,
                "industry": industry,
                "issued_from": issued_from,
                "q": search_text,
                "fit_band": fit_band_filter,
            },
            readiness_band=readiness_band,
            profile_gap_summary=gap_summary,
            band_counts=band_counts,
        )


@app.get("/tender/<int:tender_id>")
@login_required
def tender_detail(tender_id: int):
    with get_db_session() as session_db:
        user = get_current_user(session_db)
        tender = session_db.get(TenderCache, tender_id)
        if not tender:
            return render_template("404.html"), 404

        active_profile = get_active_profile(session_db, user.id)
        gap_summary = build_profile_gap_summary(active_profile)
        readiness_band = "ready" if active_profile and gap_summary["pending_count"] == 0 else "watchlist" if active_profile else "no_profile"
        readiness_note = (
            "Your active profile is ready for tender matching."
            if active_profile and gap_summary["pending_count"] == 0
            else "Upload and activate a profile to unlock matching."
            if not active_profile
            else "Your profile is active, but some readiness gaps remain."
        )

        score = keyword_overlap_score(active_profile, tender) if active_profile else None
        latest_analysis = latest_analysis_for(session_db, user.id, tender_id)
        decision = get_user_decision(session_db, user.id, tender_id)
        document_status = current_document_status(session_db, tender_id)
        running_job = find_running_analysis(session_db, user.id, tender_id) if user else None

        return render_template(
            "tender_detail.html",
            tender=tender,
            alignment_score=score,
            scope_summary=(latest_analysis or {}).get("scope_summary") or extract_scope_summary(tender),
            fit_band=fit_band_from_score(score) if score is not None else None,
            can_analyze=bool(active_profile),
            analyze_action_url=url_for("analyze_tender_page", tender_id=tender_id),
            latest_analysis=latest_analysis,
            decision=decision,
            readiness_band=readiness_band,
            readiness_note=readiness_note,
            profile_gap_summary=gap_summary,
            document_status=document_status,
            running_job=running_job,
        )


@app.post("/tender/<int:tender_id>/analyze")
@login_required
def analyze_tender_page(tender_id: int):
    with get_db_session() as session_db:
        user = get_current_user(session_db)
        tender = session_db.get(TenderCache, tender_id)
        if not tender:
            flash("Tender not found.", "error")
            return redirect(url_for("tenders"))

        active_profile = get_active_profile(session_db, user.id)
        if not active_profile:
            flash("Please upload and activate a business profile first.", "error")
            return redirect(url_for("profiles"))

        existing_running = find_running_analysis(session_db, user.id, tender_id)
        if existing_running:
            flash("An analysis is already running for this tender.", "warning")
            return redirect(url_for("tender_detail", tender_id=tender_id))

        job = AnalysisJob(
            user_id=user.id,
            profile_id=active_profile.id,
            tender_id=tender_id,
            status="running",
        )
        session_db.add(job)
        session_db.flush()

        try:
            analysis = openai_tender_analysis(session_db, tender, active_profile)

            job.status = "completed"
            job.score = float(analysis.get("score") or 0)
            job.summary = analysis.get("summary")
            job.strengths_text = "\n".join(analysis.get("strengths") or [])
            job.risks_text = "\n".join((analysis.get("risks") or []) + (analysis.get("gaps") or []))
            job.recommendations_text = "\n".join(analysis.get("recommendations") or [])
            job.raw_result_json = json.dumps(analysis, ensure_ascii=False, default=str)
            job.error_message = None

            flash("OpenAI tender analysis completed.", "success")
            return redirect(url_for("tender_detail", tender_id=tender_id))

        except Exception as exc:
            # Do not hide OpenAI failure behind generic heuristic output.
            job.status = "failed"
            job.error_message = str(exc)
            job.raw_result_json = json.dumps({
                "analysis_source": "openai_failed",
                "analysis_source_error": str(exc),
            }, ensure_ascii=False)
            flash(f"OpenAI analysis failed: {exc}", "error")
            return redirect(url_for("tender_detail", tender_id=tender_id))


@app.post("/tender/<int:tender_id>/decision")
@login_required
def save_decision(tender_id: int):
    with get_db_session() as session_db:
        user = get_current_user(session_db)
        tender = session_db.get(TenderCache, tender_id)
        if not tender:
            flash("Tender not found.", "error")
            return redirect(url_for("tenders"))

        decision = get_user_decision(session_db, user.id, tender_id)
        if not decision:
            decision = UserTenderDecision(user_id=user.id, tender_id=tender_id)
            session_db.add(decision)

        decision.pursuit_status = (request.form.get("pursuit_status") or "not_decided").strip()
        decision.owner = (request.form.get("owner") or "").strip() or None
        decision.next_action = (request.form.get("next_action") or "").strip() or None
        decision.notes = (request.form.get("notes") or "").strip() or None

        flash("Decision saved.", "success")
        return redirect(url_for("tender_detail", tender_id=tender_id))


@app.get("/profiles")
@login_required
def profiles():
    with get_db_session() as session_db:
        user = get_current_user(session_db)
        profiles_list = session_db.execute(
            select(Profile)
            .where(Profile.user_id == user.id)
            .order_by(desc(Profile.updated_at))
        ).scalars().all()
        return render_template("profiles.html", profiles=[serialize_profile(p) for p in profiles_list])


@app.post("/profiles/upload")
@login_required
def upload_profile():
    uploaded = request.files.get("profile_pdf") or request.files.get("profile_file") or request.files.get("file")
    if not uploaded or not uploaded.filename.lower().endswith(".pdf"):
        flash("Please upload a PDF profile.", "error")
        return redirect(url_for("profiles"))

    try:
        text = extract_pdf_text(uploaded)
    except Exception as exc:
        flash(f"Could not read PDF: {exc}", "error")
        return redirect(url_for("profiles"))

    parsed = parse_profile_text(text, uploaded.filename)

    with get_db_session() as session_db:
        user = get_current_user(session_db)
        session_db.execute(
            Profile.__table__.update()
            .where(Profile.user_id == user.id)
            .values(is_active=False)
        )

        profile = Profile(
            user_id=user.id,
            name=parsed.get("company_name") or os.path.splitext(uploaded.filename)[0],
            company_name=parsed.get("company_name"),
            original_filename=uploaded.filename,
            industry=parsed.get("industry"),
            capabilities_text=", ".join(parsed.get("capabilities") or []),
            locations_text=", ".join(parsed.get("locations") or []),
            extracted_text=text[:200000],
            parsed_json=json.dumps(parsed, ensure_ascii=False, default=str),
            is_active=True,
        )
        session_db.add(profile)
        session_db.flush()

        for issue in parsed.get("issues") or []:
            session_db.add(
                ProfileIssue(
                    profile_id=profile.id,
                    issue_type="profile_gap",
                    title=issue.get("title") or "Profile issue",
                    detail=issue.get("detail"),
                    penalty_weight=float(issue.get("penalty_weight") or 5),
                    status="pending",
                )
            )

    flash("Profile uploaded and set as active.", "success")
    return redirect(url_for("profiles"))


@app.post("/profiles/<int:profile_id>/activate")
@login_required
def activate_profile(profile_id: int):
    with get_db_session() as session_db:
        user = get_current_user(session_db)
        profile = session_db.get(Profile, profile_id)
        if not profile or profile.user_id != user.id:
            flash("Profile not found.", "error")
            return redirect(url_for("profiles"))

        session_db.execute(
            Profile.__table__.update()
            .where(Profile.user_id == user.id)
            .values(is_active=False)
        )
        profile.is_active = True
        flash("Active profile updated.", "success")
        return redirect(url_for("profiles"))


@app.post("/profile-issues/<int:issue_id>/status")
@login_required
def update_issue_status(issue_id: int):
    status = (request.form.get("status") or "").strip().lower()
    if status not in {"pending", "fixed"}:
        flash("Invalid issue status.", "error")
        return redirect(url_for("profiles"))

    with get_db_session() as session_db:
        user = get_current_user(session_db)
        issue = session_db.get(ProfileIssue, issue_id)
        if not issue or issue.profile.user_id != user.id:
            flash("Issue not found.", "error")
            return redirect(url_for("profiles"))
        issue.status = status
        flash("Issue status updated.", "success")
        return redirect(url_for("profiles"))


@app.get("/api/admin/run-ingest")
@app.post("/api/admin/run-ingest")
def api_run_ingest():
    with get_db_session() as session_db:
        page_size = int(request.args.get("page_size", os.getenv("ETENDERS_PAGE_SIZE", "10")))
        try:
            result = ingest_tenders(
                session=session_db,
                page_size=page_size,
            )
            return jsonify(result)
        except Exception as exc:
            session_db.rollback()
            return jsonify({"ok": False, "status": "failed", "error": str(exc)}), 500


@app.get("/api/admin/fetch-documents")
@app.post("/api/admin/fetch-documents")
def api_fetch_documents():
    with get_db_session() as session_db:
        limit = max(1, min(int(request.args.get("limit", 10)), 100))
        force_retry_failed = str(request.args.get("force_retry_failed", "false")).lower() in {"1", "true", "yes"}

        try:
            tenders = session_db.execute(
                select(TenderCache)
                .where(
                    TenderCache.is_live.is_(True),
                    TenderCache.document_url.is_not(None),
                )
                .order_by(TenderCache.closing_date.asc().nulls_last(), desc(TenderCache.updated_at))
                .limit(limit)
            ).scalars().all()

            if force_retry_failed:
                for tender in tenders:
                    latest_doc = session_db.execute(
                        select(TenderDocumentCache)
                        .where(TenderDocumentCache.tender_id == tender.id)
                        .order_by(desc(TenderDocumentCache.fetched_at), desc(TenderDocumentCache.id))
                        .limit(1)
                    ).scalars().first()
                    if latest_doc and latest_doc.fetch_status == "failed":
                        latest_doc.fetch_status = "pending"

            result_items = fetch_documents_for_tenders(session_db, tenders)

            summary = {
                "ok": True,
                "limit": limit,
                "candidates": len(tenders),
                "processed": len(result_items),
                "fetched": sum(1 for item in result_items if item.get("ok")),
                "fetch_failed": sum(1 for item in result_items if not item.get("ok")),
                "items": result_items,
            }
            return jsonify(summary)
        except Exception as exc:
            session_db.rollback()
            return jsonify({"ok": False, "status": "failed", "error": str(exc)}), 500



@app.get("/api/admin/openai-analysis-debug/<int:tender_id>")
@login_required
def api_openai_analysis_debug(tender_id: int):
    with get_db_session() as session_db:
        user = get_current_user(session_db)
        tender = session_db.get(TenderCache, tender_id)
        if not tender:
            return jsonify({"ok": False, "error": "tender_not_found"}), 404

        profile = get_active_profile(session_db, user.id)
        if not profile:
            return jsonify({"ok": False, "error": "no_active_profile"}), 400

        doc, document_text = get_cached_document_text(session_db, tender_id)
        return jsonify({
            "ok": True,
            "openai_key_present": bool(os.getenv("OPENAI_API_KEY")),
            "openai_package_available": OpenAI is not None,
            "model": OPENAI_MODEL,
            "tender_id": tender_id,
            "title": tender.title,
            "document_url": tender.document_url,
            "has_cached_document": bool(doc),
            "document_fetch_status": doc.fetch_status if doc else None,
            "document_text_chars": len(document_text),
            "document_text_preview": document_text[:1200],
            "active_profile_id": profile.id,
            "profile_company": profile.company_name,
        })



@app.get("/health")
def health():
    with get_db_session() as session_db:
        count = session_db.execute(select(func.count()).select_from(TenderCache)).scalar_one()
        return jsonify({"ok": True, "cached_tenders": count, "time": utcnow().isoformat()})


@app.errorhandler(404)
def not_found(_):
    return render_template("404.html"), 404


@app.errorhandler(500)
def internal_error(_):
    return render_template("500.html"), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
