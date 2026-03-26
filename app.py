import json
import os
import tempfile
from datetime import date, datetime, timezone

from dotenv import load_dotenv
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for

try:
    from pypdf import PdfReader
except Exception:
    try:
        from PyPDF2 import PdfReader
    except Exception:
        PdfReader = None

from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import selectinload

from database import get_db_session, init_db
from models import AnalysisJob, IngestRun, Profile, ProfileIssue, TenderCache, TenderDocumentCache
from services.document_fetcher import fetch_documents_for_tenders
from services.etenders_ingest import ingest_tenders
from services.openai_extractors import parse_supplier_profile_text, parse_tender_document_text


def utcnow():
    return datetime.now(timezone.utc)


load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "tenderai-dev-fallback-secret")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

init_db()


def get_active_profile(session):
    return session.execute(
        select(Profile)
        .options(selectinload(Profile.issues))
        .where(Profile.is_active.is_(True))
        .order_by(desc(Profile.updated_at), desc(Profile.id))
        .limit(1)
    ).scalars().first()


def extract_pdf_text(file_storage) -> str:
    if PdfReader is None:
        raise RuntimeError("PDF reader dependency is not installed")
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        file_storage.save(tmp.name)
        reader = PdfReader(tmp.name)
        pages = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                continue
    try:
        os.unlink(tmp.name)
    except Exception:
        pass
    return "\n".join(pages).strip()


def normalize_keywords(text: str) -> list[str]:
    if not text:
        return []
    raw = text.replace("\n", ",").replace(";", ",").replace("|", ",").split(",")
    results = []
    seen = set()
    for item in raw:
        cleaned = " ".join(item.strip().split())
        if len(cleaned) >= 3:
            lowered = cleaned.lower()
            if lowered not in seen:
                seen.add(lowered)
                results.append(cleaned)
    return results[:30]


def heuristic_profile_parse(text: str, filename: str | None = None) -> dict:
    lower = (text or "").lower()

    industry = None
    industry_rules = [
        ("Construction", ["construction", "contractor", "civil", "infrastructure", "building"]),
        ("ICT", ["software", "ict", "technology", "systems", "digital", "it services"]),
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
    markers = ["services", "capabilities", "core services", "scope", "specialises in", "specializes in"]
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]

    for line in lines:
        ll = line.lower()
        if any(marker in ll for marker in markers):
            capabilities.extend(normalize_keywords(line))

    if not capabilities:
        capabilities = normalize_keywords((text or "")[:3000])[:20]

    locations = []
    provinces = [
        "Gauteng", "KwaZulu-Natal", "Western Cape", "Eastern Cape", "Free State",
        "Mpumalanga", "Limpopo", "North West", "Northern Cape",
    ]
    for province in provinces:
        if province.lower() in lower:
            locations.append(province)

    company_name = lines[0][:255] if lines else None
    if not company_name and filename:
        company_name = os.path.splitext(filename)[0]

    issues = []
    if not capabilities:
        issues.append({"title": "Capabilities are not clearly structured", "detail": "The profile does not clearly list service capabilities.", "penalty_weight": 6})
    if not locations:
        issues.append({"title": "Operating locations are unclear", "detail": "The profile does not clearly indicate service provinces or locations.", "penalty_weight": 4})
    if not industry:
        issues.append({"title": "Industry focus is unclear", "detail": "The profile does not strongly indicate the primary industry.", "penalty_weight": 5})

    return {
        "company_name": company_name,
        "industry": industry,
        "capabilities": capabilities[:20],
        "locations": list(dict.fromkeys(locations)),
        "issues": issues,
    }


def serialize_issue(issue: ProfileIssue) -> dict:
    return {
        "id": issue.id,
        "issue_type": issue.issue_type,
        "title": issue.title,
        "detail": issue.detail,
        "status": issue.status,
        "penalty_weight": issue.penalty_weight,
        "created_at": issue.created_at,
        "updated_at": issue.updated_at,
    }


def serialize_profile(profile: Profile) -> dict:
    return {
        "id": profile.id,
        "name": profile.name,
        "company_name": profile.company_name,
        "original_filename": profile.original_filename,
        "industry": profile.industry,
        "capabilities_text": profile.capabilities_text,
        "locations_text": profile.locations_text,
        "parsed_json": profile.parsed_json,
        "is_active": profile.is_active,
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
        "issues": [serialize_issue(i) for i in (profile.issues or [])],
    }


