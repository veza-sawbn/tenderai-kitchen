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


APP_VERSION = os.getenv("APP_VERSION", "20260320-beta-15")
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
print(f"DATABASE_URL_CONFIGURED={bool(DATABASE_URL)}")

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
    if any(token in text_lower for token in ["yes", "active", "valid", "compliant", "verified", "true"]):
        return True
    if any(token in text_lower for token in ["no", "inactive", "invalid", "non-compliant", "false"]):
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


def openai_responses_json_schema(
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

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": OPENAI_PARSER_MODEL,
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": system_prompt}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": user_prompt}],
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "schema": schema,
                "strict": True,
            }
        },
        "temperature": temperature,
        "store": False,
    }

    try:
        response = http.post(
            "https://api.openai.com/v1/responses",
            headers=headers,
            json=payload,
            timeout=120,
        )
        response.raise_for_status()
        data = response.json()

        text_output = None
        for item in data.get("output", []):
            if item.get("type") == "message":
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        text_output = content.get("text")
                        break
            if text_output:
                break

        if not text_output:
            print("No structured output text returned from Responses API")
            return None

        parsed = json.loads(text_output)
        if not isinstance(parsed, dict):
            return None
        return parsed
    except Exception:
        print("OpenAI Responses API exception")
        traceback.print_exc()
        return None


def supplier_extraction_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "document_type": {"type": "string", "enum": ["supplier_profile"]},
            "supplier_identity": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "legal_name": {"type": ["string", "null"]},
                    "trading_name": {"type": ["string", "null"]},
                    "registration_number": {"type": ["string", "null"]},
                    "vat_number": {"type": ["string", "null"]},
                    "csd_number": {"type": ["string", "null"]},
                    "entity_type": {"type": ["string", "null"]},
                    "country": {"type": ["string", "null"]},
                    "province": {"type": ["string", "null"]},
                },
                "required": [
                    "legal_name",
                    "trading_name",
                    "registration_number",
                    "vat_number",
                    "csd_number",
                    "entity_type",
                    "country",
                    "province",
                ],
            },
            "core_capabilities": {"type": "array", "items": {"type": "string"}},
            "services_offered": {"type": "array", "items": {"type": "string"}},
            "sector_tags": {"type": "array", "items": {"type": "string"}},
            "commodity_tags": {"type": "array", "items": {"type": "string"}},
            "geographic_coverage": {"type": "array", "items": {"type": "string"}},
            "compliance_signals": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "tax_compliance_present": {"type": ["boolean", "null"]},
                    "bbbee_status_present": {"type": ["boolean", "null"]},
                    "bbbee_level": {"type": ["string", "null"]},
                    "cidb_grade": {"type": ["string", "null"]},
                    "sars_pin_present": {"type": ["boolean", "null"]},
                    "bank_verification_present": {"type": ["boolean", "null"]},
                    "company_registration_present": {"type": ["boolean", "null"]},
                },
                "required": [
                    "tax_compliance_present",
                    "bbbee_status_present",
                    "bbbee_level",
                    "cidb_grade",
                    "sars_pin_present",
                    "bank_verification_present",
                    "company_registration_present",
                ],
            },
            "certifications_and_accreditations": {"type": "array", "items": {"type": "string"}},
            "past_performance_evidence": {"type": "array", "items": {"type": "string"}},
            "capacity_signals": {"type": "array", "items": {"type": "string"}},
            "key_contacts": {"type": "array", "items": {"type": "string"}},
            "strength_summary": {"type": "array", "items": {"type": "string"}},
            "missing_or_unclear_evidence": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number"},
        },
        "required": [
            "document_type",
            "supplier_identity",
            "core_capabilities",
            "services_offered",
            "sector_tags",
            "commodity_tags",
            "geographic_coverage",
            "compliance_signals",
            "certifications_and_accreditations",
            "past_performance_evidence",
            "capacity_signals",
            "key_contacts",
            "strength_summary",
            "missing_or_unclear_evidence",
            "confidence",
        ],
    }


