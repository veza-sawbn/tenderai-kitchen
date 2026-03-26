import json
import os
import tempfile
from datetime import date, datetime, timezone

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for
from pypdf import PdfReader
from sqlalchemy import desc, func, or_, select

from database import get_db_session, init_db
from models import IngestRun, Profile, ProfileIssue, TenderCache
from services.etenders_ingest import ingest_tenders


def utcnow():
    return datetime.now(timezone.utc)


app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "tenderai-dev-fallback-secret")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

init_db()


def get_active_profile(session):
    return session.execute(
        select(Profile)
        .where(Profile.is_active.is_(True))
        .order_by(desc(Profile.updated_at))
        .limit(1)
    ).scalars().first()


def extract_pdf_text(file_storage) -> str:
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
    lower = text.lower()

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
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    for line in lines:
        ll = line.lower()
        if any(marker in ll for marker in markers):
            capabilities.extend(normalize_keywords(line))

    if not capabilities:
        capabilities = normalize_keywords(text[:3000])[:20]

    locations = []
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
    for province in provinces:
        if province.lower() in lower:
            locations.append(province)

    company_name = None
    if lines:
        company_name = lines[0][:255]
    if not company_name and filename:
        company_name = os.path.splitext(filename)[0]

    issues = []
    if not capabilities:
        issues.append({
            "title": "Capabilities are not clearly structured",
            "detail": "The profile does not clearly list service capabilities.",
            "penalty_weight": 6,
        })
    if not locations:
        issues.append({
            "title": "Operating locations are unclear",
            "detail": "The profile does not clearly indicate service provinces or locations.",
            "penalty_weight": 4,
        })
    if not industry:
        issues.append({
            "title": "Industry focus is unclear",
            "detail": "The profile does not strongly indicate the primary industry.",
            "penalty_weight": 5,
        })

    return {
        "company_name": company_name,
        "industry": industry,
        "capabilities": capabilities[:20],
        "locations": list(dict.fromkeys(locations)),
        "issues": issues,
    }


def keyword_overlap_score(profile: Profile | None, tender: TenderCache) -> float | None:
    if not profile:
        return None

    # fallback methods if the profile object is missing helpers
    def get_capabilities():
        try:
            return profile.capability_list()
        except Exception:
            text = getattr(profile, "capabilities_text", "") or ""
            return [c.strip() for c in text.split(",") if c.strip()]

    def get_locations():
        try:
            return profile.location_list()
        except Exception:
            text = getattr(profile, "locations_text", "") or ""
            return [c.strip() for c in text.split(",") if c.strip()]

    score = 30.0
    tender_blob = " ".join([
        tender.title or "",
        tender.description or "",
        tender.industry or "",
        tender.tender_type or "",
        tender.province or "",
        tender.buyer_name or "",
    ]).lower()

    try:
        # Industry match
        if profile.industry and tender.industry and profile.industry.lower() == tender.industry.lower():
            score += 25.0
        elif profile.industry and profile.industry.lower() in tender_blob:
            score += 15.0
    except Exception:
        # gracefully skip if unexpected types
        pass

    # Capabilities overlap
    matches = 0
    for capability in get_capabilities():
        try:
            if capability.lower() in tender_blob:
                matches += 1
        except Exception:
            continue
    score += min(matches * 6.0, 30.0)

    # Location match
    try:
        for loc in get_locations():
            if tender.province and tender.province.lower() == loc.lower():
                score += 8.0
                break
    except Exception:
        pass

    # Pending/fixed issues penalties/bonuses
    pending_penalty = 0.0
    fixed_bonus = 0.0
    try:
        issues = getattr(profile, "issues", []) or []
        for issue in issues:
            st = getattr(issue, "status", "").lower()
            penalty = float(getattr(issue, "penalty_weight", 0) or 0)
            if st == "pending":
                pending_penalty += penalty
            elif st == "fixed":
                # small bonus for fixed issues, cap per issue
                fixed_bonus += min(penalty, 2.0)
    except Exception:
        pass

    score -= pending_penalty
    score += fixed_bonus

    # Days left bonus
    try:
        if tender.closing_date:
            days = (tender.closing_date - date.today()).days
            if days >= 0:
                score += min(days / 2.0, 7.0)
    except Exception:
        pass

    # Clamp
    try:
        score = max(0.0, min(score, 100.0))
    except Exception:
        score = 0.0

    return score