def serialize_document(doc: TenderDocumentCache) -> dict:
    parsed_json = None
    if doc.parsed_json:
        try:
            parsed_json = json.loads(doc.parsed_json)
        except Exception:
            parsed_json = None
    return {
        "id": doc.id,
        "document_url": doc.document_url,
        "filename": doc.filename,
        "content_type": doc.content_type,
        "fetch_status": doc.fetch_status,
        "error_message": doc.error_message,
        "fetched_at": doc.fetched_at,
        "parsed_json": parsed_json,
    }


def tender_to_view_model(t: TenderCache) -> dict:
    docs = sorted((t.documents or []), key=lambda d: (d.fetched_at is None, d.fetched_at), reverse=True)
    latest_doc = docs[0] if docs else None
    parsed_document = None
    if latest_doc and latest_doc.parsed_json:
        try:
            parsed_document = json.loads(latest_doc.parsed_json)
        except Exception:
            parsed_document = None
    latest_analysis = None
    jobs = sorted((t.analysis_jobs or []), key=lambda a: (a.updated_at is None, a.updated_at), reverse=True)
    if jobs:
        a = jobs[0]
        latest_analysis = {
            "id": a.id,
            "status": a.status,
            "score": a.score,
            "summary": a.summary,
            "strengths_text": a.strengths_text,
            "risks_text": a.risks_text,
            "recommendations_text": a.recommendations_text,
            "updated_at": a.updated_at,
        }
    return {
        "id": t.id,
        "title": t.title,
        "description": t.description,
        "industry": t.industry,
        "tender_type": t.tender_type,
        "province": t.province,
        "buyer_name": t.buyer_name,
        "issued_date": t.issued_date,
        "closing_date": t.closing_date,
        "document_url": t.document_url,
        "source_url": t.source_url,
        "updated_at": t.updated_at,
        "is_live": t.is_live,
        "documents": [serialize_document(d) for d in docs],
        "parsed_document": parsed_document,
        "analysis": latest_analysis,
    }


def keyword_overlap_score(profile, tender: TenderCache) -> float | None:
    if not profile:
        return None

    def text_from(profile_obj, key):
        if isinstance(profile_obj, dict):
            return profile_obj.get(key) or ""
        return getattr(profile_obj, key, "") or ""

    def list_from(profile_obj, method_name, key):
        if isinstance(profile_obj, dict):
            text = profile_obj.get(key) or ""
            return [c.strip() for c in text.split(",") if c.strip()]
        try:
            method = getattr(profile_obj, method_name)
            return method()
        except Exception:
            text = getattr(profile_obj, key, "") or ""
            return [c.strip() for c in text.split(",") if c.strip()]

    score = 30.0
    tender_blob = " ".join([
        tender.title or "", tender.description or "", tender.industry or "", tender.tender_type or "", tender.province or "", tender.buyer_name or "",
    ]).lower()

    profile_industry = text_from(profile, "industry")
    if profile_industry and tender.industry:
        if profile_industry.lower() == (tender.industry or "").lower():
            score += 25.0
        elif profile_industry.lower() in tender_blob:
            score += 15.0

    matches = 0
    for capability in list_from(profile, "capability_list", "capabilities_text"):
        if capability.lower() in tender_blob:
            matches += 1
    score += min(matches * 6.0, 30.0)

    for loc in list_from(profile, "location_list", "locations_text"):
        if tender.province and tender.province.lower() == loc.lower():
            score += 8.0
            break

    issues = profile.get("issues", []) if isinstance(profile, dict) else getattr(profile, "issues", []) or []
    pending_penalty = 0.0
    fixed_bonus = 0.0
    for issue in issues:
        status = (issue.get("status") if isinstance(issue, dict) else getattr(issue, "status", "") or "").lower()
        weight = issue.get("penalty_weight") if isinstance(issue, dict) else getattr(issue, "penalty_weight", 0)
        penalty = float(weight or 0)
        if status == "pending":
            pending_penalty += penalty
        elif status == "fixed":
            fixed_bonus += min(penalty, 2.0)
    score -= pending_penalty
    score += fixed_bonus

    if tender.closing_date:
        days = (tender.closing_date - date.today()).days
        if days >= 0:
            score += min(days / 2.0, 7.0)

    return max(0.0, min(score, 100.0))