def tender_extraction_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "document_type": {"type": "string", "enum": ["tender_document"]},
            "tender_identity": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "tender_number": {"type": ["string", "null"]},
                    "title": {"type": ["string", "null"]},
                    "buyer_name": {"type": ["string", "null"]},
                    "buyer_type": {"type": ["string", "null"]},
                    "province": {"type": ["string", "null"]},
                },
                "required": [
                    "tender_number",
                    "title",
                    "buyer_name",
                    "buyer_type",
                    "province",
                ],
            },
            "scope_summary": {"type": ["string", "null"]},
            "deliverables": {"type": "array", "items": {"type": "string"}},
            "required_capabilities": {"type": "array", "items": {"type": "string"}},
            "mandatory_documents": {"type": "array", "items": {"type": "string"}},
            "compliance_requirements": {"type": "array", "items": {"type": "string"}},
            "functionality_criteria": {"type": "array", "items": {"type": "string"}},
            "evaluation_criteria": {"type": "array", "items": {"type": "string"}},
            "price_preference_system": {
                "type": ["string", "null"],
                "enum": ["80/20", "90/10", "other", None],
            },
            "specific_goals_or_preference_cues": {"type": "array", "items": {"type": "string"}},
            "briefing": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "briefing_required": {"type": ["boolean", "null"]},
                    "briefing_compulsory": {"type": ["boolean", "null"]},
                    "briefing_date_text": {"type": ["string", "null"]},
                },
                "required": [
                    "briefing_required",
                    "briefing_compulsory",
                    "briefing_date_text",
                ],
            },
            "submission": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "deadline_text": {"type": ["string", "null"]},
                    "validity_period_text": {"type": ["string", "null"]},
                },
                "required": ["deadline_text", "validity_period_text"],
            },
            "special_conditions": {"type": "array", "items": {"type": "string"}},
            "risk_flags": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number"},
        },
        "required": [
            "document_type",
            "tender_identity",
            "scope_summary",
            "deliverables",
            "required_capabilities",
            "mandatory_documents",
            "compliance_requirements",
            "functionality_criteria",
            "evaluation_criteria",
            "price_preference_system",
            "specific_goals_or_preference_cues",
            "briefing",
            "submission",
            "special_conditions",
            "risk_flags",
            "confidence",
        ],
    }


def bid_assessment_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "decision_summary": {"type": "string"},
            "qualification_status": {
                "type": "string",
                "enum": ["likely_qualifies", "partially_qualifies", "unlikely_to_qualify"],
            },
            "fit_score": {"type": "integer", "minimum": 0, "maximum": 100},
            "win_probability_band": {"type": "string", "enum": ["low", "moderate", "strong"]},
            "bid_recommendation": {"type": "string", "enum": ["go", "go_with_caution", "no_go"]},
            "capability_strengths": {"type": "array", "items": {"type": "string"}},
            "compliance_strengths": {"type": "array", "items": {"type": "string"}},
            "gaps_or_disqualifiers": {"type": "array", "items": {"type": "string"}},
            "competitiveness_assessment": {"type": "string"},
            "execution_burden": {"type": "string", "enum": ["low", "medium", "high"]},
            "strategic_readiness": {"type": "array", "items": {"type": "string"}},
            "improvement_actions": {"type": "array", "items": {"type": "string"}},
            "critical_unknowns": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number"},
        },
        "required": [
            "decision_summary",
            "qualification_status",
            "fit_score",
            "win_probability_band",
            "bid_recommendation",
            "capability_strengths",
            "compliance_strengths",
            "gaps_or_disqualifiers",
            "competitiveness_assessment",
            "execution_burden",
            "strategic_readiness",
            "improvement_actions",
            "critical_unknowns",
            "confidence",
        ],
    }


def llm_extract_supplier_profile(text: str) -> Optional[dict[str, Any]]:
    system_prompt = """
You are TenderAI's supplier-profile interpreter for South African procurement.
Extract only facts supported by the supplier profile.
Do not guess.
Use null or empty arrays when evidence is missing.
Focus on capabilities, compliance cues, accreditations, geographic fit, and past performance signals.
Return only valid JSON matching the schema.
""".strip()

    user_prompt = f"""
Interpret this supplier profile for procurement intelligence.

Context:
- country: South Africa
- objective: determine whether this supplier can qualify and compete for tenders

Supplier profile text:
{normalize_whitespace(text)[:30000]}
""".strip()

    return openai_responses_json_schema(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema_name="supplier_profile_extraction",
        schema=supplier_extraction_schema(),
        temperature=0.1,
    )


