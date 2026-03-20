import io
import json
import os
import re
import traceback
import uuid
from datetime import datetime, timedelta, timezone
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


APP_VERSION = os.getenv("APP_VERSION", "20260319-beta-12")
ETENDERS_BASE_URL = os.getenv("ETENDERS_BASE_URL", "https://ocds-api.etenders.gov.za")
ETENDERS_RELEASES_PATH = os.getenv("ETENDERS_RELEASES_PATH", "/api/OCDSReleases")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "45"))
MAX_TENDERS = int(os.getenv("MAX_TENDERS", "20"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip() or "sqlite:///tenderai.db"
LOCAL_UPLOAD_DIR = os.getenv("LOCAL_UPLOAD_DIR", "/tmp/uploads")
MAX_CONTENT_LENGTH = 20 * 1024 * 1024

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_PARSER_MODEL = os.getenv("OPENAI_PARSER_MODEL", "gpt-4o-mini").strip()
PARSER_MODE = os.getenv("PARSER_MODE", "auto").strip().lower()

ALLOWED_PROFILE_EXTENSIONS = {"pdf"}

print("=== TenderAI Boot Starting ===")
print(f"APP_VERSION={APP_VERSION}")
print(f"OPENAI_CONFIGURED={bool(OPENAI_API_KEY)}")
print(f"DATABASE_URL={DATABASE_URL}")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
app.secret_key = os.getenv("FLASK_SECRET_KEY", "tenderai-dev-secret")

http = requests.Session()
http.headers.update({"User-Agent": "TenderAI/1.0"})


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
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


engine = None
SessionLocal = None
DB_INIT_ERROR = None


def configure_database() -> None:
    global engine, SessionLocal, DB_INIT_ERROR

    if engine is not None:
        return

    try:
        connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
        local_engine = create_engine(
            DATABASE_URL,
            future=True,
            connect_args=connect_args,
            pool_pre_ping=True,
        )
        local_session = sessionmaker(bind=local_engine, future=True)

        Base.metadata.create_all(local_engine)

        engine = local_engine
        SessionLocal = local_session
        DB_INIT_ERROR = None
        print("Database initialized successfully")
    except Exception as exc:
        DB_INIT_ERROR = str(exc)
        engine = None
        SessionLocal = None
        print(f"Database initialization failed: {DB_INIT_ERROR}")
        traceback.print_exc()


def db_session() -> Session:
    configure_database()
    if SessionLocal is None:
        raise RuntimeError(DB_INIT_ERROR or "Database not initialized")
    return SessionLocal()


configure_database()


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


def first_match(patterns: list[str], text: str, flags: int = re.IGNORECASE) -> Optional[str]:
    for pattern in patterns:
        match = re.search(pattern, text, flags)
        if match:
            for group in match.groups():
                if group:
                    return group.strip()
    return None


def detect_yes_no(text: str) -> Optional[bool]:
    text_lower = text.lower()
    if any(token in text_lower for token in ["yes", "active", "valid", "compliant"]):
        return True
    if any(token in text_lower for token in ["no", "inactive", "invalid", "non-compliant"]):
        return False
    return None


def allowed_profile(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_PROFILE_EXTENSIONS


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
    if response.content_type and "text/html" in response.content_type:
        response.headers["Cache-Control"] = "no-store"
    return response


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


def download_pdf_text_from_url(url: str) -> str:
    if not url:
        return ""

    try:
        response = http.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        content_type = (response.headers.get("Content-Type") or "").lower()

        if "pdf" in content_type or url.lower().endswith(".pdf"):
            return extract_pdf_text_from_bytes(response.content)

        return normalize_whitespace(response.text)
    except Exception:
        traceback.print_exc()
        return ""


def openai_chat_json_schema(
    *,
    system_prompt: str,
    user_prompt: str,
    schema_name: str,
    schema: dict[str, Any],
    temperature: float = 0.1,
) -> Optional[dict[str, Any]]:
    if not OPENAI_API_KEY:
        print("OpenAI key not configured")
        return None

    payload = {
        "model": OPENAI_PARSER_MODEL,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": schema,
            },
        },
    }

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        response = http.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=90,
        )

        if not response.ok:
            print("OpenAI parser request failed")
            print(f"Status: {response.status_code}")
            print(response.text[:1200])
            return None

        data = response.json()
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        print("OpenAI parser exception")
        traceback.print_exc()
        return None