@app.context_processor
def inject_globals():
    with get_db_session() as session:
        active_profile = get_active_profile(session)
        latest_ingest = session.execute(
            select(IngestRun).order_by(desc(IngestRun.started_at), desc(IngestRun.id)).limit(1)
        ).scalars().first()
        active_profile_dict = serialize_profile(active_profile) if active_profile else None
        latest_ingest_dict = None
        if latest_ingest:
            latest_ingest_dict = {
                "id": latest_ingest.id,
                "status": latest_ingest.status,
                "started_at": latest_ingest.started_at,
                "finished_at": latest_ingest.finished_at,
                "pages_attempted": latest_ingest.pages_attempted,
                "pages_succeeded": latest_ingest.pages_succeeded,
                "tenders_seen": latest_ingest.tenders_seen,
                "tenders_upserted": latest_ingest.tenders_upserted,
                "failure_message": latest_ingest.failure_message,
            }
        return {"active_profile": active_profile_dict, "latest_ingest": latest_ingest_dict, "today": date.today()}


@app.template_filter("days_left")
def days_left_filter(closing_date):
    if not closing_date:
        return None
    return (closing_date - date.today()).days


@app.get("/health")
def health():
    with get_db_session() as session:
        count = session.execute(select(func.count()).select_from(TenderCache)).scalar_one()
        docs = session.execute(select(func.count()).select_from(TenderDocumentCache)).scalar_one()
        return jsonify({"ok": True, "cached_tenders": count, "cached_documents": docs, "time": utcnow().isoformat()})


@app.get("/")
def home():
    with get_db_session() as session:
        active_profile = get_active_profile(session)
        total_live = session.execute(select(func.count()).select_from(TenderCache).where(TenderCache.is_live.is_(True))).scalar_one()
        tenders = session.execute(
            select(TenderCache)
            .options(selectinload(TenderCache.documents), selectinload(TenderCache.analysis_jobs))
            .where(TenderCache.is_live.is_(True))
            .order_by(TenderCache.closing_date.asc().nulls_last(), desc(TenderCache.updated_at))
            .limit(24)
        ).scalars().all()
        ranked = [{"tender": tender_to_view_model(t), "score": keyword_overlap_score(active_profile, t)} for t in tenders]
        if active_profile:
            ranked.sort(key=lambda x: (x["score"] or 0), reverse=True)
        return render_template("home.html", total_live=total_live, featured=ranked[:12])


@app.get("/tenders")
def tenders():
    with get_db_session() as session:
        active_profile = get_active_profile(session)
        province = (request.args.get("province") or "").strip()
        tender_type = (request.args.get("tender_type") or "").strip()
        industry = (request.args.get("industry") or "").strip()
        issued_from = (request.args.get("issued_from") or "").strip()
        search_text = (request.args.get("q") or "").strip()
        query = select(TenderCache).options(selectinload(TenderCache.documents), selectinload(TenderCache.analysis_jobs)).where(TenderCache.is_live.is_(True))
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
            query = query.where(or_(
                func.lower(TenderCache.title).like(like_term),
                func.lower(func.coalesce(TenderCache.description, "")).like(like_term),
                func.lower(func.coalesce(TenderCache.buyer_name, "")).like(like_term),
                func.lower(func.coalesce(TenderCache.industry, "")).like(like_term),
            ))
        items = session.execute(query.order_by(TenderCache.closing_date.asc().nulls_last(), desc(TenderCache.updated_at)).limit(200)).scalars().all()
        ranked = [{"tender": tender_to_view_model(t), "score": keyword_overlap_score(active_profile, t)} for t in items]
        if active_profile:
            ranked.sort(key=lambda x: (x["score"] or 0), reverse=True)
        provinces = session.execute(select(TenderCache.province).where(TenderCache.is_live.is_(True), TenderCache.province.is_not(None)).distinct().order_by(TenderCache.province)).scalars().all()
        tender_types = session.execute(select(TenderCache.tender_type).where(TenderCache.is_live.is_(True), TenderCache.tender_type.is_not(None)).distinct().order_by(TenderCache.tender_type)).scalars().all()
        industries = session.execute(select(TenderCache.industry).where(TenderCache.is_live.is_(True), TenderCache.industry.is_not(None)).distinct().order_by(TenderCache.industry)).scalars().all()
        return render_template("feed.html", ranked_tenders=ranked, provinces=provinces, tender_types=tender_types, industries=industries, filters={"province": province, "tender_type": tender_type, "industry": industry, "issued_from": issued_from, "q": search_text})