def llm_extract_tender_document(text: str) -> Optional[dict[str, Any]]:
    system_prompt = """
You are TenderAI's tender-document interpreter for South African procurement.
Extract only facts supported by the tender document.
Do not guess.
Use null or empty arrays when evidence is missing.
Focus on scope, required capabilities, mandatory documents, compliance obligations, functionality criteria, preference point cues, briefing obligations, and special conditions.
Return only valid JSON matching the schema.
""".strip()

    user_prompt = f"""
Interpret this tender document for procurement intelligence.

Context:
- country: South Africa
- objective: determine qualification requirements, evaluation criteria, compliance obligations, and bid risks

Tender document text:
{normalize_whitespace(text)[:30000]}
""".strip()

    return openai_responses_json_schema(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema_name="tender_document_extraction",
        schema=tender_extraction_schema(),
        temperature=0.1,
    )


def llm_assess_bid(supplier_obj: dict[str, Any], tender_obj: dict[str, Any]) -> Optional[dict[str, Any]]:
    system_prompt = """
You are TenderAI's bid assessment engine for South African procurement.
Compare the supplier profile against the tender requirements.
Do not invent strengths or qualifications that are not supported by the extracted objects.
Be conservative when evidence is incomplete.
Return only valid JSON matching the schema.
""".strip()

    user_prompt = f"""
Assess whether this supplier is a strong candidate for this tender.

Supplier object:
{json.dumps(supplier_obj, ensure_ascii=False)}

Tender object:
{json.dumps(tender_obj, ensure_ascii=False)}

Required output:
- determine likely qualification status
- assign fit score
- estimate win probability band
- recommend go, go_with_caution, or no_go
- explain key strengths, gaps, and improvements
""".strip()

    return openai_responses_json_schema(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema_name="bid_assessment",
        schema=bid_assessment_schema(),
        temperature=0.1,
    )


def build_empty_profile_schema() -> dict[str, Any]:
    return {
        "document_type": "supplier_profile",
        "supplier_identity": {
            "legal_name": None,
            "trading_name": None,
            "registration_number": None,
            "vat_number": None,
            "csd_number": None,
            "entity_type": None,
            "country": "South Africa",
            "province": None,
        },
        "core_capabilities": [],
        "services_offered": [],
        "sector_tags": [],
        "commodity_tags": [],
        "geographic_coverage": [],
        "compliance_signals": {
            "tax_compliance_present": None,
            "bbbee_status_present": None,
            "bbbee_level": None,
            "cidb_grade": None,
            "sars_pin_present": None,
            "bank_verification_present": None,
            "company_registration_present": None,
        },
        "certifications_and_accreditations": [],
        "past_performance_evidence": [],
        "capacity_signals": [],
        "key_contacts": [],
        "strength_summary": [],
        "missing_or_unclear_evidence": [],
        "confidence": 0.35,
    }


def build_profile_summary_text(profile: dict[str, Any]) -> str:
    identity = profile.get("supplier_identity", {})
    bits = []

    if identity.get("legal_name"):
        bits.append(f"Legal name: {identity['legal_name']}")
    if identity.get("entity_type"):
        bits.append(f"Entity type: {identity['entity_type']}")
    if profile.get("core_capabilities"):
        bits.append(f"Capabilities: {', '.join(profile['core_capabilities'][:4])}")
    compliance = profile.get("compliance_signals", {})
    if compliance.get("bbbee_level"):
        bits.append(f"B-BBEE level: {compliance['bbbee_level']}")
    if compliance.get("cidb_grade"):
        bits.append(f"CIDB: {compliance['cidb_grade']}")
    if compliance.get("tax_compliance_present") is True:
        bits.append("Tax compliance signal present")
    return " | ".join(bits)


