import io
import json
import os
import re
import traceback
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urljoin

import requests
from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from pypdf import PdfReader
from sqlalchemy import Boolean, DateTime, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from werkzeug.utils import secure_filename

APP_VERSION = os.getenv("APP_VERSION", "20260325-beta-stable-1")
ETENDERS_BASE_URL = os.getenv("ETENDERS_BASE_URL", "https://ocds-api.etenders.gov.za")
ETENDERS_RELEASES_PATH = os.getenv("ETENDERS_RELEASES_PATH", "/api/OCDSReleases")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "45"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "12"))
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "100"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip() or "sqlite:///tenderai.db"
LOCAL_UPLOAD_DIR = os.getenv("LOCAL_UPLOAD_DIR", "/tmp/uploads")
MAX_CONTENT_LENGTH = 20 * 1024 * 1024

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
PARSER_MODE = os.getenv("PARSER_MODE", "auto").strip().lower()
OPENAI_PARSER_MODEL = os.getenv("OPENAI_PARSER_MODEL", "gpt-4o-mini").strip()

ALLOWED_PROFILE_EXTENSIONS = {"pdf"}

http = requests.Session()
http.headers.update({"User-Agent": "TenderAI/1.0"})

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
app.secret_key = os.getenv("FLASK_SECRET_KEY", "tenderai-dev-secret")


class Base(DeclarativeBase):
    pass


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    file_name: Mapped[str] = mapped_column(String(255))
    company_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    profile_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    parsed_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    summary_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


engine = None
SessionLocal = None


def configure_database() -> None:
    global engine, SessionLocal
    if engine is not None:
        return
    connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
    engine = create_engine(DATABASE_URL, future=True, connect_args=connect_args, pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine, future=True)
    Base.metadata.create_all(engine)


def db_session() -> Session:
    configure_database()
    return SessionLocal()


configure_database()


def render_json_response(payload: Any, status: int = 200):
    response = make_response(jsonify(payload), status)
    response.headers["X-App-Version"] = APP_VERSION
    response.headers["Cache-Control"] = "no-store"
    return response


@app.context_processor
def inject_globals():
    return {"app_version": APP_VERSION}


@app.after_request
def add_headers(response):
    response.headers["X-App-Version"] = APP_VERSION
    return response


def ensure_local_upload_dir() -> None:
    os.makedirs(LOCAL_UPLOAD_DIR, exist_ok=True)


