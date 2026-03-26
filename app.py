
import io
import json
import os
import re
import tempfile
from datetime import date, datetime, timezone
from html import unescape
from urllib.parse import urljoin

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        return False

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for
import requests
from sqlalchemy import desc, func, or_, select

try:
    from pypdf import PdfReader
except Exception:
    try:
        from PyPDF2 import PdfReader
    except Exception:
        PdfReader = None

try:
    from docx import Document as DocxDocument
except Exception:
    DocxDocument = None

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

from database import get_db_session, init_db
from models import IngestRun, Profile, ProfileIssue, TenderCache, TenderDocumentCache
from services.etenders_ingest import ingest_tenders

load_dotenv()


def utcnow():
    return datetime.now(timezone.utc)


def safe_json_loads(value, default=None):
    if default is None:
        default = {}
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        text = str(value).strip()
        if not text:
            return default
        return json.loads(text)
    except Exception:
        return default


app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "tenderai-dev-fallback-secret")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

init_db()

HTTP_TIMEOUT = int(os.getenv("TENDER_FETCH_TIMEOUT_SECONDS", os.getenv("ETENDERS_HTTP_TIMEOUT", "35")))
USER_AGENT = os.getenv("TENDERAI_USER_AGENT", "TenderAI/1.0")


def get_openai_client():
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key or OpenAI is None:
        return None
    try:
        return OpenAI(api_key=api_key)
    except Exception:
        return None


def get_active_profile(session):
    return session.execute(
        select(Profile)
        .where(Profile.is_active.is_(True))
        .order_by(desc(Profile.updated_at), desc(Profile.id))
        .limit(1)
    ).scalars().first()


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
        ("ICT", ["software", "ict", "technology", "systems", "digital", "it services", "web development"]),
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
        if any(marker in line.lower() for marker in markers):
            capabilities.extend(normalize_keywords(line))
    if not capabilities:
        capabilities = normalize_keywords(text[:3000])[:20]

    locations = []
    provinces = [
        "Gauteng", "KwaZulu-Natal", "Western Cape", "Eastern Cape", "Free State",
        "Mpumalanga", "Limpopo", "North West", "Northern Cape",
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
        "industry": industry or "",
        "capabilities": capabilities[:20],
        "locations": list(dict.fromkeys(locations)),
        "years_experience": "",
        "accreditations": [],
        "compliance_documents": [],
        "industry_keywords": capabilities[:10],
        "issues": issues,
        "summary": "Heuristic profile parse fallback.",
    }


def parse_supplier_profile_text(text: str, filename: str | None = None) -> dict | None:
    client = get_openai_client()
    if not client or not text.strip():
        return None
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "company_name", "industry", "capabilities", "locations", "years_experience",
            "accreditations", "compliance_documents", "industry_keywords", "issues", "summary"
        ],
        "properties": {
            "company_name": {"type": "string"},
            "industry": {"type": "string"},
            "capabilities": {"type": "array", "items": {"type": "string"}},
            "locations": {"type": "array", "items": {"type": "string"}},
            "years_experience": {"type": "string"},
            "accreditations": {"type": "array", "items": {"type": "string"}},
            "compliance_documents": {"type": "array", "items": {"type": "string"}},
            "industry_keywords": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": "string"},
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["title", "detail", "penalty_weight"],
                    "properties": {
                        "title": {"type": "string"},
                        "detail": {"type": "string"},
                        "penalty_weight": {"type": "number"},
                    },
                },
            },
        },
    }
    try:
        response = client.responses.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            input=[
                {"role": "system", "content": "Extract structured supplier profile data conservatively. Do not invent missing facts."},
                {"role": "user", "content": f"Filename: {filename or 'unknown'}\n\nDocument text:\n{text[:18000]}"},
            ],
            text={"format": {"type": "json_schema", "name": "supplier_profile_parse", "strict": True, "schema": schema}},
        )
        return json.loads(response.output_text)
    except Exception:
        return None


def parse_profile_text(text: str, filename: str | None = None) -> dict:
    parsed = parse_supplier_profile_text(text, filename)
    return parsed or heuristic_profile_parse(text, filename)


