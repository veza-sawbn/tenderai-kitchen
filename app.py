import io
import json
import os
import re
import tempfile
from datetime import date, datetime, timezone

from docx import Document
from flask import Flask, Response, flash, jsonify, redirect, render_template, request, send_file, url_for
from openai import OpenAI
from pypdf import PdfReader
from sqlalchemy import case, desc, func, or_, select

from database import get_db_session, init_db
from models import AnalysisJob, IngestRun, Profile, ProfileIssue, TenderCache, TenderDocumentCache
from services.etenders_ingest import ingest_tenders


def utcnow():
    return datetime.now(timezone.utc)


app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

init_db()


def get_openai_client():
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        return OpenAI(api_key=api_key)
    except Exception:
        return None


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
    cleaned = re.sub(r"[^a-zA-Z0-9,\-/& ]+", " ", text.lower())
    parts = re.split(r"[,/\n;|]+", cleaned)
    words = []
    for part in parts:
        part = " ".join(part.split()).strip()
        if part and len(part) > 2:
            words.append(part)
    unique = []
    seen = set()
    for item in words:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique[:50]


def heuristic_profile_parse(text: str, filename: str | None = None) -> dict:
    lower = text.lower()

    industry = None
    industry_rules = [
        ("Construction", ["construction", "contractor", "civil", "infrastructure"]),
        ("ICT", ["software", "ict", "technology", "systems", "digital", "it services"]),
        ("Transport", ["transport", "shuttle", "fleet", "vehicle", "logistics"]),
        ("Professional Services", ["consulting", "advisory", "facilitation", "professional services"]),
        ("Tourism", ["tourism", "travel", "adventure", "hospitality", "destination"]),
        ("Security", ["security services", "guarding", "surveillance"]),
        ("Education", ["training provider", "education", "learnership", "skills development"]),
    ]
    for label, words in industry_rules:
        if any(w in lower for w in words):
            industry = label
            break

    capabilities = []
    capability_patterns = [
        r"services\s*[:\-]\s*(.+)",
        r"capabilities\s*[:\-]\s*(.+)",
        r"core services\s*[:\-]\s*(.+)",
        r"scope\s*[:\-]\s*(.+)",
    ]
    for pattern in capability_patterns:
        matches = re.findall(pattern, text, flags=re.IGNORECASE)
        for match in matches:
            capabilities.extend(normalize_keywords(match))

    if not capabilities:
        capabilities = normalize_keywords(text)[:20]

    locations = []
    for province in [
        "Gauteng",
        "KwaZulu-Natal",
        "Western Cape",
        "Eastern Cape",
        "Free State",
        "Mpumalanga",
        "Limpopo",
        "North West",
        "Northern Cape",
    ]:
        if province.lower() in lower:
            locations.append(province)

    company_name = None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines:
        company_name = lines[0][:255]
    if filename and not company_name:
        company_name = os.path.splitext(filename)[0]

    return {
        "company_name": company_name,
        "industry": industry,
        "capabilities": capabilities[:20],
        "locations": list(dict.fromkeys(locations)),
        "issues": [],
    }


def openai_parse_profile(text: str) -> dict | None:
    client = get_openai_client()
    if not client:
        return None

    prompt = f"""
Extract supplier profile information from the following document text.
Return strict JSON with keys:
company_name, industry, capabilities, locations, issues

Rules:
- capabilities must be an array of short strings
- locations must be an array
- issues must be an array of objects with keys: title, detail, penalty_weight
- If uncertain, infer conservatively
- Do not include markdown

TEXT:
{text[:18000]}
""".strip()

    try:
        response = client.responses.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            input=prompt,
            temperature=0.2,
        )
        content = response.output_text.strip()
        data = json.loads(content)
        return data
    except Exception:
        return None


def get_active_profile(session):
    return session.execute(
        select(Profile).where(Profile.is_active.is_(True)).order_by(desc(Profile.updated_at))
    ).scalar_one_or_none()


def pending_penalty(profile: Profile | None) -> float:
    if not profile:
        return 0.0
    penalty = 0.0
    for issue in profile.issues:
        if issue.status == "pending":
            penalty += float(issue.penalty_weight or 0)
        elif issue.status == "fixed":
            penalty -= min(float(issue.penalty_weight or 0), 2.0)
    return penalty


def keyword_overlap_score(profile: Profile | None, tender: TenderCache) -> float:
    if not profile:
        return 0.0

    score = 30.0
    tender_blob = " ".join(
        [
            tender.title or "",
            tender.description or "",
            tender.industry or "",
            tender.tender_type or "",
            tender.province or "",
            tender.buyer_name or "",
        ]
    ).lower()

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

    locations = profile.location_list()
    if tender.province and any(tender.province.lower() == loc.lower() for loc in locations):
        score += 8.0

    if tender.closing_date:
        days = (tender.closing_date - date.today()).days
        if days >= 0:
            score += min(days / 2, 7)

    score -= pending_penalty(profile)
    return max(0.0, min(score, 100.0))