def build_empty_profile_schema() -> dict[str, Any]:
    return {
        "metadata": {
            "source_type": "uploaded_pdf",
            "parser_mode": "heuristic",
            "confidence": 0.45,
            "parsed_at": datetime.now(timezone.utc).isoformat(),
            "parser_version": APP_VERSION,
        },
        "supplier_identification": {
            "supplier_number": None,
            "legal_name": None,
            "trading_name": None,
            "registration_number": None,
            "supplier_type": None,
            "supplier_sub_type": None,
            "registration_date": None,
            "financial_year_start": None,
            "is_active": None,
            "has_bank_account": None,
            "restricted_supplier": None,
            "business_status": None,
            "country_of_origin": None,
            "government_employee": None,
            "allow_associates": None,
            "annual_turnover_band": None,
        },
        "industry_classification": {
            "main_group": [],
            "division": [],
            "core_industry": [],
            "turnover_percentage": [],
        },
        "contact_information": [],
        "address_information": [],
        "bank_information": {
            "verification_status": None,
            "verification_response": None,
        },
        "tax_information": {
            "income_tax_number": None,
            "is_vat_vendor": None,
            "is_registered_with_sars": None,
            "tax_compliance_status": None,
            "compliance_pin_provided": None,
            "last_validation_date": None,
        },
        "bbbbee_information": {
            "certificate_number": None,
            "issue_date": None,
            "expiry_date": None,
            "verification_status": None,
            "black_ownership_percent": None,
            "women_ownership_percent": None,
            "youth_ownership_percent": None,
        },
        "accreditations": [],
        "directors": [],
        "ownership_summary": {
            "black_owned": None,
            "youth_owned": None,
            "township_based": None,
            "rural_based": None,
        },
        "commodities": [],
        "provinces": [],
        "keywords": [],
        "ai_enrichment": {
            "summary": None,
            "capability_keywords": [],
            "compliance_flags": [],
            "risk_notes": [],
        },
    }


def profile_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "metadata": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "source_type": {"type": ["string", "null"]},
                    "parser_mode": {"type": ["string", "null"]},
                    "confidence": {"type": ["number", "null"]},
                },
                "required": ["source_type", "parser_mode", "confidence"],
            },
            "supplier_identification": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "supplier_number": {"type": ["string", "null"]},
                    "legal_name": {"type": ["string", "null"]},
                    "trading_name": {"type": ["string", "null"]},
                    "registration_number": {"type": ["string", "null"]},
                    "supplier_type": {"type": ["string", "null"]},
                    "supplier_sub_type": {"type": ["string", "null"]},
                    "registration_date": {"type": ["string", "null"]},
                    "financial_year_start": {"type": ["string", "null"]},
                    "is_active": {"type": ["boolean", "null"]},
                    "has_bank_account": {"type": ["boolean", "null"]},
                    "restricted_supplier": {"type": ["boolean", "null"]},
                    "business_status": {"type": ["string", "null"]},
                    "country_of_origin": {"type": ["string", "null"]},
                    "government_employee": {"type": ["boolean", "null"]},
                    "allow_associates": {"type": ["boolean", "null"]},
                    "annual_turnover_band": {"type": ["string", "null"]},
                },
                "required": [
                    "supplier_number", "legal_name", "trading_name", "registration_number",
                    "supplier_type", "supplier_sub_type", "registration_date",
                    "financial_year_start", "is_active", "has_bank_account",
                    "restricted_supplier", "business_status", "country_of_origin",
                    "government_employee", "allow_associates", "annual_turnover_band",
                ],
            },
            "industry_classification": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "main_group": {"type": "array", "items": {"type": "string"}},
                    "division": {"type": "array", "items": {"type": "string"}},
                    "core_industry": {"type": "array", "items": {"type": "string"}},
                    "turnover_percentage": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["main_group", "division", "core_industry", "turnover_percentage"],
            },
            "contact_information": {"type": "array", "items": {"type": "object"}},
            "address_information": {"type": "array", "items": {"type": "object"}},
            "bank_information": {"type": "object"},
            "tax_information": {"type": "object"},
            "bbbbee_information": {"type": "object"},
            "accreditations": {"type": "array", "items": {"type": "object"}},
            "directors": {"type": "array", "items": {"type": "object"}},
            "ownership_summary": {"type": "object"},
            "commodities": {"type": "array", "items": {"type": "string"}},
            "provinces": {"type": "array", "items": {"type": "string"}},
            "keywords": {"type": "array", "items": {"type": "string"}},
            "ai_enrichment": {"type": "object"},
        },
        "required": [
            "metadata", "supplier_identification", "industry_classification",
            "contact_information", "address_information", "bank_information",
            "tax_information", "bbbbee_information", "accreditations",
            "directors", "ownership_summary", "commodities", "provinces",
            "keywords", "ai_enrichment",
        ],
    }