def json_loads_safe(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def normalize_whitespace(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_lines(text: str) -> list[str]:
    return [line.strip() for line in normalize_whitespace(text).splitlines() if line.strip()]


def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        clean = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def format_date(value: Optional[str]) -> Optional[str]:
    dt = parse_iso_datetime(value)
    if not dt:
        return None
    return dt.strftime("%d %b %Y")


def compute_days_left(value: Optional[str]) -> Optional[int]:
    dt = parse_iso_datetime(value)
    if not dt:
        return None
    return (dt.date() - datetime.now(timezone.utc).date()).days


def sanitize_document_url(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    lowered = text.lower()
    if lowered in {"url not found", "not found", "none", "null", "nan", "-"}:
        return None
    if not lowered.startswith("http"):
        return None
    return text


def allowed_profile(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_PROFILE_EXTENSIONS


def extract_pdf_text_from_bytes(pdf_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = []
        for page in reader.pages:
            text = page.extract_text() or ""
            if text:
                pages.append(text)
        return normalize_whitespace("\n\n".join(pages))
    except Exception:
        traceback.print_exc()
        return ""


def profile_summary_from_text(text: str) -> dict[str, Any]:
    text_lower = text.lower()

    sectors = [s for s in [
        "construction", "engineering", "consulting", "it", "logistics",
        "security", "cleaning", "catering", "training", "electrical", "civil"
    ] if s in text_lower]

    capabilities = []
    for line in chunk_lines(text):
        ll = line.lower()
        if any(k in ll for k in ["services", "supply", "maintenance", "installation", "consulting", "construction"]):
            capabilities.append(line)
        if len(capabilities) >= 8:
            break

    company_name = None
    patterns = [
        r"(?:legal\s*name|supplier\s*name|enterprise\s*name)\s*[:\-]\s*(.+)",
        r"registered\s*name\s*[:\-]\s*(.+)",
        r"trading\s*name\s*[:\-]\s*(.+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match and match.group(1):
            company_name = match.group(1).strip()
            break

    return {
        "company_name": company_name,
        "summary_text": " | ".join(filter(None, [
            company_name,
            ", ".join(sectors[:3]) if sectors else None,
            ", ".join(capabilities[:2]) if capabilities else None,
        ])) or "Profile processed and stored.",
        "industry_main_groups": sectors,
        "industry_divisions": sectors,
        "keywords": capabilities,
        "accreditations": [],
        "commodities": sectors,
        "provinces": [],
    }


def get_profile_record(profile_id: str) -> Optional[Profile]:
    with db_session() as session:
        return session.get(Profile, profile_id)


def get_active_profile_record() -> Optional[Profile]:
    with db_session() as session:
        return session.scalar(select(Profile).where(Profile.is_active.is_(True)).order_by(Profile.uploaded_at.desc()))


def get_profile_data(profile_id: Optional[str]) -> Optional[dict[str, Any]]:
    record = get_profile_record(profile_id) if profile_id else get_active_profile_record()
    if not record:
        return None
    return json_loads_safe(record.parsed_json, {})


def get_profile_summary(profile_id: Optional[str]) -> Optional[dict[str, Any]]:
    record = get_profile_record(profile_id) if profile_id else get_active_profile_record()
    if not record:
        return None
    summary = json_loads_safe(record.summary_json, {})
    summary.update(
        {
            "id": record.id,
            "file_name": record.file_name,
            "company_name": record.company_name,
            "is_active": record.is_active,
            "uploaded_at": record.uploaded_at.isoformat(),
        }
    )
    return summary


def extract_releases(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        releases = payload.get("releases")
        if isinstance(releases, list):
            return releases
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("releases"), list):
            return data["releases"]
        if isinstance(payload.get("value"), list):
            return payload["value"]
    if isinstance(payload, list):
        return payload
    return []


def is_live_tender_release(item: dict[str, Any]) -> bool:
    tender = item.get("tender") or {}
    title = (tender.get("title") or "").strip().lower()
    description = (tender.get("description") or "").strip().lower()
    if not tender or not title:
        return False
    exclude_terms = [
        "award notice",
        "awarded bid",
        "contract award",
        "notice of award",
        "appointment of service provider",
        "successful bidder",
    ]
    return not any(term in title or term in description for term in exclude_terms)


def fetch_tender_page(page_number: int, page_size: int = PAGE_SIZE) -> list[dict[str, Any]]:
    url = urljoin(ETENDERS_BASE_URL, ETENDERS_RELEASES_PATH)
    response = http.get(url, params={"pageNumber": page_number, "pageSize": page_size}, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return extract_releases(response.json())


def fetch_all_current_tenders(max_pages: int = MAX_PAGES, page_size: int = PAGE_SIZE) -> list[dict[str, Any]]:
    seen = set()
    all_items: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        items = fetch_tender_page(page, page_size=page_size)
        if not items:
            break
        for item in items:
            if not is_live_tender_release(item):
                continue
            key = str(item.get("id") or item.get("ocid") or uuid.uuid4())
            if key in seen:
                continue
            seen.add(key)
            all_items.append(item)
    return all_items


def normalize_tender_release(item: dict[str, Any]) -> dict[str, Any]:
    tender = item.get("tender", {}) or {}
    buyer = item.get("buyer", {}) or {}
    documents = tender.get("documents", [])
    if not isinstance(documents, list):
        documents = []

    issue_date = item.get("date") or tender.get("datePublished") or tender.get("publishedDate")
    closing_date = tender.get("closingDate") or ((tender.get("tenderPeriod") or {}).get("endDate") if isinstance(tender.get("tenderPeriod"), dict) else None)
    category = tender.get("mainProcurementCategory") or tender.get("procurementMethodDetails") or tender.get("procurementMethod")

    return {
        "ocid": item.get("ocid"),
        "release_id": item.get("id"),
        "tender_id": tender.get("id"),
        "title": tender.get("title"),
        "status": tender.get("status"),
        "province": tender.get("province"),
        "delivery_location": tender.get("deliveryLocation"),
        "special_conditions": tender.get("specialConditions"),
        "main_procurement_category": tender.get("mainProcurementCategory"),
        "tender_type": category,
        "description": tender.get("description"),
        "eligibility_criteria": tender.get("eligibilityCriteria"),
        "selection_criteria": tender.get("selectionCriteria"),
        "briefing_session": tender.get("briefingSession"),
        "contact_person": tender.get("contactPerson"),
        "buyer_name": buyer.get("name"),
        "documents": documents,
        "issue_date": issue_date,
        "issue_date_display": format_date(issue_date),
        "closing_date": closing_date,
        "closing_date_display": format_date(closing_date),
        "days_left": compute_days_left(closing_date),
    }


def all_current_tender_records() -> list[dict[str, Any]]:
    return [normalize_tender_release(item) for item in fetch_all_current_tenders()]


def find_tender_item(identifier: str) -> Optional[dict[str, Any]]:
    for item in fetch_all_current_tenders():
        tender = normalize_tender_release(item)
        if identifier in {str(tender.get("release_id")), str(tender.get("tender_id")), str(tender.get("ocid"))}:
            return item
    return None


def prefit_score_from_profile(tender: dict[str, Any], profile_data: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not profile_data:
        return {"score": None, "band": "Browse", "reasons": []}

    profile_terms = set()
    for key in ["industry_main_groups", "industry_divisions", "keywords", "commodities"]:
        for item in profile_data.get(key, []):
            for token in re.findall(r"[A-Za-z][A-Za-z&/\-]{2,}", str(item).lower()):
                profile_terms.add(token)

    tender_text = " ".join([
        str(tender.get("title") or ""),
        str(tender.get("description") or ""),
        str(tender.get("main_procurement_category") or ""),
        str(tender.get("tender_type") or ""),
    ]).lower()

    overlap = sorted({token for token in profile_terms if len(token) > 3 and token in tender_text})
    score = 20 if not overlap else min(25 + len(overlap) * 9, 95)

    if score >= 70:
        band = "High alignment"
    elif score >= 45:
        band = "Medium alignment"
    else:
        band = "Low alignment"

    return {"score": score, "band": band, "reasons": overlap[:6]}


def compute_insights(tenders: list[dict[str, Any]]) -> dict[str, Any]:
    province_counts = Counter((t.get("province") or "Unknown") for t in tenders)
    type_counts = Counter((t.get("tender_type") or t.get("main_procurement_category") or "Unknown") for t in tenders)
    urgent = sum(1 for t in tenders if t.get("days_left") is not None and 0 <= t["days_left"] <= 7)
    return {
        "live_count": len(tenders),
        "urgent_count": urgent,
        "top_provinces": province_counts.most_common(4),
        "top_types": type_counts.most_common(4),
    }


def filter_tenders(tenders: list[dict[str, Any]], province: str = "", tender_type: str = "", industry: str = "", date_from: str = "") -> list[dict[str, Any]]:
    province = province.strip().lower()
    tender_type = tender_type.strip().lower()
    industry = industry.strip().lower()
    date_from_dt = parse_iso_datetime(date_from) if date_from else None

    filtered = []
    for tender in tenders:
        if province and province not in (tender.get("province") or "").lower():
            continue

        if tender_type:
            type_text = " ".join([str(tender.get("tender_type") or ""), str(tender.get("main_procurement_category") or "")]).lower()
            if tender_type not in type_text:
                continue

        if industry:
            industry_text = " ".join([
                str(tender.get("title") or ""),
                str(tender.get("description") or ""),
                str(tender.get("main_procurement_category") or ""),
                str(tender.get("tender_type") or ""),
            ]).lower()
            if industry not in industry_text:
                continue

        issue_dt = parse_iso_datetime(tender.get("issue_date"))
        if date_from_dt and issue_dt and issue_dt.date() < date_from_dt.date():
            continue

        filtered.append(tender)

    return filtered


@app.get("/")
def home():
    tenders = []
    active_profile = get_profile_summary(None)
    profiles = []

    try:
        with db_session() as session:
            records = session.scalars(select(Profile).order_by(Profile.is_active.desc(), Profile.uploaded_at.desc())).all()
            for profile in records:
                summary = json_loads_safe(profile.summary_json, {})
                summary.update(
                    {
                        "id": profile.id,
                        "file_name": profile.file_name,
                        "company_name": profile.company_name,
                        "is_active": profile.is_active,
                    }
                )
                profiles.append(summary)
    except Exception:
        traceback.print_exc()

    try:
        tenders = all_current_tender_records()[:8]
    except Exception:
        traceback.print_exc()

    return render_template("home.html", tenders=tenders, active_profile=active_profile, profiles=profiles)


@app.get("/profiles")
def profiles_page():
    profiles = []
    try:
        with db_session() as session:
            records = session.scalars(select(Profile).order_by(Profile.is_active.desc(), Profile.uploaded_at.desc())).all()
        for profile in records:
            summary = json_loads_safe(profile.summary_json, {})
            summary.update(
                {
                    "id": profile.id,
                    "file_name": profile.file_name,
                    "company_name": profile.company_name,
                    "is_active": profile.is_active,
                    "uploaded_at": profile.uploaded_at.isoformat(),
                }
            )
            profiles.append(summary)
    except Exception:
        traceback.print_exc()

    active_profile = next((p for p in profiles if p.get("is_active")), None)
    return render_template("profiles.html", profiles=profiles, active_profile=active_profile)


@app.get("/tenders")
def tenders_page():
    prompt = request.args.get("prompt", "").strip()
    profile_id = request.args.get("profile_id", "").strip() or None
    province = request.args.get("province", "").strip()
    tender_type = request.args.get("tender_type", "").strip()
    industry = request.args.get("industry", "").strip()
    date_from = request.args.get("date_from", "").strip()

    active_profile = get_profile_summary(profile_id)
    profile_data = get_profile_summary(profile_id) if profile_id else get_profile_summary(None)
    analysis_enabled = bool(active_profile)

    if profile_id and not active_profile:
        flash("Selected profile was not found. Please activate or upload a profile again.", "error")
        return redirect(url_for("profiles_page"))

    try:
        tenders = all_current_tender_records()
        for tender in tenders:
            tender["prefit"] = prefit_score_from_profile(tender, profile_data)
        tenders = filter_tenders(tenders, province=province, tender_type=tender_type, industry=industry, date_from=date_from)
        if profile_data:
            tenders.sort(key=lambda x: (x["prefit"]["score"] is None, -(x["prefit"]["score"] or 0), x.get("days_left") if x.get("days_left") is not None else 99999))
        else:
            tenders.sort(key=lambda x: (x.get("days_left") is None, x.get("days_left", 99999)))
        insights = compute_insights(tenders)
        error_message = None
    except Exception as exc:
        traceback.print_exc()
        tenders = []
        insights = {"live_count": 0, "urgent_count": 0, "top_provinces": [], "top_types": []}
        error_message = str(exc)

    return render_template(
        "feed.html",
        tenders=tenders,
        prompt=prompt,
        profile_id=profile_id,
        active_profile=active_profile,
        error_message=error_message,
        analysis_enabled=analysis_enabled,
        insights=insights,
        filters={"province": province, "tender_type": tender_type, "industry": industry, "date_from": date_from},
    )


@app.get("/tender/<path:tender_id>")
def tender_detail_page(tender_id: str):
    profile_id = request.args.get("profile_id", "").strip() or None
    active_profile = get_profile_summary(profile_id)

    matched = find_tender_item(tender_id)
    if not matched:
        abort(404)

    tender = normalize_tender_release(matched)

    best_doc_url = None
    if tender.get("documents"):
        for doc in tender["documents"]:
            best_doc_url = sanitize_document_url(doc.get("url")) or sanitize_document_url(doc.get("downloadUrl")) or sanitize_document_url(doc.get("uri")) or sanitize_document_url(doc.get("href"))
            if best_doc_url:
                break
    tender["document_url"] = best_doc_url

    return render_template(
        "tender_detail.html",
        tender=tender,
        analysis_enabled=bool(active_profile),
        active_profile=active_profile,
        profile_id=profile_id,
    )


@app.get("/health")
def health():
    return render_json_response(
        {
            "status": "ok",
            "app_version": APP_VERSION,
            "openai_configured": bool(OPENAI_API_KEY),
            "parser_mode": PARSER_MODE,
            "database_configured": bool(DATABASE_URL),
            "time": datetime.now(timezone.utc).isoformat(),
        }
    )


@app.post("/api/profiles")
def api_profiles_upload():
    if "file" not in request.files:
        return render_json_response({"error": "No file uploaded"}, 400)

    file = request.files["file"]
    if not file or not file.filename:
        return render_json_response({"error": "No file selected"}, 400)

    if not allowed_profile(file.filename):
        return render_json_response({"error": "Only PDF profile uploads are supported"}, 400)

    ensure_local_upload_dir()
    profile_id = str(uuid.uuid4())
    safe_name = secure_filename(file.filename)
    local_path = os.path.join(LOCAL_UPLOAD_DIR, f"{profile_id}__{safe_name}")
    file.save(local_path)

    with open(local_path, "rb") as handle:
        pdf_bytes = handle.read()

    profile_text = extract_pdf_text_from_bytes(pdf_bytes)
    summary = profile_summary_from_text(profile_text)

    try:
        with db_session() as session:
            has_any_profile = session.scalar(select(Profile.id).limit(1)) is not None
            record = Profile(
                id=profile_id,
                file_name=safe_name,
                company_name=summary.get("company_name"),
                profile_text=profile_text,
                parsed_json=json.dumps(summary),
                summary_json=json.dumps(summary),
                is_active=not has_any_profile,
            )
            session.add(record)
            session.commit()
    except Exception as exc:
        traceback.print_exc()
        return render_json_response({"error": str(exc)}, 500)

    return render_json_response(
        {
            "id": profile_id,
            "file_name": safe_name,
            "company_name": summary.get("company_name"),
            "summary_text": summary.get("summary_text"),
            "is_active": not has_any_profile,
            **summary,
        },
        201,
    )


@app.post("/api/profiles/<profile_id>/activate")
def api_profiles_activate(profile_id: str):
    try:
        with db_session() as session:
            target = session.get(Profile, profile_id)
            if not target:
                return render_json_response({"error": "Profile not found"}, 404)
            all_profiles = session.scalars(select(Profile)).all()
            for profile in all_profiles:
                profile.is_active = profile.id == profile_id
            session.commit()
    except Exception as exc:
        traceback.print_exc()
        return render_json_response({"error": str(exc)}, 500)

    return render_json_response({"status": "ok", "active_profile_id": profile_id})


@app.delete("/api/profiles/<profile_id>")
def api_profiles_delete(profile_id: str):
    try:
        with db_session() as session:
            target = session.get(Profile, profile_id)
            if not target:
                return render_json_response({"error": "Profile not found"}, 404)

            was_active = target.is_active
            session.delete(target)
            session.commit()

            next_active_id = None
            if was_active:
                next_profile = session.scalar(select(Profile).order_by(Profile.uploaded_at.desc()))
                if next_profile:
                    next_profile.is_active = True
                    session.commit()
                    next_active_id = next_profile.id
    except Exception as exc:
        traceback.print_exc()
        return render_json_response({"error": str(exc)}, 500)

    return render_json_response({"status": "deleted", "id": profile_id, "active_profile_id": next_active_id})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
