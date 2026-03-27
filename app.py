import json
import os
import re
import tempfile
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        return False

import requests
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for
from pypdf import PdfReader
from sqlalchemy import desc, func, or_, select

try:
    from docx import Document as DocxDocument
except ImportError:
    DocxDocument = None

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

from database import get_db_session, init_db
from models import AnalysisJob, IngestRun, Profile, ProfileIssue, TenderCache, TenderDocumentCache
from services.etenders_ingest import ingest_tenders


load_dotenv()


def utcnow():
    return datetime.now(timezone.utc)


app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "tenderai-dev-fallback-secret")
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024

init_db()

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")


def get_openai_client():
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key or OpenAI is None:
        return None
    try:
        return OpenAI(api_key=api_key)
    except Exception:
        return None


def json_from_text(value: str) -> dict:
    if not value:
        return {}
    text = value.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        return {}


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


def admin_allowed() -> bool:
    token = (os.getenv("ADMIN_TOKEN") or "").strip()
    if not token:
        return True
    supplied = (request.headers.get("X-Admin-Token") or request.args.get("token") or "").strip()
    return supplied == token


def get_active_profile(session) -> Optional[Profile]:
    return session.execute(
        select(Profile)
        .where(Profile.is_active.is_(True))
        .order_by(desc(Profile.updated_at))
        .limit(1)
    ).scalars().first()


def normalize_keywords(text: str) -> List[str]:
    if not text:
        return []
    raw = text.replace("\n", ",").replace(";", ",").replace("|", ",").split(",")
    cleaned = []
    seen = set()
    for item in raw:
        value = " ".join(item.strip().split())
        if len(value) >= 3:
            key = value.lower()
            if key not in seen:
                seen.add(key)
                cleaned.append(value)
    return cleaned[:30]


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


def extract_docx_text(path: str) -> str:
    if DocxDocument is None:
        return ""
    try:
        doc = DocxDocument(path)
        return "\n".join([p.text for p in doc.paragraphs if p.text]).strip()
    except Exception:
        return ""


def parse_profile_with_openai(text: str, filename: str | None = None) -> dict:
    client = get_openai_client()
    if not client or not text.strip():
        return {}

    prompt = f"""
You are TenderAI. Extract structured supplier profile information.
Return JSON only.

Schema:
{{
  "company_name": "string or null",
  "industry": "string or null",
  "capabilities": ["string"],
  "locations": ["string"],
  "issues": [
    {{
      "title": "string",
      "detail": "string",
      "penalty_weight": 0
    }}
  ]
}}

Profile filename: {filename or ""}
Profile text:
{text[:22000]}
""".strip()

    try:
        response = client.responses.create(model=OPENAI_MODEL, input=prompt)
        parsed = json_from_text(response.output_text)
        if parsed:
            parsed["_parse_mode"] = "openai"
        return parsed
    except Exception:
        return {}


def heuristic_profile_parse(text: str, filename: str | None = None) -> dict:
    lower = text.lower()

    industry = None
    rules = [
        ("Construction", ["construction", "contractor", "civil", "infrastructure", "building"]),
        ("ICT", ["software", "ict", "technology", "systems", "digital", "it services"]),
        ("Transport", ["transport", "shuttle", "fleet", "vehicle", "logistics"]),
        ("Professional Services", ["consulting", "advisory", "facilitation", "professional services"]),
        ("Tourism", ["tourism", "travel", "adventure", "hospitality", "destination"]),
        ("Security", ["security services", "guarding", "surveillance"]),
        ("Education", ["training provider", "education", "learnership", "skills development"]),
    ]
    for label, words in rules:
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
    for province in [
        "Gauteng", "KwaZulu-Natal", "Western Cape", "Eastern Cape",
        "Free State", "Mpumalanga", "Limpopo", "North West", "Northern Cape",
    ]:
        if province.lower() in lower:
            locations.append(province)

    company_name = lines[0][:255] if lines else None
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
        "_parse_mode": "heuristic",
    }