def tender_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "scoring_criteria": {"type": "array", "items": {"type": "string"}},
            "mandatory_requirements": {"type": "array", "items": {"type": "string"}},
            "specifications_scope": {"type": "array", "items": {"type": "string"}},
            "special_conditions": {"type": "array", "items": {"type": "string"}},
            "briefing_details": {"type": "array", "items": {"type": "string"}},
            "compliance_cues": {"type": "array", "items": {"type": "string"}},
            "evaluation_cues": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": ["number", "null"]},
        },
        "required": [
            "scoring_criteria", "mandatory_requirements", "specifications_scope",
            "special_conditions", "briefing_details", "compliance_cues",
            "evaluation_cues", "confidence",
        ],
    }


def llm_parse_profile_pdf_text(text: str) -> Optional[dict[str, Any]]:
    doc_text = normalize_whitespace(text)[:24000]

    system_prompt = """
You extract structured supplier profile data from South African business profile or CSD-style documents.
Return only facts supported by the document.
Do not guess.
Use null when unknown.
"""

    user_prompt = f"""
Extract this supplier profile into the required JSON schema.

Document text:
{doc_text}
"""

    parsed = openai_chat_json_schema(
        system_prompt=system_prompt.strip(),
        user_prompt=user_prompt.strip(),
        schema_name="supplier_profile",
        schema=profile_schema(),
        temperature=0.1,
    )

    if parsed:
        parsed.setdefault("metadata", {})
        parsed["metadata"]["source_type"] = "uploaded_pdf"
        parsed["metadata"]["parser_mode"] = "llm"
        parsed["metadata"]["confidence"] = parsed["metadata"].get("confidence", 0.86)

    return parsed


def llm_parse_tender_document_text(text: str) -> Optional[dict[str, Any]]:
    doc_text = normalize_whitespace(text)[:24000]

    system_prompt = """
You extract procurement intelligence from South African tender documents.
Return only facts supported by the document.
Focus on requirements, scope, special conditions, briefing details, compliance obligations,
and evaluation or scoring cues such as functionality, 80/20, 90/10, preference points,
specific goals, B-BBEE, and mandatory submission items.
"""

    user_prompt = f"""
Extract this tender document into the required JSON schema.

Document text:
{doc_text}
"""

    parsed = openai_chat_json_schema(
        system_prompt=system_prompt.strip(),
        user_prompt=user_prompt.strip(),
        schema_name="tender_document",
        schema=tender_schema(),
        temperature=0.1,
    )

    if parsed and parsed.get("confidence") is None:
        parsed["confidence"] = 0.84

    return parsed


def build_profile_summary_text(profile: dict[str, Any]) -> str:
    supplier = profile.get("supplier_identification", {})
    industry = profile.get("industry_classification", {})
    tax_info = profile.get("tax_information", {})
    bbbee = profile.get("bbbbee_information", {})

    bits = []
    if supplier.get("legal_name"):
        bits.append(f"Legal name: {supplier['legal_name']}")
    if supplier.get("supplier_sub_type"):
        bits.append(f"Supplier subtype: {supplier['supplier_sub_type']}")
    if industry.get("main_group"):
        bits.append(f"Main groups: {', '.join(industry['main_group'][:3])}")
    if industry.get("division"):
        bits.append(f"Divisions: {', '.join(industry['division'][:3])}")
    if tax_info.get("tax_compliance_status"):
        bits.append(f"Tax status: {tax_info['tax_compliance_status']}")
    if bbbee.get("verification_status"):
        bits.append(f"B-BBEE: {bbbee['verification_status']}")
    return " | ".join(bits)