def parse_tender_document_text(metadata: dict, text: str) -> dict | None:
    client = get_openai_client()
    if not client or not text.strip():
        return None
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "scope_summary", "deliverables", "eligibility_requirements", "compulsory_documents",
            "evaluation_criteria", "functionality_criteria", "cidb_requirements", "briefing_session",
            "closing_rules", "contract_duration", "location", "industry_tags", "risk_flags", "confidence"
        ],
        "properties": {
            "scope_summary": {"type": "string"},
            "deliverables": {"type": "array", "items": {"type": "string"}},
            "eligibility_requirements": {"type": "array", "items": {"type": "string"}},
            "compulsory_documents": {"type": "array", "items": {"type": "string"}},
            "evaluation_criteria": {"type": "array", "items": {"type": "string"}},
            "functionality_criteria": {"type": "array", "items": {"type": "string"}},
            "cidb_requirements": {"type": "array", "items": {"type": "string"}},
            "briefing_session": {
                "type": "object",
                "additionalProperties": False,
                "required": ["required", "date", "location", "notes"],
                "properties": {
                    "required": {"type": "string"},
                    "date": {"type": "string"},
                    "location": {"type": "string"},
                    "notes": {"type": "string"},
                },
            },
            "closing_rules": {"type": "array", "items": {"type": "string"}},
            "contract_duration": {"type": "string"},
            "location": {"type": "string"},
            "industry_tags": {"type": "array", "items": {"type": "string"}},
            "risk_flags": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        },
    }
    try:
        response = client.responses.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            input=[
                {"role": "system", "content": "Extract procurement intelligence conservatively from the tender document text."},
                {"role": "user", "content": "Tender metadata:\n" + "\n".join(
                    [
                        f"title: {metadata.get('title') or ''}",
                        f"buyer_name: {metadata.get('buyer_name') or ''}",
                        f"province: {metadata.get('province') or ''}",
                        f"closing_date: {metadata.get('closing_date') or ''}",
                        f"source_url: {metadata.get('source_url') or ''}",
                    ]
                ) + "\n\nTender document text:\n" + text[:24000]},
            ],
            text={"format": {"type": "json_schema", "name": "tender_document_parse", "strict": True, "schema": schema}},
        )
        return json.loads(response.output_text)
    except Exception:
        return None


def split_csvish(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value)
    chunks = []
    seen = set()
    for item in text.replace("\n", ",").replace(";", ",").split(","):
        cleaned = " ".join(item.split()).strip()
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            chunks.append(cleaned)
    return chunks


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
        ]
    ).lower()
    profile_industry = getattr(profile, "industry", None)
    tender_industry = getattr(tender, "industry", None)
    if profile_industry and tender_industry:
        if profile_industry.lower() == tender_industry.lower():
            score += 25.0
        elif profile_industry.lower() in tender_blob:
            score += 12.0

    matches = 0
    for capability in split_csvish(getattr(profile, "capabilities_text", "")):
        if capability.lower() in tender_blob:
            matches += 1
    score += min(matches * 6.0, 30.0)

    tender_province = getattr(tender, "province", None)
    profile_locations = split_csvish(getattr(profile, "locations_text", ""))
    if tender_province and any(tender_province.lower() == item.lower() for item in profile_locations):
        score += 8.0

    for issue in getattr(profile, "issues", []) or []:
        status = (getattr(issue, "status", "") or "").lower()
        weight = float(getattr(issue, "penalty_weight", 0) or 0)
        if status == "pending":
            score -= weight
        elif status == "fixed":
            score += min(weight, 2.0)

    closing_date = getattr(tender, "closing_date", None)
    if closing_date:
        try:
            days = (closing_date - date.today()).days
            if days >= 0:
                score += min(days / 2.0, 7.0)
        except Exception:
            pass

    return max(0.0, min(score, 100.0))


def serialize_profile_issue(issue: ProfileIssue) -> dict:
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


def serialize_profile(profile: Profile, include_issues: bool = False) -> dict:
    data = {
        "id": profile.id,
        "name": profile.name,
        "company_name": profile.company_name,
        "original_filename": profile.original_filename,
        "industry": profile.industry,
        "capabilities_text": profile.capabilities_text,
        "locations_text": profile.locations_text,
        "extracted_text": profile.extracted_text,
        "parsed_json": safe_json_loads(profile.parsed_json, {}),
        "is_active": profile.is_active,
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
    }
    if include_issues:
        data["issues"] = [serialize_profile_issue(i) for i in getattr(profile, "issues", []) or []]
    return data


