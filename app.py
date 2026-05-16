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
    render_template_string,
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
from services.tender_document_parser import parse_tender_document, parse_live_tender_documents, get_latest_parsed_document, process_parse_worker
from services.tender_ai_analysis import analyze_tender_against_profile
from services.etenders_ingest import ingest_tenders

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "tenderai-dev-fallback-secret")

init_db()


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
        "probability_of_acquisition": raw.get("probability_of_acquisition"),
        "executive_assessment": raw.get("executive_assessment"),
        "profile_fit": raw.get("profile_fit") or {},
        "estimated_project_costs": raw.get("estimated_project_costs") or {},
        "estimated_revenue": raw.get("estimated_revenue") or {},
        "commercial_view": raw.get("commercial_view") or {},
        "measures_to_improve_chances": raw.get("measures_to_improve_chances") or [],
        "mandatory_requirements": raw.get("mandatory_requirements") or [],
        "compliance_documents": raw.get("compliance_documents") or [],
        "key_dates": raw.get("key_dates") or [],
        "strengths": raw.get("strengths") or [],
        "gaps": raw.get("gaps") or [],
        "risks": raw.get("risks") or [],
        "recommendations": raw.get("recommendations") or [],
        "questions_to_clarify": raw.get("questions_to_clarify") or [],
        "evidence_notes": raw.get("evidence_notes") or [],
        "analysis_source": raw.get("analysis_source"),
        "analysis_source_error": raw.get("analysis_source_error"),
        "parsed_document_id": raw.get("parsed_document_id"),
        "parse_confidence": raw.get("parse_confidence"),
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

        parsed_doc = get_latest_parsed_document(session_db, tender_id)
        if not parsed_doc:
            flash("Parse the tender document first. Analysis now uses the parsed tender intelligence record, not the raw document.", "warning")
            return redirect(url_for("tender_detail", tender_id=tender_id))

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
            analysis = analyze_tender_against_profile(session_db, tender, active_profile)

            job.status = "completed"
            job.score = float(analysis.get("score") or 0)
            job.summary = analysis.get("summary")
            job.strengths_text = "\n".join(
                (analysis.get("strengths") or [])
                + (analysis.get("measures_to_improve_chances") or [])
            )
            job.risks_text = "\n".join(
                (analysis.get("risks") or [])
                + (analysis.get("gaps") or [])
                + ((analysis.get("commercial_view") or {}).get("margin_risks") or [])
                + ((analysis.get("commercial_view") or {}).get("cashflow_risks") or [])
            )
            job.recommendations_text = "\n".join(analysis.get("recommendations") or [])
            job.raw_result_json = json.dumps(analysis, ensure_ascii=False, default=str)
            job.error_message = None

            flash("Tender analysis completed from parsed document intelligence.", "success")
            return redirect(url_for("analysis_report", tender_id=tender_id))

        except Exception as exc:
            job.status = "failed"
            job.error_message = str(exc)
            job.raw_result_json = json.dumps({
                "analysis_source": "openai_from_parsed_document_failed",
                "analysis_source_error": str(exc),
            }, ensure_ascii=False)
            flash(f"Analysis failed: {exc}", "error")
            return redirect(url_for("tender_detail", tender_id=tender_id))



def money_value(value):
    if value is None or value == "":
        return "Not visible"
    try:
        return "R{:,.0f}".format(float(value))
    except Exception:
        return str(value)


def list_or_missing(items, missing="Not extracted from the parsed tender record."):
    if not items:
        return [missing]
    return items