@app.get("/tender/<int:tender_id>")
def tender_detail(tender_id: int):
    with get_db_session() as session:
        tender = session.execute(
            select(TenderCache)
            .options(selectinload(TenderCache.documents), selectinload(TenderCache.analysis_jobs))
            .where(TenderCache.id == tender_id)
        ).scalars().first()
        if not tender:
            abort(404)
        active_profile = get_active_profile(session)
        return render_template("tender_detail.html", tender=tender_to_view_model(tender), alignment_score=keyword_overlap_score(active_profile, tender))


@app.get("/profiles")
def profiles():
    with get_db_session() as session:
        profiles_list = session.execute(select(Profile).options(selectinload(Profile.issues)).order_by(desc(Profile.updated_at), desc(Profile.id))).scalars().all()
        return render_template("profiles.html", profiles=[serialize_profile(p) for p in profiles_list])


@app.post("/profiles/upload")
def upload_profile():
    uploaded = request.files.get("profile_pdf")
    if not uploaded or not uploaded.filename.lower().endswith(".pdf"):
        flash("Please upload a PDF profile.", "error")
        return redirect(url_for("profiles"))

    try:
        text = extract_pdf_text(uploaded)
    except Exception as exc:
        flash(f"Could not read PDF: {exc}", "error")
        return redirect(url_for("profiles"))

    parsed = parse_supplier_profile_text(text, uploaded.filename) or heuristic_profile_parse(text, uploaded.filename)

    with get_db_session() as session:
        session.execute(Profile.__table__.update().values(is_active=False))
        profile = Profile(
            name=parsed.get("company_name") or os.path.splitext(uploaded.filename)[0],
            company_name=parsed.get("company_name"),
            original_filename=uploaded.filename,
            industry=parsed.get("industry"),
            capabilities_text=", ".join(parsed.get("capabilities") or []),
            locations_text=", ".join(parsed.get("locations") or []),
            extracted_text=text[:200000],
            parsed_json=json.dumps(parsed, ensure_ascii=False),
            is_active=True,
        )
        session.add(profile)
        session.flush()
        for issue in parsed.get("issues") or []:
            session.add(ProfileIssue(profile_id=profile.id, issue_type="profile_gap", title=issue.get("title") or "Profile issue", detail=issue.get("detail"), penalty_weight=float(issue.get("penalty_weight") or 5), status="pending"))
    flash("Profile uploaded and set as active.", "success")
    return redirect(url_for("profiles"))


@app.post("/profiles/<int:profile_id>/activate")
def activate_profile(profile_id: int):
    with get_db_session() as session:
        profile = session.get(Profile, profile_id)
        if not profile:
            flash("Profile not found.", "error")
            return redirect(url_for("profiles"))
        session.execute(Profile.__table__.update().values(is_active=False))
        profile.is_active = True
        flash("Active profile updated.", "success")
        return redirect(url_for("profiles"))


@app.post("/profile-issues/<int:issue_id>/status")
def update_issue_status(issue_id: int):
    status = (request.form.get("status") or "").strip().lower()
    if status not in {"pending", "fixed"}:
        flash("Invalid issue status.", "error")
        return redirect(url_for("profiles"))
    with get_db_session() as session:
        issue = session.get(ProfileIssue, issue_id)
        if not issue:
            flash("Issue not found.", "error")
            return redirect(url_for("profiles"))
        issue.status = status
        flash("Issue status updated.", "success")
        return redirect(url_for("profiles"))


def admin_allowed():
    token = os.getenv("ADMIN_TOKEN", "").strip()
    if not token:
        return True
    supplied = request.headers.get("X-Admin-Token", "") or request.args.get("token", "")
    return supplied == token