def parse_profile_pdf_text_heuristic(text: str) -> dict[str, Any]:
    profile = build_empty_profile_schema()
    text_lower = text.lower()

    identity = profile["supplier_identity"]
    compliance = profile["compliance_signals"]

    identity["legal_name"] = first_match(
        [
            r"(?:legal\s*name|supplier\s*name|enterprise\s*name)\s*[:\-]\s*(.+)",
            r"registered\s*name\s*[:\-]\s*(.+)",
        ],
        text,
    )
    identity["trading_name"] = first_match([r"(?:trading\s*name|business\s*name)\s*[:\-]\s*(.+)"], text)
    identity["registration_number"] = first_match(
        [r"(?:registration\s*(?:number|no\.?))\s*[:\-]\s*([A-Z0-9\/\-]+)"],
        text,
    )
    identity["vat_number"] = first_match([r"(?:vat\s*(?:number|no\.?))\s*[:\-]\s*([A-Z0-9\/\-]+)"], text)
    identity["csd_number"] = first_match([r"(?:csd\s*(?:number|no\.?))\s*[:\-]?\s*([A-Z0-9\/\-]+)"], text)
    identity["entity_type"] = first_match([r"(?:supplier\s*type|entity\s*type)\s*[:\-]\s*(.+)"], text)
    identity["country"] = first_match([r"country\s*of\s*origin\s*[:\-]\s*(.+)"], text) or "South Africa"

    provinces = [
        "Eastern Cape", "Free State", "Gauteng", "KwaZulu-Natal", "Limpopo",
        "Mpumalanga", "Northern Cape", "North West", "Western Cape",
    ]
    found_provinces = [province for province in provinces if province.lower() in text_lower]
    identity["province"] = found_provinces[0] if found_provinces else None
    profile["geographic_coverage"] = found_provinces

    profile["core_capabilities"] = find_keyword_lines(
        text,
        ["services", "supply", "maintenance", "construction", "consulting", "training", "installation"],
        limit=8,
    )
    profile["services_offered"] = profile["core_capabilities"][:]
    profile["sector_tags"] = [token for token in ["construction", "engineering", "it", "consulting", "logistics", "cleaning", "training"] if token in text_lower]
    profile["commodity_tags"] = [token for token in ["equipment", "software", "transport", "furniture", "security", "catering"] if token in text_lower]

    compliance["tax_compliance_present"] = True if "tax compliance" in text_lower or "sars" in text_lower else None
    compliance["bbbee_status_present"] = True if "bbbee" in text_lower or "b-bbee" in text_lower else None
    compliance["bbbee_level"] = first_match([r"(?:b[\-\s]*bbbee|bbbbee).{0,30}(?:level|status\s*level)\s*[:\-]?\s*(.+)"], text)
    compliance["cidb_grade"] = first_match([r"cidb.{0,20}(?:grade|grading)\s*[:\-]?\s*([A-Z0-9]+)"], text)
    compliance["sars_pin_present"] = True if "pin" in text_lower and "sars" in text_lower else None
    compliance["bank_verification_present"] = True if "bank verification" in text_lower else None
    compliance["company_registration_present"] = True if bool(identity["registration_number"]) else None

    profile["certifications_and_accreditations"] = find_keyword_lines(text, ["iso", "certified", "accreditation", "registered with"], limit=6)
    profile["past_performance_evidence"] = find_keyword_lines(text, ["project", "client", "contract", "delivered", "experience"], limit=6)
    profile["capacity_signals"] = find_keyword_lines(text, ["team", "staff", "employees", "fleet", "equipment", "capacity"], limit=6)
    profile["key_contacts"] = find_keyword_lines(text, ["email", "tel", "phone", "contact"], limit=5)

    if profile["core_capabilities"]:
        profile["strength_summary"].append("Core business capabilities detected in profile")
    if compliance["tax_compliance_present"]:
        profile["strength_summary"].append("Tax compliance signal detected")
    if compliance["bbbee_status_present"]:
        profile["strength_summary"].append("B-BBEE signal detected")
    if not compliance["company_registration_present"]:
        profile["missing_or_unclear_evidence"].append("Company registration not clearly detected")
    if not compliance["tax_compliance_present"]:
        profile["missing_or_unclear_evidence"].append("Tax compliance evidence not clearly detected")

    profile["confidence"] = 0.42
    return profile