def openai_analyze(profile: Profile, tender: TenderCache) -> dict | None:
    client = get_openai_client()
    if not client:
        return None

    prompt = f"""
Compare this supplier profile to this tender and return strict JSON.

JSON keys:
score, summary, strengths, risks, recommendations

Rules:
- score is 0 to 100
- strengths, risks, recommendations are arrays of short bullet-style strings
- be practical and procurement-aware
- do not output markdown

SUPPLIER PROFILE:
Company: {profile.company_name}
Industry: {profile.industry}
Capabilities: {profile.capabilities_text}
Locations: {profile.locations_text}
Issues:
{chr(10).join([f"- {i.title} ({i.status})" for i in profile.issues])}

TENDER:
Title: {tender.title}
Buyer: {tender.buyer_name}
Province: {tender.province}
Type: {tender.tender_type}
Industry: {tender.industry}
Issued: {tender.issued_date}
Closing: {tender.closing_date}
Description:
{(tender.description or "")[:6000]}
""".strip()

    try:
        response = client.responses.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            input=prompt,
            temperature=0.2,
        )
        content = response.output_text.strip()
        return json.loads(content)
    except Exception:
        return None


def fallback_analyze(profile: Profile, tender: TenderCache) -> dict:
    score = keyword_overlap_score(profile, tender)

    strengths = []
    risks = []
    recommendations = []

    if profile.industry and tender.industry and profile.industry.lower() == tender.industry.lower():
        strengths.append("Industry alignment is strong.")
    if tender.province and tender.province in profile.location_list():
        strengths.append("Geographic relevance matches the active supplier footprint.")
    if score >= 70:
        strengths.append("Capability overlap suggests a competitive fit.")

    pending = [i for i in profile.issues if i.status == "pending"]
    if pending:
        risks.append("Pending supplier profile issues may weaken compliance readiness.")
        recommendations.append("Resolve pending profile issues before submission.")

    if not strengths:
        strengths.append("Some baseline overlap exists between profile keywords and tender requirements.")

    if score < 60:
        risks.append("Tender requirements appear broader than the current profile strength.")
        recommendations.append("Sharpen capability evidence and supporting credentials in the bid.")

    if not recommendations:
        recommendations.append("Emphasize exact capability match, delivery history, and compliance readiness.")

    summary = (
        f"This opportunity scored {round(score, 1)}/100 using fallback scoring based on "
        f"industry, capability overlap, location relevance, and issue penalties."
    )

    return {
        "score": round(score, 1),
        "summary": summary,
        "strengths": strengths,
        "risks": risks,
        "recommendations": recommendations,
    }


def get_or_create_analysis(session, profile: Profile, tender: TenderCache) -> AnalysisJob:
    existing = session.execute(
        select(AnalysisJob)
        .where(AnalysisJob.profile_id == profile.id, AnalysisJob.tender_id == tender.id)
        .order_by(desc(AnalysisJob.updated_at))
    ).scalar_one_or_none()
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

    ai_result = openai_analyze(profile, tender)
    result = ai_result or fallback_analyze(profile, tender)

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
Company: {profile.company_name}
Industry: {profile.industry}
Capabilities: {profile.capabilities_text}
Locations: {profile.locations_text}

TENDER:
Title: {tender.title}
Buyer: {tender.buyer_name}
Province: {tender.province}
Type: {tender.tender_type}
Industry: {tender.industry}
Issued: {tender.issued_date}
Closing: {tender.closing_date}
Description:
{(tender.description or "")[:7000]}

ANALYSIS:
Summary: {job.summary}
Strengths: {job.strengths_text}
Risks: {job.risks_text}
Recommendations: {job.recommendations_text}
""".strip()
        try:
            response = client.responses.create(
                model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
                input=prompt,
                temperature=0.4,
            )
            proposal = response.output_text.strip()
            if proposal:
                return proposal
        except Exception:
            pass

    return f"""
Proposal Draft

Supplier
{profile.company_name or "Supplier"}

Tender
{tender.title}

Introduction
We submit this expression of interest / proposal draft in response to the above tender opportunity. Our team believes there is meaningful alignment between the tender requirements and our operational capability profile.

Why We Fit
{job.strengths_text or "Our capability profile shows relevant overlap with the tender scope."}