@app.context_processor
def inject_globals():
    with get_db_session() as session:
        active_profile = get_active_profile(session)
        latest_ingest = session.execute(
            select(IngestRun)
            .order_by(desc(IngestRun.started_at), desc(IngestRun.id))
            .limit(1)
        ).scalars().first()

        active_profile_dict = None
        if active_profile:
            active_profile_dict = {
                "id": active_profile.id,
                "company_name": active_profile.company_name,
                "is_active": active_profile.is_active,
                "updated_at": active_profile.updated_at,
                "industry": active_profile.industry,
                "capabilities_text": active_profile.capabilities_text,
                "locations_text": active_profile.locations_text,
            }

        latest_ingest_dict = None
        if latest_ingest:
            latest_ingest_dict = {
                "status": latest_ingest.status,
                "started_at": latest_ingest.started_at,
            }

        return {
            "active_profile": active_profile_dict,
            "latest_ingest": latest_ingest_dict,
            "today": date.today(),
        }


@app.template_filter("days_left")
def days_left_filter(closing_date):
    if not closing_date:
        return None
    return (closing_date - date.today()).days


@app.get("/health")
def health():
    with get_db_session() as session:
        count = session.execute(
            select(func.count()).select_from(TenderCache)
        ).scalar_one()
        return jsonify({
            "ok": True,
            "cached_tenders": count,
            "time": utcnow().isoformat(),
        })


@app.get("/")
def home():
    with get_db_session() as session:
        active_profile = get_active_profile(session)

        total_live = session.execute(
            select(func.count()).select_from(TenderCache).where(TenderCache.is_live.is_(True))
        ).scalar_one()

        tenders = session.execute(
            select(TenderCache)
            .where(TenderCache.is_live.is_(True))
            .order_by(TenderCache.closing_date.asc().nulls_last(), desc(TenderCache.updated_at))
            .limit(24)
        ).scalars().all()

        ranked = [{"tender": t, "score": keyword_overlap_score(active_profile, t)} for t in tenders]
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

        items = session.execute(
            query.order_by(TenderCache.closing_date.asc().nulls_last(), desc(TenderCache.updated_at)).limit(200)
        ).scalars().all()

        ranked = [{"tender": t, "score": keyword_overlap_score(active_profile, t)} for t in items]
        if active_profile:
            ranked.sort(key=lambda x: (x["score"] or 0), reverse=True)

        provinces = session.execute(
            select(TenderCache.province)
            .where(TenderCache.is_live.is_(True), TenderCache.province.is_not(None))
            .distinct()
            .order_by(TenderCache.province)
        ).scalars().all()

        tender_types = session.execute(
            select(TenderCache.tender_type)
            .where(TenderCache.is_live.is_(True), TenderCache.tender_type.is_not(None))
            .distinct()
            .order_by(TenderCache.tender_type)
        ).scalars().all()

        industries = session.execute(
            select(TenderCache.industry)
            .where(TenderCache.is_live.is_(True), TenderCache.industry.is_not(None))
            .distinct()
            .order_by(TenderCache.industry)
        ).scalars().all()

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
            },
        )


@app.get("/tender/<int:tender_id>")
def tender_detail(tender_id: int):
    with get_db_session() as session:
        tender = session.get(TenderCache, tender_id)
        if not tender:
            abort(404)

        active_profile = get_active_profile(session)
        score = keyword_overlap_score(active_profile, tender)

        return render_template(
            "tender_detail.html",
            tender=tender,
            alignment_score=score,
        )


@app.get("/profiles")
def profiles():
    with get_db_session() as session:
        profiles_list = session.execute(
            select(Profile).order_by(desc(Profile.updated_at))
        ).scalars().all()
        return render_template("profiles.html", profiles=profiles_list)


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

    parsed = heuristic_profile_parse(text, uploaded.filename)

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


@app.post("/api/admin/run-ingest")
@app.get("/api/admin/run-ingest")
def api_run_ingest():
    if not admin_allowed():
        return jsonify({"ok": False, "error": "unauthorized"}), 403

    with get_db_session() as session:
        result = ingest_tenders(
            session=session,
            max_pages=int(os.getenv("INGEST_MAX_PAGES", "1")),
        )
        return jsonify(result)


@app.errorhandler(404)
def not_found(_):
    return render_template("404.html"), 404


@app.errorhandler(500)
def internal_error(_):
    return render_template("500.html"), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