def parse_profile_pdf_text_heuristic(text: str) -> dict[str, Any]:
    profile = build_empty_profile_schema()
    text_lower = text.lower()

    supplier = profile["supplier_identification"]
    tax_info = profile["tax_information"]
    bbbee = profile["bbbbee_information"]

    supplier["supplier_number"] = first_match(
        [
            r"supplier\s*(?:number|no\.?)\s*[:\-]\s*([A-Z0-9\-\/]+)",
            r"\bMAAA\s*([0-9]{6,})\b",
        ],
        text,
    )
    supplier["legal_name"] = first_match(
        [
            r"(?:legal\s*name|supplier\s*name|enterprise\s*name)\s*[:\-]\s*(.+)",
            r"registered\s*name\s*[:\-]\s*(.+)",
        ],
        text,
    )
    supplier["trading_name"] = first_match([r"(?:trading\s*name|business\s*name)\s*[:\-]\s*(.+)"], text)
    supplier["registration_number"] = first_match([r"(?:registration\s*(?:number|no\.?))\s*[:\-]\s*([A-Z0-9\/\-]+)"], text)
    supplier["supplier_type"] = first_match([r"supplier\s*type\s*[:\-]\s*(.+)"], text)
    supplier["supplier_sub_type"] = first_match([r"supplier\s*sub[\-\s]*type\s*[:\-]\s*(.+)"], text)
    supplier["country_of_origin"] = first_match([r"country\s*of\s*origin\s*[:\-]\s*(.+)"], text)
    supplier["annual_turnover_band"] = first_match([r"(?:annual\s*turnover|turnover\s*band)\s*[:\-]\s*(.+)"], text)
    supplier["business_status"] = first_match([r"business\s*status\s*[:\-]\s*(.+)"], text)

    active_field = first_match([r"(?:supplier\s*active\s*status|active)\s*[:\-]\s*(.+)"], text)
    supplier["is_active"] = detect_yes_no(active_field or "")

    tax_info["tax_compliance_status"] = first_match([r"tax\s*compliance\s*status\s*[:\-]\s*(.+)"], text)
    tax_info["is_registered_with_sars"] = detect_yes_no(
        first_match([r"(?:registered\s*with\s*SARS)\s*[:\-]\s*(.+)"], text) or ""
    )
    tax_info["is_vat_vendor"] = detect_yes_no(
        first_match([r"(?:VAT\s*vendor|is\s*vat\s*vendor)\s*[:\-]\s*(.+)"], text) or ""
    )

    bbbee["verification_status"] = first_match(
        [r"(?:B[\-\s]*BBEE|BBBEE).{0,30}verification\s*status\s*[:\-]\s*(.+)"],
        text,
    )
    bbbee["certificate_number"] = first_match(
        [r"(?:B[\-\s]*BBEE|BBBEE).{0,40}(?:certificate\s*(?:number|no\.?))\s*[:\-]\s*([A-Z0-9\-\/]+)"],
        text,
    )

    provinces = [
        "Eastern Cape", "Free State", "Gauteng", "KwaZulu-Natal", "Limpopo",
        "Mpumalanga", "Northern Cape", "North West", "Western Cape",
    ]
    profile["provinces"] = [province for province in provinces if province.lower() in text_lower]
    profile["ai_enrichment"]["summary"] = build_profile_summary_text(profile)
    return profile


def parse_profile_pdf_text(text: str) -> dict[str, Any]:
    if PARSER_MODE in {"auto", "llm"} and OPENAI_API_KEY:
        parsed = llm_parse_profile_pdf_text(text)
        if parsed:
            return parsed
    return parse_profile_pdf_text_heuristic(text)


def profile_summary_for_ui(profile_data: dict[str, Any]) -> dict[str, Any]:
    supplier = profile_data.get("supplier_identification", {})
    industry = profile_data.get("industry_classification", {})
    tax_info = profile_data.get("tax_information", {})
    bbbee = profile_data.get("bbbbee_information", {})

    return {
        "company_name": supplier.get("legal_name") or supplier.get("trading_name"),
        "supplier_active_status": supplier.get("is_active"),
        "supplier_sub_type": supplier.get("supplier_sub_type"),
        "country_of_origin": supplier.get("country_of_origin"),
        "government_employee": supplier.get("government_employee"),
        "overall_tax_status": tax_info.get("tax_compliance_status"),
        "sars_registration_status": tax_info.get("is_registered_with_sars"),
        "industry_classification": industry,
        "industry_main_groups": industry.get("main_group", []),
        "industry_divisions": industry.get("division", []),
        "address_information": profile_data.get("address_information", []),
        "bbbee_information": bbbee,
        "bbbee_details": bbbee,
        "ownership_information": profile_data.get("ownership_summary", {}),
        "directors_members_owners": profile_data.get("directors", []),
        "accreditations": profile_data.get("accreditations", []),
        "commodities": profile_data.get("commodities", []),
        "provinces": profile_data.get("provinces", []),
        "keywords": profile_data.get("keywords", []),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }


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