@app.route("/api/admin/run-ingest", methods=["GET", "POST"])
def api_run_ingest():
    if not admin_allowed():
        return jsonify({"ok": False, "error": "unauthorized"}), 403
    with get_db_session() as session:
        return jsonify(ingest_tenders(session=session, max_pages=int(os.getenv("INGEST_MAX_PAGES", "2"))))


@app.route("/api/admin/fetch-documents", methods=["GET", "POST"])
def api_fetch_documents():
    if not admin_allowed():
        return jsonify({"ok": False, "error": "unauthorized"}), 403
    limit = max(1, min(int(request.args.get("limit", os.getenv("FETCH_DOCUMENT_LIMIT", "20"))), 100))
    with get_db_session() as session:
        tenders = session.execute(
            select(TenderCache)
            .options(selectinload(TenderCache.documents))
            .where(TenderCache.is_live.is_(True))
            .order_by(TenderCache.closing_date.asc().nulls_last(), desc(TenderCache.updated_at))
            .limit(limit)
        ).scalars().all()
        results = fetch_documents_for_tenders(session, tenders)
        return jsonify({"ok": True, "count": len(results), "results": results})


@app.route("/api/admin/parse-documents", methods=["GET", "POST"])
def api_parse_documents():
    if not admin_allowed():
        return jsonify({"ok": False, "error": "unauthorized"}), 403
    limit = max(1, min(int(request.args.get("limit", os.getenv("PARSE_DOCUMENT_LIMIT", "20"))), 100))
    parsed = 0
    failed = []
    with get_db_session() as session:
        docs = session.execute(
            select(TenderDocumentCache)
            .options(selectinload(TenderDocumentCache.tender))
            .where(TenderDocumentCache.fetch_status.in_(["fetched", "fetched_no_text"]))
            .order_by(desc(TenderDocumentCache.updated_at), desc(TenderDocumentCache.id))
            .limit(limit)
        ).scalars().all()
        for doc in docs:
            if doc.parsed_json:
                continue
            try:
                parsed_json = parse_tender_document_text({
                    "title": doc.tender.title if doc.tender else "",
                    "buyer_name": doc.tender.buyer_name if doc.tender else "",
                    "province": doc.tender.province if doc.tender else "",
                    "closing_date": str(doc.tender.closing_date) if doc.tender and doc.tender.closing_date else "",
                    "source_url": doc.tender.source_url if doc.tender else "",
                }, doc.extracted_text or "")
                if parsed_json:
                    doc.parsed_json = json.dumps(parsed_json, ensure_ascii=False)
                    parsed += 1
            except Exception as exc:
                doc.error_message = f"parse failed: {exc}"
                failed.append({"document_id": doc.id, "error": str(exc)})
        return jsonify({"ok": True, "parsed": parsed, "failed": failed})


@app.route("/api/admin/reparse-profile/<int:profile_id>", methods=["GET", "POST"])
def api_reparse_profile(profile_id: int):
    if not admin_allowed():
        return jsonify({"ok": False, "error": "unauthorized"}), 403
    with get_db_session() as session:
        profile = session.get(Profile, profile_id)
        if not profile:
            return jsonify({"ok": False, "error": "profile_not_found"}), 404
        text = profile.extracted_text or ""
        parsed = parse_supplier_profile_text(text, profile.original_filename) or heuristic_profile_parse(text, profile.original_filename)
        profile.company_name = parsed.get("company_name") or profile.company_name
        profile.industry = parsed.get("industry") or profile.industry
        profile.capabilities_text = ", ".join(parsed.get("capabilities") or [])
        profile.locations_text = ", ".join(parsed.get("locations") or [])
        profile.parsed_json = json.dumps(parsed, ensure_ascii=False)
        session.execute(ProfileIssue.__table__.delete().where(ProfileIssue.profile_id == profile.id))
        for issue in parsed.get("issues") or []:
            session.add(ProfileIssue(profile_id=profile.id, issue_type="profile_gap", title=issue.get("title") or "Profile issue", detail=issue.get("detail"), penalty_weight=float(issue.get("penalty_weight") or 5), status="pending"))
        return jsonify({"ok": True, "profile_id": profile.id, "company_name": profile.company_name})


@app.errorhandler(404)
def not_found(_):
    return render_template("404.html"), 404


@app.errorhandler(500)
def internal_error(_):
    return render_template("500.html"), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