def normalize_supplier_profile(profile: dict[str, Any]) -> dict[str, Any]:
    normalized = build_empty_profile_schema()
    normalized.update({k: v for k, v in profile.items() if k in normalized})

    normalized["document_type"] = "supplier_profile"
    normalized.setdefault("supplier_identity", {})
    normalized.setdefault("compliance_signals", {})

    for key, value in build_empty_profile_schema()["supplier_identity"].items():
        normalized["supplier_identity"].setdefault(key, value)
    for key, value in build_empty_profile_schema()["compliance_signals"].items():
        normalized["compliance_signals"].setdefault(key, value)

    for key in [
        "core_capabilities",
        "services_offered",
        "sector_tags",
        "commodity_tags",
        "geographic_coverage",
        "certifications_and_accreditations",
        "past_performance_evidence",
        "capacity_signals",
        "key_contacts",
        "strength_summary",
        "missing_or_unclear_evidence",
    ]:
        value = normalized.get(key)
        if not isinstance(value, list):
            normalized[key] = []

    if not isinstance(normalized.get("confidence"), (int, float)):
        normalized["confidence"] = 0.5

    return normalized


def parse_profile_pdf_text(text: str) -> dict[str, Any]:
    if PARSER_MODE in {"auto", "llm"} and OPENAI_API_KEY:
        parsed = llm_extract_supplier_profile(text)
        if parsed:
            return normalize_supplier_profile(parsed)
    return parse_profile_pdf_text_heuristic(text)