Delivery Approach
We would structure delivery around the exact specifications, timelines, reporting obligations, and compliance expectations stated in the tender documents. We would prioritize clear mobilisation, accountable project controls, and consistent communication with the contracting authority.

Risk Management
{job.risks_text or "We will manage submission and delivery risk through documented controls and early compliance checks."}

Next Steps
We recommend finalising all compliance documentation, tailoring the methodology to the scope, and strengthening evidence for the most relevant past experience before submission.
""".strip()


def build_proposal_docx(profile: Profile, tender: TenderCache, job: AnalysisJob) -> io.BytesIO:
    if not job.proposal_draft_text:
        job.proposal_draft_text = generate_proposal_text(profile, tender, job)

    doc = Document()
    doc.add_heading("Tender Proposal Draft", 0)

    doc.add_paragraph(f"Supplier: {profile.company_name or profile.name or 'Supplier'}")
    doc.add_paragraph(f"Tender: {tender.title}")
    doc.add_paragraph(f"Buyer: {tender.buyer_name or 'N/A'}")
    doc.add_paragraph(f"Province: {tender.province or 'N/A'}")
    doc.add_paragraph(f"Closing Date: {tender.closing_date or 'N/A'}")
    doc.add_paragraph(f"Alignment Score: {round(job.score or 0, 1)}/100")

    doc.add_heading("Draft", level=1)
    for paragraph in (job.proposal_draft_text or "").split("\n\n"):
        doc.add_paragraph(paragraph.strip())

    buffer = io.BytesIO()
    doc.save(buffer)
    buffer.seek(0)
    return buffer


@app.context_processor
def inject_globals():
    with get_db_session() as session:
        active_profile = get_active_profile(session)
        latest_ingest = session.execute(select(IngestRun).order_by(desc(IngestRun.started_at))).scalar_one_or_none()
        return {
            "active_profile": active_profile,
            "latest_ingest": latest_ingest,
            "today": date.today(),
        }


@app.get("/")
def home():
    with get_db_session() as session:
        active_profile = get_active_profile(session)

        total_live = session.execute(
            select(func.count()).select_from(TenderCache).where(TenderCache.is_live.is_(True))
        ).scalar_one()

        q = select(TenderCache).where(TenderCache.is_live.is_(True))
        if active_profile:
            score_expr = case(
                (
                    func.lower(TenderCache.industry) == func.lower(active_profile.industry or ""),
                    100,
                ),
                else_=0,
            )
            q = q.order_by(desc(score_expr), TenderCache.closing_date.asc().nulls_last(), desc(TenderCache.updated_at))
        else:
            q = q.order_by(TenderCache.closing_date.asc().nulls_last(), desc(TenderCache.updated_at))

        featured = session.execute(q.limit(12)).scalars().all()

        ranked = []
        for tender in featured:
            rank_score = keyword_overlap_score(active_profile, tender) if active_profile else None
            ranked.append({"tender": tender, "score": rank_score})

        return render_template("home.html", total_live=total_live, featured=ranked)


@app.get("/tenders")
def feed():
    with get_db_session() as session:
        active_profile = get_active_profile(session)

        province = (request.args.get("province") or "").strip()
        tender_type = (request.args.get("tender_type") or "").strip()
        industry = (request.args.get("industry") or "").strip()
        issued_from = (request.args.get("issued_from") or "").strip()
        search_text = (request.args.get("q") or "").strip()

        q = select(TenderCache).where(TenderCache.is_live.is_(True))

        if province:
            q = q.where(TenderCache.province == province)
        if tender_type:
            q = q.where(TenderCache.tender_type == tender_type)
        if industry:
            q = q.where(TenderCache.industry == industry)
        if issued_from:
            try:
                issued_from_date = datetime.strptime(issued_from, "%Y-%m-%d").date()
                q = q.where(TenderCache.issued_date >= issued_from_date)
            except ValueError:
                pass
        if search_text:
            like_term = f"%{search_text.lower()}%"
            q = q.where(
                or_(
                    func.lower(TenderCache.title).like(like_term),
                    func.lower(func.coalesce(TenderCache.description, "")).like(like_term),
                    func.lower(func.coalesce(TenderCache.buyer_name, "")).like(like_term),
                    func.lower(func.coalesce(TenderCache.industry, "")).like(like_term),
                )
            )

        tenders = session.execute(
            q.order_by(TenderCache.closing_date.asc().nulls_last(), desc(TenderCache.updated_at)).limit(200)
        ).scalars().all()

        ranked = []
        for tender in tenders:
            ranked.append({"tender": tender, "score": keyword_overlap_score(active_profile, tender) if active_profile else None})

        if active_profile:
            ranked.sort(key=lambda item: (item["score"] or 0), reverse=True)

        provinces = session.execute(
            select(TenderCache.province).where(TenderCache.is_live.is_(True), TenderCache.province.is_not(None)).distinct().order_by(TenderCache.province)
        ).scalars().all()
        tender_types = session.execute(
            select(TenderCache.tender_type).where(TenderCache.is_live.is_(True), TenderCache.tender_type.is_not(None)).distinct().order_by(TenderCache.tender_type)
        ).scalars().all()
        industries = session.execute(
            select(TenderCache.industry).where(TenderCache.is_live.is_(True), TenderCache.industry.is_not(None)).distinct().order_by(TenderCache.industry)
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
            flash("Tender not found.", "error")
            return redirect(url_for("feed"))

        active_profile = get_active_profile(session)
        analysis = None
        score = None
        if active_profile:
            score = keyword_overlap_score(active_profile, tender)
            analysis = session.execute(
                select(AnalysisJob)
                .where(AnalysisJob.profile_id == active_profile.id, AnalysisJob.tender_id == tender.id)
                .order_by(desc(AnalysisJob.updated_at))
            ).scalar_one_or_none()

        documents = session.execute(
            select(TenderDocumentCache).where(TenderDocumentCache.tender_id == tender.id).order_by(desc(TenderDocumentCache.updated_at))
        ).scalars().all()

        return render_template(
            "tender_detail.html",
            tender=tender,
            alignment_score=score,
            analysis=analysis,
            documents=documents,
        )


@app.get("/profiles")
def profiles():
    with get_db_session() as session:
        profiles_list = session.execute(select(Profile).order_by(desc(Profile.updated_at))).scalars().all()
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

    ai_data = openai_parse_profile(text)
    parsed = ai_data or heuristic_profile_parse(text, uploaded.filename)

    with get_db_session() as session:
        session.execute(
            Profile.__table__.update().values(is_active=False)
        )

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

        issues = parsed.get("issues") or []
        if not issues:
            heuristic_issues = []
            if not profile.capabilities_text:
                heuristic_issues.append(
                    {"title": "Capabilities are not clearly structured", "detail": "The uploaded profile should list clearer capabilities.", "penalty_weight": 6}
                )
            if not profile.locations_text:
                heuristic_issues.append(
                    {"title": "Operational locations are not clearly stated", "detail": "Province or service geography is unclear.", "penalty_weight": 4}
                )
            issues = heuristic_issues

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


@app.post("/tender/<int:tender_id>/analyze")
def analyze_tender(tender_id: int):
    with get_db_session() as session:
        tender = session.get(TenderCache, tender_id)
        profile = get_active_profile(session)

        if not tender:
            flash("Tender not found.", "error")
            return redirect(url_for("feed"))
        if not profile:
            flash("Upload and activate a supplier profile first.", "error")
            return redirect(url_for("profiles"))

        job = run_analysis(session, profile, tender)
        flash(f"Tender analyzed. Score: {round(job.score or 0, 1)}/100", "success")
        return redirect(url_for("tender_detail", tender_id=tender.id))


@app.get("/analysis/<int:job_id>/proposal.docx")
def download_proposal(job_id: int):
    with get_db_session() as session:
        job = session.get(AnalysisJob, job_id)
        if not job or not job.profile or not job.tender:
            flash("Proposal source not found.", "error")
            return redirect(url_for("home"))

        buffer = build_proposal_docx(job.profile, job.tender, job)
        filename = f"proposal_{job.tender.id}_{job.profile.id}.docx"
        return send_file(
            buffer,
            as_attachment=True,
            download_name=filename,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )


@app.post("/api/admin/run-ingest")
def api_run_ingest():
    with get_db_session() as session:
        result = ingest_tenders(session=session, max_pages=int(os.getenv("INGEST_MAX_PAGES", "20")))
        return jsonify(result)


@app.get("/admin/run-ingest")
def admin_run_ingest():
    with get_db_session() as session:
        result = ingest_tenders(session=session, max_pages=int(os.getenv("INGEST_MAX_PAGES", "20")))
        if result["status"] in {"completed", "partial_success"}:
            flash(f"Ingest finished: {result['tenders_seen']} active tenders processed.", "success")
        else:
            flash(f"Ingest failed: {result.get('failure_message')}", "error")
    return redirect(url_for("home"))


@app.get("/health")
def health():
    with get_db_session() as session:
        tender_count = session.execute(
            select(func.count()).select_from(TenderCache)
        ).scalar_one()
        return jsonify({"ok": True, "cached_tenders": tender_count})


@app.template_filter("days_left")
def days_left_filter(closing_date):
    if not closing_date:
        return None
    return (closing_date - date.today()).days


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