def parse_profile_text(text: str, filename: str | None = None) -> dict:
    parsed = parse_profile_with_openai(text, filename)
    if parsed:
        return parsed
    return heuristic_profile_parse(text, filename)


def serialize_profile(profile: Profile, include_issues: bool = True) -> dict:
    parsed = safe_loads(profile.parsed_json, {})
    data = {
        "id": profile.id,
        "name": profile.name,
        "company_name": profile.company_name,
        "original_filename": profile.original_filename,
        "industry": profile.industry,
        "capabilities_text": profile.capabilities_text,
        "locations_text": profile.locations_text,
        "extracted_text": profile.extracted_text,
        "parsed_json": parsed,
        "is_active": profile.is_active,
        "created_at": profile.created_at.isoformat() if profile.created_at else None,
        "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
    }
    if include_issues:
        issues = getattr(profile, "issues", []) or []
        data["issues"] = [
            {
                "id": issue.id,
                "issue_type": issue.issue_type,
                "title": issue.title,
                "detail": issue.detail,
                "status": issue.status,
                "penalty_weight": issue.penalty_weight,
            }
            for issue in issues
        ]
    return data


def tender_to_view_model(t: TenderCache) -> dict:
    return {
        "id": t.id,
        "tender_uid": t.tender_uid,
        "title": t.title,
        "description": t.description,
        "industry": t.industry,
        "tender_type": t.tender_type,
        "province": t.province,
        "buyer_name": t.buyer_name,
        "issued_date": t.issued_date.isoformat() if t.issued_date else None,
        "closing_date": t.closing_date.isoformat() if t.closing_date else None,
        "document_url": t.document_url,
        "source_url": t.source_url,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        "is_live": t.is_live,
    }


def keyword_overlap_score(profile: Profile | None, tender: TenderCache) -> float | None:
    if not profile:
        return None

    capabilities = [c.strip() for c in (profile.capabilities_text or "").split(",") if c.strip()]
    locations = [c.strip() for c in (profile.locations_text or "").split(",") if c.strip()]

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

    matches = 0
    for capability in capabilities:
        if capability.lower() in tender_blob:
            matches += 1
    score += min(matches * 6.0, 30.0)

    for loc in locations:
        if tender.province and tender.province.lower() == loc.lower():
            score += 8.0
            break

    issues = getattr(profile, "issues", []) or []
    pending_penalty = 0.0
    fixed_bonus = 0.0
    for issue in issues:
        st = (getattr(issue, "status", "") or "").lower()
        penalty = float(getattr(issue, "penalty_weight", 0) or 0)
        if st == "pending":
            pending_penalty += penalty
        elif st == "fixed":
            fixed_bonus += min(penalty, 2.0)

    score -= pending_penalty
    score += fixed_bonus

    try:
        if tender.closing_date:
            days = (tender.closing_date - date.today()).days
            if days >= 0:
                score += min(days / 2.0, 7.0)
    except Exception:
        pass

    return max(0.0, min(score, 100.0))


def build_fit_reasons(profile: Profile | None, tender: TenderCache) -> List[str]:
    if not profile:
        return []

    reasons = []
    capabilities = [x.strip() for x in (profile.capabilities_text or "").split(",") if x.strip()]
    locations = [x.strip() for x in (profile.locations_text or "").split(",") if x.strip()]
    tender_blob = f"{tender.title or ''} {tender.description or ''}".lower()

    matched_caps = [cap for cap in capabilities if cap.lower() in tender_blob]
    if matched_caps:
        reasons.append(f"Matches your service keywords: {', '.join(matched_caps[:4])}")

    if profile.industry and tender.industry and profile.industry.lower() == tender.industry.lower():
        reasons.append(f"Industry alignment with {tender.industry}")

    if locations and tender.province and any(loc.lower() == tender.province.lower() for loc in locations):
        reasons.append(f"Location alignment in {tender.province}")

    if tender.buyer_name:
        reasons.append(f"Issued by {tender.buyer_name}")

    return reasons