def profile_summary_for_ui(profile_data: dict[str, Any]) -> dict[str, Any]:
    identity = profile_data.get("supplier_identity", {})
    compliance = profile_data.get("compliance_signals", {})
    return {
        "company_name": identity.get("legal_name") or identity.get("trading_name"),
        "summary_text": build_profile_summary_text(profile_data) or "Profile processed and stored.",
        "supplier_active_status": None,
        "supplier_sub_type": identity.get("entity_type"),
        "country_of_origin": identity.get("country"),
        "government_employee": None,
        "overall_tax_status": "Detected" if compliance.get("tax_compliance_present") else "Unclear",
        "sars_registration_status": compliance.get("sars_pin_present"),
        "industry_classification": {
            "main_group": profile_data.get("sector_tags", []),
            "division": profile_data.get("commodity_tags", []),
        },
        "industry_main_groups": profile_data.get("sector_tags", []),
        "industry_divisions": profile_data.get("commodity_tags", []),
        "address_information": profile_data.get("geographic_coverage", []),
        "bbbee_information": {
            "verification_status": compliance.get("bbbee_status_present"),
            "level": compliance.get("bbbee_level"),
        },
        "bbbee_details": {
            "verification_status": compliance.get("bbbee_status_present"),
            "level": compliance.get("bbbee_level"),
        },
        "ownership_information": {},
        "directors_members_owners": [],
        "accreditations": profile_data.get("certifications_and_accreditations", []),
        "commodities": profile_data.get("commodity_tags", []),
        "provinces": profile_data.get("geographic_coverage", []),
        "keywords": profile_data.get("core_capabilities", []),
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
    if not text:
        return {
            "document_type": "tender_document",
            "tender_identity": {
                "tender_number": None,
                "title": None,
                "buyer_name": None,
                "buyer_type": None,
                "province": None,
            },
            "scope_summary": None,
            "deliverables": [],
            "required_capabilities": [],
            "mandatory_documents": [],
            "compliance_requirements": [],
            "functionality_criteria": [],
            "evaluation_criteria": [],
            "price_preference_system": None,
            "specific_goals_or_preference_cues": [],
            "briefing": {
                "briefing_required": None,
                "briefing_compulsory": None,
                "briefing_date_text": None,
            },
            "submission": {
                "deadline_text": None,
                "validity_period_text": None,
            },
            "special_conditions": [],
            "risk_flags": [],
            "confidence": 0.0,
        }

    scoring = find_keyword_lines(text, ["80/20", "90/10", "functionality", "points", "specific goals"], limit=6)
    mandatory = find_keyword_lines(text, ["must submit", "mandatory", "required", "csd", "cidb", "tax", "sbd"], limit=8)
    briefing = find_keyword_lines(text, ["briefing", "site inspection", "clarification meeting", "compulsory briefing"], limit=5)
    compliance = find_keyword_lines(text, ["tax compliance", "csd", "pin", "declaration", "cidb", "bbbee"], limit=8)
    evaluation = find_keyword_lines(text, ["pppfa", "80/20", "90/10", "specific goals", "functionality", "evaluation"], limit=8)

    scope = extract_section(
        text,
        ["scope of work", "specification", "specifications", "terms of reference", "deliverables", "project scope"],
        ["special conditions", "evaluation", "briefing", "contact person", "closing date", "mandatory requirements"],
    )[:8]

    special = extract_section(
        text,
        ["special conditions", "conditions of tender", "conditions", "special condition"],
        ["evaluation", "briefing", "scope of work", "specification", "contact person", "closing date"],
    )[:6]

    return {
        "document_type": "tender_document",
        "tender_identity": {
            "tender_number": None,
            "title": None,
            "buyer_name": None,
            "buyer_type": None,
            "province": None,
        },
        "scope_summary": " ".join(scope[:3]) if scope else None,
        "deliverables": scope,
        "required_capabilities": scope,
        "mandatory_documents": mandatory,
        "compliance_requirements": compliance,
        "functionality_criteria": scoring,
        "evaluation_criteria": evaluation,
        "price_preference_system": "80/20" if any("80/20" in x for x in evaluation + scoring) else ("90/10" if any("90/10" in x for x in evaluation + scoring) else None),
        "specific_goals_or_preference_cues": evaluation,
        "briefing": {
            "briefing_required": bool(briefing),
            "briefing_compulsory": any("compulsory" in x.lower() for x in briefing),
            "briefing_date_text": briefing[0] if briefing else None,
        },
        "submission": {
            "deadline_text": None,
            "validity_period_text": None,
        },
        "special_conditions": special,
        "risk_flags": [],
        "confidence": 0.46,
    }


def normalize_tender_extraction(parsed: dict[str, Any], tender: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    normalized = parse_tender_document_text_heuristic("")
    normalized.update({k: v for k, v in parsed.items() if k in normalized})

    if tender:
        normalized["tender_identity"]["tender_number"] = normalized["tender_identity"].get("tender_number") or tender.get("tender_id") or tender.get("ocid")
        normalized["tender_identity"]["title"] = normalized["tender_identity"].get("title") or tender.get("title")
        normalized["tender_identity"]["buyer_name"] = normalized["tender_identity"].get("buyer_name") or tender.get("buyer_name")
        normalized["tender_identity"]["province"] = normalized["tender_identity"].get("province") or tender.get("province")

    for key in [
        "deliverables",
        "required_capabilities",
        "mandatory_documents",
        "compliance_requirements",
        "functionality_criteria",
        "evaluation_criteria",
        "specific_goals_or_preference_cues",
        "special_conditions",
        "risk_flags",
    ]:
        if not isinstance(normalized.get(key), list):
            normalized[key] = []

    if not isinstance(normalized.get("briefing"), dict):
        normalized["briefing"] = {
            "briefing_required": None,
            "briefing_compulsory": None,
            "briefing_date_text": None,
        }
    if not isinstance(normalized.get("submission"), dict):
        normalized["submission"] = {
            "deadline_text": None,
            "validity_period_text": None,
        }

    if not isinstance(normalized.get("confidence"), (int, float)):
        normalized["confidence"] = 0.5

    normalized["scoring_criteria"] = list(dict.fromkeys(normalized["functionality_criteria"] + normalized["evaluation_criteria"]))[:10]
    normalized["mandatory_requirements"] = normalized["mandatory_documents"][:]
    normalized["specifications_scope"] = normalized["deliverables"][:]
    normalized["briefing_details"] = [normalized["briefing"].get("briefing_date_text")] if normalized["briefing"].get("briefing_date_text") else []
    normalized["compliance_cues"] = normalized["compliance_requirements"][:]
    normalized["evaluation_cues"] = normalized["evaluation_criteria"][:]

    return normalized


def parse_tender_document_text(text: str, tender: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    if PARSER_MODE in {"auto", "llm"} and OPENAI_API_KEY and text:
        parsed = llm_extract_tender_document(text)
        if parsed:
            return normalize_tender_extraction(parsed, tender=tender)
    return normalize_tender_extraction(parse_tender_document_text_heuristic(text), tender=tender)


def infer_profile_keywords(profile_data: dict[str, Any]) -> set[str]:
    tokens = set()
    for key in [
        "core_capabilities",
        "services_offered",
        "sector_tags",
        "commodity_tags",
        "geographic_coverage",
        "past_performance_evidence",
        "capacity_signals",
    ]:
        value = profile_data.get(key)
        if isinstance(value, list):
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
    if not profile_data:
        return {
            "fit_score": None,
            "fit_band": "Profile required",
            "fit_reasons": [],
            "risk_flags": ["No active supplier profile loaded, so no supplier-fit analysis was performed."],
            "competitiveness": "Not assessed",
            "execution_investment": "Not assessed",
            "strategic_readiness": ["Upload or activate a supplier profile to enable TenderAI analysis."],
            "analysis_ready": False,
            "decision_summary": "No supplier profile available for assessment.",
            "bid_recommendation": "no_go",
            "win_probability_band": "low",
            "improvement_actions": [],
            "critical_unknowns": [],
            "qualification_status": "unlikely_to_qualify",
            "confidence": 0.0,
        }

    assessment = None
    if PARSER_MODE in {"auto", "llm"} and OPENAI_API_KEY:
        assessment = llm_assess_bid(profile_data, parsed_doc)

    if assessment:
        fit_score = assessment.get("fit_score")
        if fit_score is None:
            fit_band = "Profile assessed"
        elif fit_score >= 75:
            fit_band = "High fit"
        elif fit_score >= 55:
            fit_band = "Medium fit"
        else:
            fit_band = "Low fit"

        return {
            "fit_score": fit_score,
            "fit_band": fit_band,
            "fit_reasons": assessment.get("capability_strengths", []),
            "risk_flags": assessment.get("gaps_or_disqualifiers", []),
            "competitiveness": assessment.get("competitiveness_assessment", "Not assessed"),
            "execution_investment": assessment.get("execution_burden", "Not assessed"),
            "strategic_readiness": assessment.get("strategic_readiness", []),
            "analysis_ready": True,
            "decision_summary": assessment.get("decision_summary"),
            "bid_recommendation": assessment.get("bid_recommendation"),
            "win_probability_band": assessment.get("win_probability_band"),
            "improvement_actions": assessment.get("improvement_actions", []),
            "critical_unknowns": assessment.get("critical_unknowns", []),
            "qualification_status": assessment.get("qualification_status"),
            "confidence": assessment.get("confidence"),
        }

    tender_text_parts = [
        tender.get("title") or "",
        tender.get("description") or "",
        " ".join(parsed_doc.get("mandatory_documents", [])),
        " ".join(parsed_doc.get("deliverables", [])),
        " ".join(parsed_doc.get("evaluation_criteria", [])),
        prompt or "",
    ]
    tender_text = " ".join(tender_text_parts).lower()

    fit_score = 20
    reasons = []
    risks = []
    readiness = []

    profile_keywords = infer_profile_keywords(profile_data)
    overlap = sorted({token for token in profile_keywords if len(token) > 3 and token in tender_text})
    fit_score += min(len(overlap) * 5, 30)

    if overlap:
        reasons.append(f"Capability overlap: {', '.join(overlap[:6])}")
    else:
        risks.append("Limited direct capability overlap detected from current profile extraction")

    compliance = profile_data.get("compliance_signals", {})
    if compliance.get("tax_compliance_present"):
        fit_score += 8
        readiness.append("Tax compliance signal detected")
    else:
        risks.append("Tax compliance status not clearly confirmed in profile")

    if compliance.get("bbbee_status_present"):
        fit_score += 4
        readiness.append("B-BBEE signal detected")

    if parsed_doc.get("briefing", {}).get("briefing_required"):
        risks.append("Tender appears to include briefing or site inspection obligations")

    if any("cidb" in item.lower() for item in parsed_doc.get("mandatory_documents", []) + parsed_doc.get("compliance_requirements", [])):
        risks.append("CIDB-related compliance may be required")

    fit_score = max(5, min(95, fit_score))

    if fit_score >= 75:
        fit_band = "High fit"
        competitiveness = "Potentially strong"
        execution_investment = "Medium"
        bid_recommendation = "go"
        win_probability_band = "strong"
        qualification_status = "likely_qualifies"
    elif fit_score >= 55:
        fit_band = "Medium fit"
        competitiveness = "Moderate"
        execution_investment = "Medium"
        bid_recommendation = "go_with_caution"
        win_probability_band = "moderate"
        qualification_status = "partially_qualifies"
    else:
        fit_band = "Low fit"
        competitiveness = "Uncertain"
        execution_investment = "High"
        bid_recommendation = "no_go"
        win_probability_band = "low"
        qualification_status = "unlikely_to_qualify"

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
        "analysis_ready": False,
        "decision_summary": "Fallback assessment used because structured bid assessment was unavailable.",
        "bid_recommendation": bid_recommendation,
        "win_probability_band": win_probability_band,
        "improvement_actions": ["Verify mandatory documents", "Strengthen compliance evidence", "Tailor capability proof to tender scope"],
        "critical_unknowns": [],
        "qualification_status": qualification_status,
        "confidence": 0.35,
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

    parsed_text = ""
    parsed_doc = normalize_tender_extraction(parse_tender_document_text_heuristic(""), tender=tender)

    if best_doc_url:
        parsed_text = download_pdf_text_from_url(best_doc_url)
        if parsed_text:
            parsed_doc = parse_tender_document_text(parsed_text, tender=tender)

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
        summary = json_loads_safe(profile.summary_json, {})
        parsed_profiles.append(
            {
                "id": profile.id,
                "file_name": profile.file_name,
                "company_name": profile.company_name,
                "is_active": profile.is_active,
                "uploaded_at": profile.uploaded_at.isoformat(),
                **summary,
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
    analysis_enabled = bool(profile_data)

    try:
        releases = fetch_tenders(date_from=date_from, date_to=date_to, page_number=1, page_size=MAX_TENDERS)
        for item in releases[:MAX_TENDERS]:
            enriched.append(enrich_tender(item, profile=profile_data if analysis_enabled else None, prompt=prompt))
    except Exception as exc:
        traceback.print_exc()
        error_message = str(exc)

    return render_template(
        "feed.html",
        tenders=enriched,
        prompt=prompt,
        profile_id=profile_id,
        error_message=error_message,
        analysis_enabled=analysis_enabled,
    )


@app.get("/tender/<path:tender_id>")
def tender_detail_page(tender_id: str):
    profile_id = request.args.get("profile_id", "").strip() or None
    profile_data = get_profile_data(profile_id)

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

    enriched = enrich_tender(matched, profile=profile_data)
    return render_template(
        "tender_detail.html",
        tender=enriched,
        analysis_enabled=bool(profile_data),
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


@app.get("/debug/static-check")
def debug_static_check():
    return render_template("base.html", title="Static Check")


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
                "company_name": profile.company_name,
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
            "company_name": summary.get("company_name"),
            "summary_text": summary.get("summary_text"),
            "is_active": not has_any_profile,
            "parser_mode": "llm" if OPENAI_API_KEY and PARSER_MODE in {"auto", "llm"} else "heuristic",
            "parser_confidence": parsed.get("confidence"),
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
    profile_id = request.args.get("profile_id", "").strip() or None
    profile_data = get_profile_data(profile_id)

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

    return render_json_response(enrich_tender(matched, profile=profile_data))


@app.post("/api/score")
def api_score():
    payload = request.get_json(silent=True) or {}
    prompt = (payload.get("prompt") or "").strip()
    profile_id = (payload.get("profile_id") or "").strip() or None

    profile_data = get_profile_data(profile_id)
    if not profile_data:
        return render_json_response({"error": "No active or selected profile found"}, 404)

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
    analysis = tender.get("analysis") or {}

    if profile and parsed_doc and OPENAI_API_KEY and PARSER_MODE in {"auto", "llm"}:
        assessment = llm_assess_bid(profile, parsed_doc)
        if assessment:
            return render_json_response(assessment)

    return render_json_response(
        {
            "summary": analysis.get("decision_summary") or "Prioritize compliance completeness, capability proof, and evaluation-fit evidence.",
            "actions": analysis.get("improvement_actions", []) or [
                "Validate all mandatory submission items against the tender document.",
                "Prepare a response structure aligned to specifications and scope headings.",
                "Surface tax, CSD, and B-BBEE evidence early in the submission pack.",
                "Address functionality thresholds and scoring cues explicitly where detected.",
                "Confirm briefing attendance requirements and date constraints before bid/no-bid.",
            ],
            "profile_signals": profile.get("strength_summary", []),
            "tender_signals": parsed_doc.get("evaluation_criteria", []),
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
