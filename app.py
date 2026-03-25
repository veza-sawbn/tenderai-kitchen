import os
from datetime import date, datetime, timezone

from flask import Flask, abort, jsonify, render_template, request
from sqlalchemy import desc, func, or_, select

from database import get_db_session, init_db
from models import IngestRun, Profile, TenderCache
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


def keyword_overlap_score(profile: Profile | None, tender: TenderCache) -> float | None:
    if not profile:
        return None

    score = 30.0
    tender_blob = " ".join([
        tender.title or "",
        tender.description or "",
        tender.industry or "",
        tender.tender_type or "",
        tender.province or "",
        tender.buyer_name or "",
    ]).lower()

    if profile.industry and tender.industry and profile.industry.lower() == tender.industry.lower():
        score += 25.0
    elif profile.industry and profile.industry.lower() in tender_blob:
        score += 15.0

    capabilities = profile.capability_list()
    matches = 0
    for capability in capabilities:
        if capability.lower() in tender_blob:
            matches += 1
    score += min(matches * 6.0, 30.0)

    if tender.province and any(tender.province.lower() == loc.lower() for loc in profile.location_list()):
        score += 8.0

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
            select(IngestRun)
            .order_by(desc(IngestRun.started_at), desc(IngestRun.id))
            .limit(1)
        ).scalars().first()

        return {
            "active_profile": active_profile,
            "latest_ingest": latest_ingest,
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
            .limit(12)
        ).scalars().all()

        ranked = [{"tender": t, "score": keyword_overlap_score(active_profile, t)} for t in tenders]
        if active_profile:
            ranked.sort(key=lambda x: (x["score"] or 0), reverse=True)

        return render_template("home.html", total_live=total_live, featured=ranked)


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
            max_pages=int(os.getenv("INGEST_MAX_PAGES", "3")),
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