def fetch_tenders(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page_number: int = 1,
    page_size: int = 20,
) -> list[dict[str, Any]]:
    url = urljoin(ETENDERS_BASE_URL, ETENDERS_RELEASES_PATH)

    params = {"pageNumber": page_number, "pageSize": page_size}
    if date_from:
        params["dateFrom"] = date_from
    if date_to:
        params["dateTo"] = date_to

    response = http.get(url, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    return extract_releases(payload)


def document_url(doc: dict[str, Any]) -> Optional[str]:
    for key in ["url", "downloadUrl", "uri", "href"]:
        if doc.get(key):
            return doc[key]
    return None


def pick_best_tender_document(documents: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not documents:
        return None

    scored = []
    for doc in documents:
        title = " ".join(str(doc.get(key, "")) for key in ["title", "description", "documentType"]).lower()
        url = (document_url(doc) or "").lower()

        score = 0
        if "pdf" in url or url.endswith(".pdf"):
            score += 10
        if "terms of reference" in title or "tor" in title:
            score += 10
        if "specification" in title or "scope of work" in title:
            score += 9
        if "tender" in title:
            score += 8
        if "bid" in title:
            score += 7
        if "rfq" in title or "rfp" in title:
            score += 6
        if "evaluation" in title or "pricing schedule" in title:
            score += 5
        if "advert" in title or "invitation" in title or "notice" in title:
            score -= 4

        scored.append((score, doc))

    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def extract_section(text: str, headings: list[str], next_headings: list[str]) -> list[str]:
    if not text:
        return []

    pattern_headings = "|".join(re.escape(item) for item in headings)
    pattern_next = "|".join(re.escape(item) for item in next_headings)

    pattern = (
        rf"(?is)\b(?:{pattern_headings})\b[:\s\-]*"
        rf"(.*?)"
        rf"(?=(?:\n[A-Z][A-Z0-9 /()\-]{{3,}})|(?:\b(?:{pattern_next})\b)|\Z)"
    )
    matches = re.findall(pattern, text)
    results = []

    for match in matches[:3]:
        lines = chunk_lines(match)
        for line in lines[:20]:
            if 4 < len(line) < 300:
                results.append(line)

    return list(dict.fromkeys(results))[:12]


def find_keyword_lines(text: str, keywords: list[str], limit: int = 12) -> list[str]:
    results = []
    for line in chunk_lines(text):
        line_lower = line.lower()
        if any(keyword.lower() in line_lower for keyword in keywords):
            results.append(line)
        if len(results) >= limit:
            break
    return results


def parse_tender_document_text_heuristic(text: str) -> dict[str, Any]:
    return {
        "scoring_criteria": find_keyword_lines(text, ["80/20", "90/10", "functionality", "points"]),
        "mandatory_requirements": find_keyword_lines(text, ["must submit", "mandatory", "required", "csd", "cidb", "tax"]),
        "specifications_scope": extract_section(
            text,
            ["scope of work", "specification", "specifications", "terms of reference", "deliverables"],
            ["special conditions", "evaluation", "briefing", "contact person", "closing date"],
        ),
        "special_conditions": extract_section(
            text,
            ["special conditions", "conditions of tender", "conditions"],
            ["evaluation", "briefing", "scope of work", "specification", "contact person"],
        ),
        "briefing_details": find_keyword_lines(text, ["briefing", "site inspection", "clarification meeting"]),
        "compliance_cues": find_keyword_lines(text, ["tax compliance", "csd", "pin", "sbd", "declaration"]),
        "evaluation_cues": find_keyword_lines(text, ["pppfa", "80/20", "90/10", "specific goals", "functionality"]),
        "confidence": 0.46,
    }


def parse_tender_document_text(text: str) -> dict[str, Any]:
    if not text:
        return {
            "scoring_criteria": [],
            "mandatory_requirements": [],
            "specifications_scope": [],
            "special_conditions": [],
            "briefing_details": [],
            "compliance_cues": [],
            "evaluation_cues": [],
            "confidence": 0.0,
        }

    if PARSER_MODE in {"auto", "llm"} and OPENAI_API_KEY:
        parsed = llm_parse_tender_document_text(text)
        if parsed:
            return parsed

    return parse_tender_document_text_heuristic(text)


def infer_profile_keywords(profile_data: dict[str, Any]) -> set[str]:
    tokens = set()
    for key in ["industry_classification", "keywords", "commodities", "provinces"]:
        value = profile_data.get(key)
        if isinstance(value, dict):
            for nested in value.values():
                if isinstance(nested, list):
                    for item in nested:
                        if item:
                            tokens.update(re.findall(r"[A-Za-z][A-Za-z&/\-]{2,}", str(item).lower()))
        elif isinstance(value, list):
            for item in value:
                if item:
                    tokens.update(re.findall(r"[A-Za-z][A-Za-z&/\-]{2,}", str(item).lower()))
    return tokens


def score_fit(
    tender: dict[str, Any],
    parsed_doc: dict[str, Any],
    profile_data: Optional[dict[str, Any]],
    prompt: str,
) -> dict[str, Any]:
    tender_text_parts = [
        tender.get("title") or "",
        tender.get("description") or "",
        tender.get("eligibility_criteria") or "",
        tender.get("special_conditions") or "",
        " ".join(parsed_doc.get("mandatory_requirements", [])),
        " ".join(parsed_doc.get("specifications_scope", [])),
        " ".join(parsed_doc.get("evaluation_cues", [])),
        " ".join(parsed_doc.get("compliance_cues", [])),
        prompt or "",
    ]
    tender_text = " ".join(tender_text_parts).lower()

    fit_score = 35
    reasons = []
    risks = []
    readiness = []

    if profile_data:
        profile_keywords = infer_profile_keywords(profile_data)
        overlap = sorted({token for token in profile_keywords if len(token) > 3 and token in tender_text})
        fit_score += min(len(overlap) * 4, 24)

        if overlap:
            reasons.append(f"Capability overlap: {', '.join(overlap[:6])}")
        else:
            risks.append("Limited direct capability overlap detected from current profile extraction")

        tax_status = (profile_data.get("tax_information", {}).get("tax_compliance_status") or "").lower()
        if "compliant" in tax_status or "valid" in tax_status:
            fit_score += 8
            readiness.append("Tax compliance signal detected")
        else:
            risks.append("Tax compliance status not clearly confirmed in profile")

        bbbee_status = (
            profile_data.get("bbbbee_information", {}).get("verification_status") or ""
        ).strip()
        if bbbee_status:
            fit_score += 4
            readiness.append("B-BBEE signal detected")

    evaluation_cues = [x.lower() for x in parsed_doc.get("evaluation_cues", [])]
    compliance_cues = [x.lower() for x in parsed_doc.get("compliance_cues", [])]
    briefing_details = parsed_doc.get("briefing_details", [])
    mandatory_requirements = parsed_doc.get("mandatory_requirements", [])

    if any("80/20" in cue for cue in evaluation_cues):
        reasons.append("80/20 preference system detected")
    if any("90/10" in cue for cue in evaluation_cues):
        reasons.append("90/10 preference system detected")
    if any("functionality" in cue for cue in evaluation_cues):
        risks.append("Functionality scoring appears relevant and may require stronger evidence")
    if any("cidb" in cue for cue in compliance_cues + [m.lower() for m in mandatory_requirements]):
        risks.append("CIDB-related compliance may be required")
    if briefing_details:
        risks.append("Tender appears to include briefing or site inspection obligations")

    fit_score = max(5, min(95, fit_score))

    if fit_score >= 75:
        fit_band = "High fit"
        competitiveness = "Potentially strong"
        execution_investment = "Medium"
    elif fit_score >= 55:
        fit_band = "Medium fit"
        competitiveness = "Moderate"
        execution_investment = "Medium"
    else:
        fit_band = "Low fit"
        competitiveness = "Uncertain"
        execution_investment = "High"

    if not readiness:
        readiness.append("Prepare a structured compliance and capability evidence pack")

    return {
        "fit_score": fit_score,
        "fit_band": fit_band,
        "fit_reasons": reasons[:5],
        "risk_flags": risks[:6],
        "competitiveness": competitiveness,
        "execution_investment": execution_investment,
        "strategic_readiness": readiness[:6],
    }


def normalize_tender_release(item: dict[str, Any]) -> dict[str, Any]:
    tender = item.get("tender", {}) or {}
    buyer = item.get("buyer", {}) or {}
    documents = tender.get("documents", [])
    if not isinstance(documents, list):
        documents = []

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
        "description": tender.get("description"),
        "eligibility_criteria": tender.get("eligibilityCriteria"),
        "selection_criteria": tender.get("selectionCriteria"),
        "briefing_session": tender.get("briefingSession"),
        "contact_person": tender.get("contactPerson"),
        "buyer_name": buyer.get("name"),
        "documents": documents,
    }


def enrich_tender(
    item: dict[str, Any],
    profile: Optional[dict[str, Any]] = None,
    prompt: str = "",
) -> dict[str, Any]:
    tender = normalize_tender_release(item)
    best_doc = pick_best_tender_document(tender.get("documents", []))
    best_doc_url = document_url(best_doc or {})
    parsed_text = download_pdf_text_from_url(best_doc_url) if best_doc_url else ""
    parsed_doc = parse_tender_document_text(parsed_text)
    fit = score_fit(tender, parsed_doc, profile, prompt)

    tender["document_url"] = best_doc_url
    tender["document_title"] = (best_doc or {}).get("title")
    tender["parsed_document"] = parsed_doc
    tender["document_parser_confidence"] = parsed_doc.get("confidence")
    tender["analysis"] = fit
    return tender


def get_profile_record(profile_id: str) -> Optional[Profile]:
    with db_session() as session:
        return session.get(Profile, profile_id)


def get_active_profile_record() -> Optional[Profile]:
    with db_session() as session:
        statement = select(Profile).where(Profile.is_active.is_(True)).order_by(Profile.uploaded_at.desc())
        return session.scalar(statement)


def get_profile_data(profile_id: Optional[str]) -> Optional[dict[str, Any]]:
    record = get_profile_record(profile_id) if profile_id else get_active_profile_record()
    if not record:
        return None
    return json_loads_safe(record.parsed_json, {})


@app.get("/")
def home():
    tenders = []
    profiles = []

    try:
        today = datetime.now(timezone.utc).date()
        date_from = (today - timedelta(days=7)).isoformat()
        date_to = (today + timedelta(days=30)).isoformat()
        releases = fetch_tenders(date_from=date_from, date_to=date_to, page_number=1, page_size=10)
        for item in releases[:8]:
            tenders.append(normalize_tender_release(item))
    except Exception:
        traceback.print_exc()

    try:
        with db_session() as session:
            profiles = session.scalars(
                select(Profile).order_by(Profile.is_active.desc(), Profile.uploaded_at.desc())
            ).all()
    except Exception:
        traceback.print_exc()

    return render_template("home.html", tenders=tenders, profiles=profiles)


@app.get("/profiles")
def profiles_page():
    profiles = []
    try:
        with db_session() as session:
            profiles = session.scalars(
                select(Profile).order_by(Profile.is_active.desc(), Profile.uploaded_at.desc())
            ).all()
    except Exception:
        traceback.print_exc()

    parsed_profiles = []
    for profile in profiles:
        parsed_profiles.append(
            {
                "id": profile.id,
                "file_name": profile.file_name,
                "is_active": profile.is_active,
                "uploaded_at": profile.uploaded_at.isoformat(),
                "summary": json_loads_safe(profile.summary_json, {}),
            }
        )

    return render_template("profiles.html", profiles=parsed_profiles)


@app.get("/tenders")
def tenders_page():
    prompt = request.args.get("prompt", "").strip()
    profile_id = request.args.get("profile_id", "").strip() or None

    profile_data = get_profile_data(profile_id)
    if profile_id and not profile_data:
        flash("Selected profile was not found. Please activate or upload a profile again.", "error")
        return redirect(url_for("profiles_page"))

    today = datetime.now(timezone.utc).date()
    date_from = request.args.get("date_from", (today - timedelta(days=7)).isoformat())
    date_to = request.args.get("date_to", (today + timedelta(days=30)).isoformat())

    enriched = []
    error_message = None

    try:
        releases = fetch_tenders(date_from=date_from, date_to=date_to, page_number=1, page_size=MAX_TENDERS)
        for item in releases[:MAX_TENDERS]:
            enriched.append(enrich_tender(item, profile=profile_data, prompt=prompt))
    except Exception as exc:
        traceback.print_exc()
        error_message = str(exc)

    return render_template(
        "feed.html",
        tenders=enriched,
        prompt=prompt,
        profile_id=profile_id,
        error_message=error_message,
    )


@app.get("/tender/<path:tender_id>")
def tender_detail_page(tender_id: str):
    today = datetime.now(timezone.utc).date()
    releases = fetch_tenders(
        date_from=(today - timedelta(days=30)).isoformat(),
        date_to=(today + timedelta(days=60)).isoformat(),
        page_number=1,
        page_size=MAX_TENDERS,
    )

    matched = None
    for item in releases:
        tender = normalize_tender_release(item)
        if str(tender.get("tender_id")) == tender_id or str(tender.get("ocid")) == tender_id:
            matched = item
            break

    if not matched:
        abort(404)

    enriched = enrich_tender(matched)
    return render_template("tender_detail.html", tender=enriched)


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


@app.get("/api/profiles")
def api_profiles_list():
    try:
        with db_session() as session:
            profiles = session.scalars(
                select(Profile).order_by(Profile.is_active.desc(), Profile.uploaded_at.desc())
            ).all()
    except Exception as exc:
        return render_json_response({"error": str(exc)}, 500)

    payload = []
    for profile in profiles:
        payload.append(
            {
                "id": profile.id,
                "file_name": profile.file_name,
                "is_active": profile.is_active,
                "uploaded_at": profile.uploaded_at.isoformat(),
                **json_loads_safe(profile.summary_json, {}),
            }
        )

    return render_json_response(payload)


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
    parsed = parse_profile_pdf_text(profile_text)
    summary = profile_summary_for_ui(parsed)

    try:
        with db_session() as session:
            has_any_profile = session.scalar(select(Profile.id).limit(1)) is not None
            record = Profile(
                id=profile_id,
                file_name=safe_name,
                company_name=summary.get("company_name"),
                profile_text=profile_text,
                parsed_json=json.dumps(parsed),
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
            "is_active": not has_any_profile,
            "parser_mode": parsed.get("metadata", {}).get("parser_mode"),
            "parser_confidence": parsed.get("metadata", {}).get("confidence"),
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
        next_active_id = None
        with db_session() as session:
            target = session.get(Profile, profile_id)
            if not target:
                return render_json_response({"error": "Profile not found"}, 404)

            was_active = target.is_active
            session.delete(target)
            session.commit()

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


@app.get("/api/tenders")
def api_tenders():
    today = datetime.now(timezone.utc).date()
    date_from = request.args.get("date_from", (today - timedelta(days=7)).isoformat())
    date_to = request.args.get("date_to", (today + timedelta(days=30)).isoformat())
    page_number = int(request.args.get("page_number", "1"))
    page_size = int(request.args.get("page_size", "20"))

    releases = fetch_tenders(
        date_from=date_from,
        date_to=date_to,
        page_number=page_number,
        page_size=page_size,
    )
    payload = [normalize_tender_release(item) for item in releases]
    return render_json_response(payload)


@app.get("/api/tender/<path:tender_id>")
def api_tender_detail(tender_id: str):
    today = datetime.now(timezone.utc).date()
    releases = fetch_tenders(
        date_from=(today - timedelta(days=30)).isoformat(),
        date_to=(today + timedelta(days=60)).isoformat(),
        page_number=1,
        page_size=MAX_TENDERS,
    )

    matched = None
    for item in releases:
        tender = normalize_tender_release(item)
        if str(tender.get("tender_id")) == tender_id or str(tender.get("ocid")) == tender_id:
            matched = item
            break

    if not matched:
        return render_json_response({"error": "Tender not found"}, 404)

    return render_json_response(enrich_tender(matched))


@app.post("/api/score")
def api_score():
    payload = request.get_json(silent=True) or {}
    prompt = (payload.get("prompt") or "").strip()
    profile_id = (payload.get("profile_id") or "").strip() or None

    profile_data = get_profile_data(profile_id)
    if profile_id and not profile_data:
        return render_json_response({"error": "Profile not found"}, 404)

    today = datetime.now(timezone.utc).date()
    releases = fetch_tenders(
        date_from=(today - timedelta(days=7)).isoformat(),
        date_to=(today + timedelta(days=30)).isoformat(),
        page_number=1,
        page_size=MAX_TENDERS,
    )

    enriched = [enrich_tender(item, profile=profile_data, prompt=prompt) for item in releases[:MAX_TENDERS]]
    return render_json_response(enriched)


@app.post("/api/advise")
def api_advise():
    payload = request.get_json(silent=True) or {}
    tender = payload.get("tender") or {}
    profile = payload.get("profile") or {}
    parsed_doc = tender.get("parsed_document") or {}

    return render_json_response(
        {
            "summary": "Prioritize compliance completeness, capability proof, and evaluation-fit evidence.",
            "actions": [
                "Validate all mandatory submission items against the tender document.",
                "Prepare a response structure aligned to specifications and scope headings.",
                "Surface tax, CSD, and B-BBEE evidence early in the submission pack.",
                "Address functionality thresholds and scoring cues explicitly where detected.",
                "Confirm briefing attendance requirements and date constraints before bid/no-bid.",
            ],
            "profile_signals": profile.get("ai_enrichment", {}).get("compliance_flags", []),
            "tender_signals": parsed_doc.get("evaluation_cues", []),
        }
    )


@app.post("/api/service-request")
def api_service_request():
    payload = request.get_json(silent=True) or {}
    return render_json_response(
        {
            "status": "received",
            "message": "Service request stub recorded",
            "payload": payload,
        },
        202,
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    print(f"Starting TenderAI on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