def fit_band_from_score(score: Optional[float]) -> Optional[str]:
    if score is None:
        return None
    if score >= 75:
        return "high_potential"
    if score >= 45:
        return "possible_fit"
    return "low_fit"


def build_fit_summary(score: Optional[float], reasons: List[str]) -> Optional[str]:
    if score is None:
        return None
    if reasons:
        return "This tender could be a good fit because " + "; ".join(reasons[:3]) + "."
    if score >= 60:
        return "This tender shows promising metadata alignment with your active profile."
    return "This tender has limited visible alignment from metadata alone and may require careful review."


def upsert_profile_issues(session, profile: Profile, issues: List[dict]):
    existing = session.execute(
        select(ProfileIssue).where(ProfileIssue.profile_id == profile.id)
    ).scalars().all()
    for issue in existing:
        session.delete(issue)
    session.flush()

    for issue in issues or []:
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


def latest_analysis_for(session, tender_id: int, profile_id: Optional[int]) -> Optional[dict]:
    if not profile_id:
        return None

    job = session.execute(
        select(AnalysisJob)
        .where(
            AnalysisJob.tender_id == tender_id,
            AnalysisJob.profile_id == profile_id,
        )
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
        "strengths": raw.get("strengths") or [x.strip() for x in (job.strengths_text or "").split("\n") if x.strip()],
        "gaps": raw.get("gaps") or [],
        "risks": raw.get("risks") or [x.strip() for x in (job.risks_text or "").split("\n") if x.strip()],
        "recommendation": job.recommendations_text,
        "error_message": job.error_message,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


def fetch_tender_document(session, tender: TenderCache) -> dict:
    document_url = tender.document_url or tender.source_url
    if not document_url:
        return {"ok": False, "error": "No tender document URL available."}

    doc = session.execute(
        select(TenderDocumentCache)
        .where(
            TenderDocumentCache.tender_id == tender.id,
            TenderDocumentCache.document_url == document_url,
        )
        .limit(1)
    ).scalars().first()

    if not doc:
        doc = TenderDocumentCache(tender_id=tender.id, document_url=document_url)
        session.add(doc)
        session.flush()

    try:
        response = requests.get(document_url, timeout=25)
        response.raise_for_status()

        content_type = (response.headers.get("Content-Type") or "").lower()
        filename = document_url.split("/")[-1][:255] if "/" in document_url else None

        suffix = ".pdf"
        if "wordprocessingml" in content_type or document_url.lower().endswith(".docx"):
            suffix = ".docx"
        elif "pdf" not in content_type and not document_url.lower().endswith(".pdf"):
            suffix = ".bin"

        extracted_text = ""
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(response.content)
            temp_path = tmp.name

        try:
            if suffix == ".pdf":
                extracted_text = extract_pdf_text(temp_path)
            elif suffix == ".docx":
                extracted_text = extract_docx_text(temp_path)
        finally:
            try:
                os.unlink(temp_path)
            except Exception:
                pass

        doc.filename = filename
        doc.content_type = content_type[:100] if content_type else None
        doc.binary_content = response.content
        doc.extracted_text = extracted_text
        doc.fetch_status = "fetched"
        doc.error_message = None
        doc.fetched_at = utcnow()
        session.flush()

        return {"ok": True, "text_len": len(extracted_text)}
    except Exception as exc:
        doc.fetch_status = "fetch_failed"
        doc.error_message = str(exc)
        session.flush()
        return {"ok": False, "error": str(exc)}


def analyze_tender_against_profile(tender: TenderCache, profile: Profile, document_text: str) -> dict:
    client = get_openai_client()

    profile_json = safe_loads(profile.parsed_json, {})
    tender_vm = tender_to_view_model(tender)

    if client and document_text.strip():
        prompt = f"""
You are TenderAI, a procurement intelligence assistant.
Analyse the tender document against the supplier profile.
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
{json.dumps(profile_json, ensure_ascii=False, default=str)[:12000]}

Tender metadata:
{json.dumps(tender_vm, ensure_ascii=False, default=str)[:6000]}

Tender document text:
{document_text[:22000]}
""".strip()

        try:
            response = client.responses.create(model=OPENAI_MODEL, input=prompt)
            parsed = json_from_text(response.output_text)
            if parsed:
                parsed["_analysis_mode"] = "openai"
                return parsed
        except Exception:
            pass

    score = keyword_overlap_score(profile, tender) or 0
    reasons = build_fit_reasons(profile, tender)

    return {
        "score": score,
        "summary": build_fit_summary(score, reasons) or "Fallback analysis was used.",
        "strengths": reasons[:4],
        "gaps": ["Detailed AI interpretation was unavailable."],
        "risks": [] if score >= 60 else ["Profile alignment appears limited from the available metadata."],
        "recommendation": "Proceed to full response preparation." if score >= 60 else "Review carefully before committing resources.",
        "_analysis_mode": "heuristic",
    }


@app.context_processor
def inject_globals():
    with get_db_session() as session:
        active_profile = get_active_profile(session)
        latest_ingest = session.execute(
            select(IngestRun).order_by(desc(IngestRun.started_at), desc(IngestRun.id)).limit(1)
        ).scalars().first()

        return {
            "active_profile": serialize_profile(active_profile, include_issues=False) if active_profile else None,
            "latest_ingest": {
                "status": latest_ingest.status,
                "started_at": latest_ingest.started_at.isoformat() if latest_ingest.started_at else None,
            } if latest_ingest else None,
            "today": date.today().isoformat(),
        }


@app.template_filter("days_left")
def days_left_filter(closing_date):
    if not closing_date:
        return None
    if isinstance(closing_date, str):
        try:
            closing_date = date.fromisoformat(closing_date)
        except Exception:
            return None
    return (closing_date - date.today()).days


@app.get("/health")
def health():
    with get_db_session() as session:
        count = session.execute(select(func.count()).select_from(TenderCache)).scalar_one()
        return jsonify({"ok": True, "cached_tenders": count, "time": utcnow().isoformat()})


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

        featured = []
        for t in tenders:
            score = keyword_overlap_score(active_profile, t)
            reasons = build_fit_reasons(active_profile, t)
            featured.append({
                "tender": tender_to_view_model(t),
                "score": score,
                "fit_band": fit_band_from_score(score),
                "fit_summary": build_fit_summary(score, reasons),
            })

        if active_profile:
            featured.sort(key=lambda x: (x["score"] or 0), reverse=True)

        return render_template("home.html", total_live=total_live, featured=featured[:12])


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

        ranked = []
        for t in items:
            score = keyword_overlap_score(active_profile, t)
            reasons = build_fit_reasons(active_profile, t)
            ranked.append({
                "tender": tender_to_view_model(t),
                "score": score,
                "fit_band": fit_band_from_score(score),
                "fit_summary": build_fit_summary(score, reasons),
                "fit_reasons": reasons,
            })

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
        reasons = build_fit_reasons(active_profile, tender)
        fit_band = fit_band_from_score(score)
        fit_summary = build_fit_summary(score, reasons)
        latest_analysis = latest_analysis_for(session, tender_id, active_profile.id if active_profile else None)

        return render_template(
            "tender_detail.html",
            tender=tender_to_view_model(tender),
            alignment_score=score,
            fit_summary=fit_summary,
            fit_reasons=reasons,
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

        job = AnalysisJob(
            profile_id=active_profile.id,
            tender_id=tender_id,
            status="running",
        )
        session.add(job)
        session.flush()

        try:
            document_url = tender.document_url or tender.source_url
            if not document_url:
                job.status = "failed"
                job.error_message = "No tender document URL is available for this tender."
                flash(job.error_message, "error")
                return redirect(url_for("tender_detail", tender_id=tender_id))

            doc = session.execute(
                select(TenderDocumentCache)
                .where(
                    TenderDocumentCache.tender_id == tender_id,
                    TenderDocumentCache.document_url == document_url,
                )
                .limit(1)
            ).scalars().first()

            if not doc or doc.fetch_status != "fetched":
                fetch_result = fetch_tender_document(session, tender)
                if not fetch_result.get("ok"):
                    job.status = "failed"
                    job.error_message = fetch_result.get("error") or "Unable to fetch tender document."
                    flash(f"Unable to fetch tender document: {job.error_message}", "error")
                    return redirect(url_for("tender_detail", tender_id=tender_id))

                doc = session.execute(
                    select(TenderDocumentCache)
                    .where(
                        TenderDocumentCache.tender_id == tender_id,
                        TenderDocumentCache.document_url == document_url,
                    )
                    .limit(1)
                ).scalars().first()

            extracted_text = (doc.extracted_text or "").strip() if doc else ""
            if not extracted_text:
                job.status = "failed"
                job.error_message = "Tender document was fetched, but no readable text was extracted."
                flash(job.error_message, "error")
                return redirect(url_for("tender_detail", tender_id=tender_id))

            analysis = analyze_tender_against_profile(tender, active_profile, extracted_text) or {}

            job.status = "completed"
            job.score = float(analysis.get("score") or 0)
            job.summary = analysis.get("summary")
            job.strengths_text = "\n".join(analysis.get("strengths") or [])
            job.risks_text = "\n".join(analysis.get("risks") or [])
            job.recommendations_text = analysis.get("recommendation")
            job.raw_result_json = json.dumps(analysis, ensure_ascii=False, default=str)
            job.error_message = None

            doc.parsed_json = json.dumps(analysis, ensure_ascii=False, default=str)
            session.flush()

            flash("Tender analysis completed.", "success")
            return redirect(url_for("tender_detail", tender_id=tender_id))

        except Exception as exc:
            job.status = "failed"
            job.error_message = str(exc)
            session.flush()
            flash(f"Analysis failed: {exc}", "error")
            return redirect(url_for("tender_detail", tender_id=tender_id))


@app.get("/profiles")
def profiles():
    with get_db_session() as session:
        profiles_list = session.execute(
            select(Profile).order_by(desc(Profile.updated_at))
        ).scalars().all()

        return render_template(
            "profiles.html",
            profiles=[serialize_profile(p, include_issues=True) for p in profiles_list],
        )


@app.post("/profiles")
@app.post("/profiles/upload")
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
            parsed_json=json.dumps(parsed, ensure_ascii=False, default=str),
            is_active=True,
        )
        session.add(profile)
        session.flush()

        upsert_profile_issues(session, profile, parsed.get("issues") or [])

    flash(f"Profile uploaded and set as active. Parse mode: {parsed.get('_parse_mode', 'heuristic')}.", "success")
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


@app.get("/api/admin/openai-status")
def admin_openai_status():
    if not admin_allowed():
        return jsonify({"ok": False, "error": "unauthorized"}), 403
    client = get_openai_client()
    return jsonify({
        "ok": True,
        "api_key_present": bool((os.getenv("OPENAI_API_KEY") or "").strip()),
        "client_ready": client is not None,
        "model": OPENAI_MODEL,
    })


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


@app.post("/api/admin/fetch-documents")
@app.get("/api/admin/fetch-documents")
def api_fetch_documents():
    if not admin_allowed():
        return jsonify({"ok": False, "error": "unauthorized"}), 403

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


@app.errorhandler(404)
def not_found(_):
    return render_template("404.html"), 404


@app.errorhandler(500)
def internal_error(_):
    return render_template("500.html"), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