ANALYSIS_REPORT_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>TenderAI Analysis Report</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        :root {
            --bg: #080b10;
            --panel: #111827;
            --panel2: #0f172a;
            --text: #f8fafc;
            --muted: #94a3b8;
            --line: rgba(148, 163, 184, 0.25);
            --orange: #f97316;
            --green: #22c55e;
            --yellow: #eab308;
            --red: #ef4444;
            --blue: #38bdf8;
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: radial-gradient(circle at top left, rgba(249,115,22,.18), transparent 38%), var(--bg);
            color: var(--text);
        }
        .wrap { max-width: 1180px; margin: 0 auto; padding: 28px 18px 60px; }
        a { color: var(--orange); text-decoration: none; }
        .topbar { display: flex; justify-content: space-between; gap: 12px; align-items: center; margin-bottom: 22px; }
        .btn {
            display: inline-flex; align-items: center; gap: 8px;
            border: 1px solid var(--line); background: rgba(15,23,42,.7);
            color: var(--text); padding: 10px 14px; border-radius: 12px; font-weight: 650;
        }
        .hero {
            background: linear-gradient(135deg, rgba(17,24,39,.96), rgba(15,23,42,.92));
            border: 1px solid var(--line);
            border-radius: 22px;
            padding: 24px;
            box-shadow: 0 20px 60px rgba(0,0,0,.28);
            margin-bottom: 18px;
        }
        .eyebrow { color: var(--orange); text-transform: uppercase; letter-spacing: .08em; font-size: 12px; font-weight: 800; }
        h1 { margin: 8px 0 8px; font-size: clamp(26px, 4vw, 42px); line-height: 1.06; }
        .subtitle { color: var(--muted); max-width: 850px; line-height: 1.55; font-size: 16px; }
        .meta { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 16px; }
        .pill {
            border: 1px solid var(--line); background: rgba(8,11,16,.55);
            border-radius: 999px; padding: 8px 11px; color: #cbd5e1; font-size: 13px;
        }
        .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 16px; }
        .card {
            grid-column: span 12;
            background: rgba(17,24,39,.88);
            border: 1px solid var(--line);
            border-radius: 18px;
            padding: 18px;
        }
        @media (min-width: 800px) {
            .span4 { grid-column: span 4; }
            .span6 { grid-column: span 6; }
            .span8 { grid-column: span 8; }
            .span12 { grid-column: span 12; }
        }
        h2 { margin: 0 0 12px; font-size: 19px; }
        h3 { margin: 16px 0 8px; font-size: 15px; color: #e2e8f0; }
        p { color: #cbd5e1; line-height: 1.58; }
        ul { margin: 8px 0 0; padding-left: 20px; }
        li { color: #cbd5e1; margin: 8px 0; line-height: 1.45; }
        .scorebox {
            display: flex; gap: 14px; align-items: center; justify-content: space-between;
        }
        .number {
            font-size: 46px; font-weight: 900; color: var(--orange); line-height: 1;
        }
        .label { color: var(--muted); font-size: 13px; margin-top: 4px; }
        .decision {
            font-size: 18px; font-weight: 900; padding: 10px 14px; border-radius: 14px;
            background: rgba(249,115,22,.14); border: 1px solid rgba(249,115,22,.35);
        }
        .table { width: 100%; border-collapse: collapse; margin-top: 10px; overflow: hidden; }
        .table th, .table td {
            border-bottom: 1px solid var(--line);
            padding: 10px 8px;
            color: #cbd5e1;
            text-align: left;
            vertical-align: top;
        }
        .table th { color: #f8fafc; font-size: 13px; }
        .muted { color: var(--muted); }
        .warning { color: #fde68a; }
        .good { color: #86efac; }
        .bad { color: #fca5a5; }
        .raw {
            white-space: pre-wrap;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            background: #020617;
            border: 1px solid var(--line);
            padding: 14px;
            border-radius: 14px;
            color: #cbd5e1;
            max-height: 420px;
            overflow: auto;
            font-size: 12px;
        }
    </style>
</head>
<body>
<div class="wrap">
    <div class="topbar">
        <a class="btn" href="{{ url_for('tender_detail', tender_id=tender.id) }}">← Back to tender</a>
        <a class="btn" href="{{ url_for('tenders') }}">Browse tenders</a>
    </div>

    <section class="hero">
        <div class="eyebrow">TenderAI Bid Intelligence Report</div>
        <h1>{{ tender.title or "Tender opportunity" }}</h1>
        <div class="subtitle">
            {{ analysis.executive_assessment or analysis.summary or "Analysis was completed, but the response did not include an executive assessment." }}
        </div>
        <div class="meta">
            <span class="pill">Buyer: {{ tender.buyer_name or "Unknown" }}</span>
            <span class="pill">Province: {{ tender.province or "Unknown" }}</span>
            <span class="pill">Closing: {{ tender.closing_date or "Unknown" }}</span>
            <span class="pill">Source: {{ analysis.analysis_source or "Unknown" }}</span>
            <span class="pill">Parse confidence: {{ analysis.parse_confidence or "Unknown" }}</span>
        </div>
    </section>

    <div class="grid">
        <section class="card span4">
            <h2>Bid Decision</h2>
            <div class="scorebox">
                <div>
                    <div class="number">{{ analysis.score or 0 }}</div>
                    <div class="label">Fit score / 100</div>
                </div>
                <div>
                    <div class="decision">{{ analysis.bid_decision or "review_first" }}</div>
                    <div class="label">Recommended action</div>
                </div>
            </div>
            <p><strong>Probability of acquisition:</strong> {{ analysis.probability_of_acquisition or 0 }}%</p>
            <p><strong>Fit band:</strong> {{ analysis.fit_band or "Not classified" }}</p>
            <p><strong>Confidence:</strong> {{ analysis.confidence_level or "Not stated" }}</p>
        </section>

        <section class="card span8">
            <h2>Scope of Work</h2>
            <p>{{ analysis.scope_summary or "No scope summary was generated." }}</p>
            <h3>Parsed tender evidence</h3>
            <ul>
            {% for item in list_or_missing(analysis.evidence_notes, "No evidence notes were returned.") %}
                <li>{{ item }}</li>
            {% endfor %}
            </ul>
        </section>

        <section class="card span6">
            <h2>Mandatory Requirements</h2>
            <ul>
            {% for item in list_or_missing(analysis.mandatory_requirements) %}
                <li>{{ item }}</li>
            {% endfor %}
            </ul>

            <h3>Compliance Documents</h3>
            <ul>
            {% for item in list_or_missing(analysis.compliance_documents) %}
                <li>{{ item }}</li>
            {% endfor %}
            </ul>
        </section>

        <section class="card span6">
            <h2>Criteria, Dates and Contacts</h2>
            <h3>Key dates</h3>
            <ul>
            {% for item in list_or_missing(analysis.key_dates, "No key dates were extracted besides the tender metadata.") %}
                <li>{{ item }}</li>
            {% endfor %}
            </ul>
            <p><strong>Briefing date:</strong> {{ analysis.briefing_date or "Not extracted" }}</p>
            <p><strong>Contact email:</strong> {{ analysis.contact_email or "Not extracted" }}</p>
            <p><strong>Contact phone:</strong> {{ analysis.contact_phone or "Not extracted" }}</p>
        </section>

        <section class="card span6">
            <h2>Profile Fit</h2>
            {% set fit = analysis.profile_fit or {} %}
            <h3>Matching capabilities</h3>
            <ul>
            {% for item in list_or_missing(fit.get('matching_capabilities') or analysis.strengths, "No strong matching capabilities were identified.") %}
                <li>{{ item }}</li>
            {% endfor %}
            </ul>

            <h3>Weaknesses or gaps</h3>
            <ul>
            {% for item in list_or_missing(fit.get('weaknesses_or_gaps') or analysis.gaps, "No specific profile gaps were returned.") %}
                <li>{{ item }}</li>
            {% endfor %}
            </ul>
            <p><strong>Geographic fit:</strong> {{ fit.get('geographic_fit') or "Unknown" }}</p>
            <p><strong>Capacity fit:</strong> {{ fit.get('capacity_fit') or "Unknown" }}</p>
            <p><strong>Track record fit:</strong> {{ fit.get('track_record_fit') or "Unknown" }}</p>
        </section>

        <section class="card span6">
            <h2>Measures to Improve Chances</h2>
            <ul>
            {% for item in list_or_missing(analysis.measures_to_improve_chances, "No improvement measures were returned.") %}
                <li>{{ item }}</li>
            {% endfor %}
            </ul>

            <h3>Recommended next steps</h3>
            <ul>
            {% for item in list_or_missing(analysis.recommendations, "No next steps were returned.") %}
                <li>{{ item }}</li>
            {% endfor %}
            </ul>
        </section>

        <section class="card span6">
            <h2>Estimated Project Costs</h2>
            {% set costs = analysis.estimated_project_costs or {} %}
            <table class="table">
                <tr><th>Low</th><td>{{ money_value(costs.get('low')) }}</td></tr>
                <tr><th>Base</th><td>{{ money_value(costs.get('base')) }}</td></tr>
                <tr><th>High</th><td>{{ money_value(costs.get('high')) }}</td></tr>
            </table>
            <h3>Cost breakdown</h3>
            <table class="table">
                <tr><th>Category</th><th>Estimate</th><th>Basis</th></tr>
                {% for row in costs.get('cost_breakdown') or [] %}
                    <tr>
                        <td>{{ row.get('category') }}</td>
                        <td>{{ money_value(row.get('estimate')) }}</td>
                        <td>{{ row.get('basis') }}</td>
                    </tr>
                {% endfor %}
                {% if not costs.get('cost_breakdown') %}
                    <tr><td colspan="3">No cost breakdown was produced.</td></tr>
                {% endif %}
            </table>
            <h3>Cost assumptions</h3>
            <ul>
            {% for item in list_or_missing(costs.get('cost_assumptions'), "No cost assumptions were returned.") %}
                <li>{{ item }}</li>
            {% endfor %}
            </ul>
        </section>

        <section class="card span6">
            <h2>Estimated Revenue</h2>
            {% set revenue = analysis.estimated_revenue or {} %}
            <table class="table">
                <tr><th>Low</th><td>{{ money_value(revenue.get('low')) }}</td></tr>
                <tr><th>Base</th><td>{{ money_value(revenue.get('base')) }}</td></tr>
                <tr><th>High</th><td>{{ money_value(revenue.get('high')) }}</td></tr>
            </table>
            <p><strong>Revenue basis:</strong> {{ revenue.get('revenue_basis') or "No basis returned." }}</p>
            <p><strong>Gross margin view:</strong> {{ revenue.get('gross_margin_comment') or "No margin comment returned." }}</p>
        </section>

        <section class="card span6">
            <h2>Commercial View</h2>
            {% set commercial = analysis.commercial_view or {} %}
            <p><strong>Pricing strategy:</strong> {{ commercial.get('pricing_strategy') or "No pricing strategy returned." }}</p>
            <h3>Margin risks</h3>
            <ul>
            {% for item in list_or_missing(commercial.get('margin_risks'), "No margin risks returned.") %}
                <li>{{ item }}</li>
            {% endfor %}
            </ul>
            <h3>Cash-flow risks</h3>
            <ul>
            {% for item in list_or_missing(commercial.get('cashflow_risks'), "No cash-flow risks returned.") %}
                <li>{{ item }}</li>
            {% endfor %}
            </ul>
        </section>

        <section class="card span6">
            <h2>Risks and Clarification Questions</h2>
            <h3>Risks</h3>
            <ul>
            {% for item in list_or_missing(analysis.risks, "No risks were returned.") %}
                <li>{{ item }}</li>
            {% endfor %}
            </ul>
            <h3>Questions to clarify</h3>
            <ul>
            {% for item in list_or_missing(analysis.questions_to_clarify, "No clarification questions were returned.") %}
                <li>{{ item }}</li>
            {% endfor %}
            </ul>
        </section>

        <section class="card span12">
            <h2>Diagnostics</h2>
            <p><strong>Document match:</strong> {{ analysis.document_match }}</p>
            <p><strong>Document reason:</strong> {{ analysis.document_match_reason or "Not provided" }}</p>
            <p><strong>Parsed document ID:</strong> {{ analysis.parsed_document_id or "Not linked" }}</p>
            <details>
                <summary class="muted">Raw analysis JSON</summary>
                <div class="raw">{{ raw_json }}</div>
            </details>
        </section>
    </div>
</div>
</body>
</html>
"""


@app.get("/tender/<int:tender_id>/analysis-report")
@login_required
def analysis_report(tender_id: int):
    with get_db_session() as session_db:
        user = get_current_user(session_db)
        tender = session_db.get(TenderCache, tender_id)
        if not tender:
            return render_template("404.html"), 404

        job = session_db.execute(
            select(AnalysisJob)
            .where(AnalysisJob.user_id == user.id, AnalysisJob.tender_id == tender_id)
            .order_by(desc(AnalysisJob.updated_at))
            .limit(1)
        ).scalars().first()

        if not job:
            flash("No analysis result found for this tender yet.", "warning")
            return redirect(url_for("tender_detail", tender_id=tender_id))

        analysis = safe_loads(job.raw_result_json, {})
        if not analysis:
            analysis = {
                "summary": job.summary,
                "score": job.score,
                "analysis_source_error": job.error_message,
            }

        raw_json = json.dumps(analysis, indent=2, ensure_ascii=False, default=str)

        return render_template_string(
            ANALYSIS_REPORT_TEMPLATE,
            tender=tender,
            analysis=analysis,
            raw_json=raw_json,
            money_value=money_value,
            list_or_missing=list_or_missing,
        )


@app.get("/api/tender/<int:tender_id>/analysis-result")
@login_required
def api_analysis_result(tender_id: int):
    with get_db_session() as session_db:
        user = get_current_user(session_db)
        job = session_db.execute(
            select(AnalysisJob)
            .where(AnalysisJob.user_id == user.id, AnalysisJob.tender_id == tender_id)
            .order_by(desc(AnalysisJob.updated_at))
            .limit(1)
        ).scalars().first()

        if not job:
            return jsonify({"ok": False, "error": "no_analysis_found"}), 404

        return jsonify({
            "ok": True,
            "job_id": job.id,
            "status": job.status,
            "score": job.score,
            "summary": job.summary,
            "raw_result": safe_loads(job.raw_result_json, {}),
            "error_message": job.error_message,
        })



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



@app.get("/api/admin/parse-document/<int:tender_id>")
@app.post("/api/admin/parse-document/<int:tender_id>")
def api_parse_document(tender_id: int):
    with get_db_session() as session_db:
        tender = session_db.get(TenderCache, tender_id)
        if not tender:
            return jsonify({"ok": False, "error": "tender_not_found"}), 404

        force = str(request.args.get("force", "false")).lower() in {"1", "true", "yes"}

        try:
            result = parse_tender_document(session_db, tender, force=force)
            return jsonify(result)
        except Exception as exc:
            session_db.rollback()
            return jsonify({"ok": False, "status": "failed", "error": str(exc)}), 500


@app.get("/api/admin/parse-documents")
@app.post("/api/admin/parse-documents")
def api_parse_documents():
    with get_db_session() as session_db:
        limit = int(request.args.get("limit", 3))
        force = str(request.args.get("force", "false")).lower() in {"1", "true", "yes"}

        try:
            result = parse_live_tender_documents(session_db, limit=limit, force=force)
            return jsonify(result)
        except Exception as exc:
            session_db.rollback()
            return jsonify({"ok": False, "status": "failed", "error": str(exc)}), 500



@app.get("/api/admin/parse-worker")
@app.post("/api/admin/parse-worker")
def api_parse_worker():
    with get_db_session() as session_db:
        limit = int(request.args.get("limit", 1))
        try:
            result = process_parse_worker(session_db, limit=limit)
            return jsonify(result)
        except Exception as exc:
            session_db.rollback()
            return jsonify({"ok": False, "status": "failed", "error": str(exc)}), 500


@app.get("/api/admin/parsed-document/<int:tender_id>")
def api_get_parsed_document(tender_id: int):
    with get_db_session() as session_db:
        parsed = get_latest_parsed_document(session_db, tender_id)
        if not parsed:
            return jsonify({"ok": False, "error": "no_parsed_document"}), 404
        return jsonify({"ok": True, "parsed_document": parsed})


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

@app.get("/api/admin/db-inspect")
def api_db_inspect():
    from sqlalchemy import text

    with get_db_session() as session_db:
        current = session_db.execute(text("""
            SELECT 
                current_database() AS database_name,
                current_schema() AS schema_name,
                current_user AS db_user
        """)).mappings().first()

        tables = session_db.execute(text("""
            SELECT 
                table_schema,
                table_name
            FROM information_schema.tables
            WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
            ORDER BY table_schema, table_name
        """)).mappings().all()

        indexes = session_db.execute(text("""
            SELECT 
                schemaname,
                tablename,
                indexname
            FROM pg_indexes
            WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
            ORDER BY schemaname, tablename, indexname
        """)).mappings().all()

        return jsonify({
            "ok": True,
            "connection": dict(current),
            "tables": [dict(row) for row in tables],
            "indexes": [dict(row) for row in indexes],
        })