def serialize_tender_document(doc: TenderDocumentCache) -> dict:
    return {
        "id": doc.id,
        "tender_id": doc.tender_id,
        "document_url": doc.document_url,
        "filename": doc.filename,
        "content_type": doc.content_type,
        "fetch_status": doc.fetch_status,
        "error_message": doc.error_message,
        "fetched_at": doc.fetched_at,
        "extracted_text": doc.extracted_text,
        "parsed_json": safe_json_loads(doc.parsed_json, {}),
        "updated_at": doc.updated_at,
    }


def tender_to_view_model(t: TenderCache) -> dict:
    doc = None
    try:
        docs = getattr(t, "documents", None) or []
        if docs:
            doc = max(docs, key=lambda x: x.updated_at or x.created_at or utcnow())
    except Exception:
        doc = None
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
        "source_url": t.source_url,
        "document_url": t.document_url,
        "updated_at": t.updated_at,
        "is_live": t.is_live,
        "parsed_document": serialize_tender_document(doc) if doc else None,
        "analysis": None,
    }


def serialize_ingest_run(run: IngestRun | None):
    if not run:
        return None
    return {
        "id": run.id,
        "status": run.status,
        "pages_attempted": run.pages_attempted,
        "pages_succeeded": run.pages_succeeded,
        "tenders_seen": run.tenders_seen,
        "tenders_upserted": run.tenders_upserted,
        "failure_message": run.failure_message,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


def refresh_profile_issues(session, profile_id: int, issues: list[dict]):
    existing = session.execute(
        select(ProfileIssue).where(ProfileIssue.profile_id == profile_id)
    ).scalars().all()
    for row in existing:
        session.delete(row)
    session.flush()
    for issue in issues or []:
        session.add(
            ProfileIssue(
                profile_id=profile_id,
                issue_type="profile_gap",
                title=(issue.get("title") or "Profile issue")[:255],
                detail=issue.get("detail"),
                penalty_weight=float(issue.get("penalty_weight") or 5),
                status="pending",
            )
        )


def _extract_links_from_html(base_url: str, html: str) -> list[str]:
    links = []
    for href in re.findall(r'href=["\']([^"\']+)["\']', html, flags=re.IGNORECASE):
        href = href.strip()
        if href:
            links.append(urljoin(base_url, href))
    return list(dict.fromkeys(links))


def _extract_html_text(html: str) -> str:
    clean = re.sub(r"<script.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    clean = re.sub(r"<style.*?</style>", " ", clean, flags=re.IGNORECASE | re.DOTALL)
    clean = re.sub(r"<[^>]+>", " ", clean)
    return " ".join(unescape(clean).split())


def _extract_pdf_bytes_text(content: bytes) -> str:
    if PdfReader is None:
        return ""
    try:
        reader = PdfReader(io.BytesIO(content))
        pages = []
        for page in getattr(reader, "pages", []):
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(pages).strip()
    except Exception:
        return ""


def _extract_docx_bytes_text(content: bytes) -> str:
    if DocxDocument is None:
        return ""
    try:
        doc = DocxDocument(io.BytesIO(content))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip()).strip()
    except Exception:
        return ""


def fetch_and_store_tender_document(session, tender: TenderCache) -> dict:
    candidate_url = (tender.document_url or tender.source_url or "").strip()
    if not candidate_url:
        existing = session.execute(
            select(TenderDocumentCache).where(TenderDocumentCache.tender_id == tender.id).order_by(desc(TenderDocumentCache.updated_at))
        ).scalars().first()
        if existing:
            existing.fetch_status = "failed"
            existing.error_message = "No document or source URL on tender."
        else:
            session.add(TenderDocumentCache(
                tender_id=tender.id,
                document_url="",
                fetch_status="failed",
                error_message="No document or source URL on tender.",
            ))
        return {"ok": False, "fetch_error": "No document or source URL on tender."}

    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.get(candidate_url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:
        row = session.execute(
            select(TenderDocumentCache).where(TenderDocumentCache.tender_id == tender.id, TenderDocumentCache.document_url == candidate_url)
        ).scalars().first()
        if not row:
            row = TenderDocumentCache(tender_id=tender.id, document_url=candidate_url)
            session.add(row)
        row.fetch_status = "failed"
        row.error_message = str(exc)
        return {"ok": False, "fetch_error": str(exc)}

    resolved_url = resp.url
    content_type = (resp.headers.get("content-type") or "").lower()
    body = resp.content

    # If HTML, try to find a linked PDF/DOCX first.
    if ("text/html" in content_type or "<html" in (resp.text[:300] if hasattr(resp, "text") else "").lower()) and body:
        links = _extract_links_from_html(resolved_url, resp.text)
        preferred = [u for u in links if u.lower().endswith(".pdf") or ".pdf?" in u.lower()]
        preferred += [u for u in links if u.lower().endswith(".docx") or ".docx?" in u.lower()]
        if preferred:
            try:
                resp = requests.get(preferred[0], headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True)
                resp.raise_for_status()
                resolved_url = resp.url
                content_type = (resp.headers.get("content-type") or "").lower()
                body = resp.content
            except Exception:
                pass

    text = ""
    if "application/pdf" in content_type or resolved_url.lower().endswith(".pdf"):
        text = _extract_pdf_bytes_text(body)
    elif "wordprocessingml.document" in content_type or resolved_url.lower().endswith(".docx"):
        text = _extract_docx_bytes_text(body)
    elif "text/html" in content_type:
        text = _extract_html_text(resp.text)

    filename = os.path.basename(resolved_url.split("?", 1)[0])[:255] if resolved_url else None

    row = session.execute(
        select(TenderDocumentCache)
        .where(TenderDocumentCache.tender_id == tender.id, TenderDocumentCache.document_url == resolved_url)
    ).scalars().first()
    if not row:
        row = TenderDocumentCache(tender_id=tender.id, document_url=resolved_url)
        session.add(row)

    row.filename = filename
    row.content_type = content_type[:100] if content_type else None
    row.extracted_text = text[:800000] if text else None
    row.fetch_status = "fetched"
    row.error_message = None
    row.binary_content = body[:5_000_000] if body else None
    row.fetched_at = utcnow()
    tender.document_url = resolved_url or tender.document_url
    return {"ok": True, "document_url": resolved_url, "filename": filename, "content_type": content_type, "text_len": len(text or "")}


@app.context_processor
def inject_globals():
    with get_db_session() as session:
        active_profile = get_active_profile(session)
        latest_ingest = session.execute(
            select(IngestRun).order_by(desc(IngestRun.started_at), desc(IngestRun.id)).limit(1)
        ).scalars().first()
        return {
            "active_profile": serialize_profile(active_profile) if active_profile else None,
            "latest_ingest": serialize_ingest_run(latest_ingest),
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

        view_ranked = [{"tender": tender_to_view_model(t), "score": keyword_overlap_score(active_profile, t)} for t in tenders]
        if active_profile:
            view_ranked.sort(key=lambda x: (x["score"] or 0), reverse=True)
        return render_template("home.html", total_live=total_live, featured=view_ranked[:12])


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

        ranked = [{"tender": tender_to_view_model(t), "score": keyword_overlap_score(active_profile, t)} for t in items]
        if active_profile:
            ranked.sort(key=lambda x: (x["score"] or 0), reverse=True)

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
            filters={"province": province, "tender_type": tender_type, "industry": industry, "issued_from": issued_from, "q": search_text},
        )


@app.get("/tender/<int:tender_id>")
def tender_detail(tender_id: int):
    with get_db_session() as session:
        tender = session.get(TenderCache, tender_id)
        if not tender:
            abort(404)
        active_profile = get_active_profile(session)
        score = keyword_overlap_score(active_profile, tender)
        return render_template("tender_detail.html", tender=tender_to_view_model(tender), alignment_score=score)


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

    parsed = parse_profile_text(text, uploaded.filename)
    with get_db_session() as session:
        session.execute(Profile.__table__.update().values(is_active=False))
        profile = Profile(
            name=(parsed.get("company_name") or os.path.splitext(uploaded.filename)[0])[:255],
            company_name=(parsed.get("company_name") or "")[:255] or None,
            original_filename=uploaded.filename,
            industry=(parsed.get("industry") or "")[:255] or None,
            capabilities_text=", ".join(parsed.get("capabilities") or []),
            locations_text=", ".join(parsed.get("locations") or []),
            extracted_text=text[:200000],
            parsed_json=json.dumps(parsed, ensure_ascii=False),
            is_active=True,
        )
        session.add(profile)
        session.flush()
        refresh_profile_issues(session, profile.id, parsed.get("issues") or [])
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
        result = ingest_tenders(
            session=session,
            max_pages=int(os.getenv("INGEST_MAX_PAGES", "1")),
            page_size=int(os.getenv("ETENDERS_PAGE_SIZE", "25")),
        )
        return jsonify(result)


@app.route("/api/admin/fetch-documents", methods=["GET", "POST"])
def api_fetch_documents():
    if not admin_allowed():
        return jsonify({"ok": False, "error": "unauthorized"}), 403
    limit = int(request.args.get("limit") or request.form.get("limit") or os.getenv("DOCUMENT_FETCH_LIMIT", "20"))
    fetched = 0
    errors = []
    with get_db_session() as session:
        tenders = session.execute(
            select(TenderCache)
            .where(TenderCache.is_live.is_(True))
            .order_by(TenderCache.closing_date.asc().nulls_last(), desc(TenderCache.updated_at))
            .limit(limit)
        ).scalars().all()
        for tender in tenders:
            try:
                result = fetch_and_store_tender_document(session, tender)
                if result.get("ok"):
                    fetched += 1
                else:
                    errors.append({"tender_id": tender.id, "error": result.get("fetch_error")})
            except Exception as exc:
                errors.append({"tender_id": tender.id, "error": str(exc)})
        return jsonify({"ok": True, "requested": len(tenders), "fetched": fetched, "errors": errors})


@app.route("/api/admin/parse-documents", methods=["GET", "POST"])
def api_parse_documents():
    if not admin_allowed():
        return jsonify({"ok": False, "error": "unauthorized"}), 403
    limit = int(request.args.get("limit") or request.form.get("limit") or os.getenv("DOCUMENT_PARSE_LIMIT", "20"))
    parsed_count = 0
    skipped = 0
    errors = []
    with get_db_session() as session:
        docs = session.execute(
            select(TenderDocumentCache)
            .join(TenderCache, TenderDocumentCache.tender_id == TenderCache.id)
            .where(TenderCache.is_live.is_(True))
            .order_by(TenderCache.closing_date.asc().nulls_last(), desc(TenderDocumentCache.updated_at))
            .limit(limit)
        ).scalars().all()
        for doc in docs:
            try:
                if not (doc.extracted_text or "").strip():
                    skipped += 1
                    continue
                tender = session.get(TenderCache, doc.tender_id)
                if not tender:
                    skipped += 1
                    continue
                parsed = parse_tender_document_text(
                    {
                        "title": tender.title,
                        "buyer_name": tender.buyer_name,
                        "province": tender.province,
                        "closing_date": str(tender.closing_date) if tender.closing_date else "",
                        "source_url": tender.source_url,
                    },
                    doc.extracted_text,
                )
                if not parsed:
                    skipped += 1
                    continue
                doc.parsed_json = json.dumps(parsed, ensure_ascii=False)
                doc.fetch_status = "parsed"
                doc.error_message = None
                parsed_count += 1
            except Exception as exc:
                doc.error_message = str(exc)
                errors.append({"tender_id": doc.tender_id, "error": str(exc)})
        return jsonify({"ok": True, "parsed": parsed_count, "skipped": skipped, "errors": errors})


@app.route("/api/admin/reparse-profile/<int:profile_id>", methods=["GET", "POST"])
def api_reparse_profile(profile_id: int):
    if not admin_allowed():
        return jsonify({"ok": False, "error": "unauthorized"}), 403
    with get_db_session() as session:
        profile = session.get(Profile, profile_id)
        if not profile:
            return jsonify({"ok": False, "error": "profile_not_found"}), 404
        text = getattr(profile, "extracted_text", None) or ""
        if not text.strip():
            return jsonify({"ok": False, "error": "profile_has_no_extracted_text"}), 400
        parsed = parse_profile_text(text, getattr(profile, "original_filename", None))
        profile.company_name = (parsed.get("company_name") or profile.company_name or profile.name or "")[:255] or None
        profile.industry = (parsed.get("industry") or profile.industry or "")[:255] or None
        profile.capabilities_text = ", ".join(parsed.get("capabilities") or [])
        profile.locations_text = ", ".join(parsed.get("locations") or [])
        profile.parsed_json = json.dumps(parsed, ensure_ascii=False)
        refresh_profile_issues(session, profile.id, parsed.get("issues") or [])
        return jsonify({"ok": True, "profile": serialize_profile(profile, include_issues=True)})


@app.errorhandler(404)
def not_found(_):
    return render_template("404.html"), 404


@app.errorhandler(500)
def internal_error(_):
    return render_template("500.html"), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
