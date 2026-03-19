import io
import json
import os
import re
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
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from werkzeug.utils import secure_filename


APP_VERSION = os.getenv("APP_VERSION", "20260319-beta-5")
ETENDERS_BASE_URL = os.getenv("ETENDERS_BASE_URL", "https://ocds-api.etenders.gov.za")
ETENDERS_RELEASES_PATH = os.getenv("ETENDERS_RELEASES_PATH", "/api/OCDSReleases")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "45"))
MAX_TENDERS = int(os.getenv("MAX_TENDERS", "24"))
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///tenderai.db")
LOCAL_UPLOAD_DIR = os.getenv("LOCAL_UPLOAD_DIR", "uploads")
MAX_CONTENT_LENGTH = 20 * 1024 * 1024

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_PARSER_MODEL = os.getenv("OPENAI_PARSER_MODEL", "gpt-4o-mini").strip()
PARSER_MODE = os.getenv("PARSER_MODE", "auto").strip().lower()

ALLOWED_PROFILE_EXTENSIONS = {"pdf"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
app.secret_key = os.getenv("FLASK_SECRET_KEY", "tenderai-dev-secret")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)


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


engine = create_engine(DATABASE_URL, future=True)
Base.metadata.create_all(engine)


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
        return ""


def download_pdf_text_from_url(url: str) -> str:
    if not url:
        return ""

    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        content_type = (response.headers.get("Content-Type") or "").lower()

        if "pdf" in content_type or url.lower().endswith(".pdf"):
            return extract_pdf_text_from_bytes(response.content)

        return normalize_whitespace(response.text)
    except Exception:
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
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=90,
        )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception:
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
                "additionalProperties": False,
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
                "additionalProperties": False,
                "properties": {
                    "main_group": {"type": "array", "items": {"type": "string"}},
                    "division": {"type": "array", "items": {"type": "string"}},
                    "core_industry": {"type": "array", "items": {"type": "string"}},
                    "turnover_percentage": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["main_group", "division", "core_industry", "turnover_percentage"],
            },
            "contact_information": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "contact_type": {"type": ["string", "null"]},
                        "is_preferred": {"type": ["boolean", "null"]},
                        "name": {"type": ["string", "null"]},
                        "surname": {"type": ["string", "null"]},
                        "phone": {"type": ["string", "null"]},
                        "email": {"type": ["string", "null"]},
                        "website": {"type": ["string", "null"]},
                        "communication_preference": {"type": ["string", "null"]},
                        "is_csd_user": {"type": ["boolean", "null"]},
                    },
                    "required": [
                        "contact_type", "is_preferred", "name", "surname", "phone",
                        "email", "website", "communication_preference", "is_csd_user",
                    ],
                },
            },
            "address_information": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "address_line_1": {"type": ["string", "null"]},
                        "address_line_2": {"type": ["string", "null"]},
                        "suburb": {"type": ["string", "null"]},
                        "city": {"type": ["string", "null"]},
                        "municipality": {"type": ["string", "null"]},
                        "province": {"type": ["string", "null"]},
                        "country": {"type": ["string", "null"]},
                        "postal_code": {"type": ["string", "null"]},
                        "ward_number": {"type": ["string", "null"]},
                    },
                    "required": [
                        "address_line_1", "address_line_2", "suburb", "city", "municipality",
                        "province", "country", "postal_code", "ward_number",
                    ],
                },
            },
            "bank_information": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "verification_status": {"type": ["string", "null"]},
                    "verification_response": {"type": ["string", "null"]},
                },
                "required": ["verification_status", "verification_response"],
            },
            "tax_information": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "income_tax_number": {"type": ["string", "null"]},
                    "is_vat_vendor": {"type": ["boolean", "null"]},
                    "is_registered_with_sars": {"type": ["boolean", "null"]},
                    "tax_compliance_status": {"type": ["string", "null"]},
                    "compliance_pin_provided": {"type": ["boolean", "null"]},
                    "last_validation_date": {"type": ["string", "null"]},
                },
                "required": [
                    "income_tax_number", "is_vat_vendor", "is_registered_with_sars",
                    "tax_compliance_status", "compliance_pin_provided", "last_validation_date",
                ],
            },
            "bbbbee_information": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "certificate_number": {"type": ["string", "null"]},
                    "issue_date": {"type": ["string", "null"]},
                    "expiry_date": {"type": ["string", "null"]},
                    "verification_status": {"type": ["string", "null"]},
                    "black_ownership_percent": {"type": ["string", "null"]},
                    "women_ownership_percent": {"type": ["string", "null"]},
                    "youth_ownership_percent": {"type": ["string", "null"]},
                },
                "required": [
                    "certificate_number", "issue_date", "expiry_date", "verification_status",
                    "black_ownership_percent", "women_ownership_percent", "youth_ownership_percent",
                ],
            },
            "accreditations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "body": {"type": ["string", "null"]},
                        "description": {"type": ["string", "null"]},
                        "accreditation_number": {"type": ["string", "null"]},
                        "issue_date": {"type": ["string", "null"]},
                        "expiry_date": {"type": ["string", "null"]},
                        "status": {"type": ["string", "null"]},
                        "verification_status": {"type": ["string", "null"]},
                    },
                    "required": [
                        "body", "description", "accreditation_number", "issue_date",
                        "expiry_date", "status", "verification_status",
                    ],
                },
            },
            "directors": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": ["string", "null"]},
                        "ownership_flags": {"type": "array", "items": {"type": "string"}},
                        "youth_flag": {"type": ["boolean", "null"]},
                        "disability_flag": {"type": ["boolean", "null"]},
                        "veteran_flag": {"type": ["boolean", "null"]},
                        "government_employee_flag": {"type": ["boolean", "null"]},
                    },
                    "required": [
                        "name", "ownership_flags", "youth_flag", "disability_flag",
                        "veteran_flag", "government_employee_flag",
                    ],
                },
            },
            "ownership_summary": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "black_owned": {"type": ["boolean", "null"]},
                    "youth_owned": {"type": ["boolean", "null"]},
                    "township_based": {"type": ["boolean", "null"]},
                    "rural_based": {"type": ["boolean", "null"]},
                },
                "required": ["black_owned", "youth_owned", "township_based", "rural_based"],
            },
            "commodities": {"type": "array", "items": {"type": "string"}},
            "provinces": {"type": "array", "items": {"type": "string"}},
            "keywords": {"type": "array", "items": {"type": "string"}},
            "ai_enrichment": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "summary": {"type": ["string", "null"]},
                    "capability_keywords": {"type": "array", "items": {"type": "string"}},
                    "compliance_flags": {"type": "array", "items": {"type": "string"}},
                    "risk_notes": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["summary", "capability_keywords", "compliance_flags", "risk_notes"],
            },
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
    lines = chunk_lines(text)
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

    email_matches = sorted(set(re.findall(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", text, re.IGNORECASE)))
    phone_matches = sorted(set(re.findall(r"(?:\+27|0)[0-9][0-9\s\-]{7,}", text, re.IGNORECASE)))

    if email_matches or phone_matches:
        profile["contact_information"].append(
            {
                "contact_type": "primary",
                "is_preferred": True,
                "name": None,
                "surname": None,
                "phone": phone_matches[0] if phone_matches else None,
                "email": email_matches[0] if email_matches else None,
                "website": None,
                "communication_preference": None,
                "is_csd_user": None,
            }
        )

    provinces = [
        "Eastern Cape", "Free State", "Gauteng", "KwaZulu-Natal", "Limpopo",
        "Mpumalanga", "Northern Cape", "North West", "Western Cape",
    ]
    profile["provinces"] = [province for province in provinces if province.lower() in text_lower]

    accreditation_lines = []
    for line in lines:
        if re.search(r"(accredit|iso|cidb|sacec|sacpcmp|nhbrc|saqa|qcto)", line, re.IGNORECASE):
            accreditation_lines.append(line)

    for line in accreditation_lines[:8]:
        profile["accreditations"].append(
            {
                "body": first_match([r"(CIDB|ISO|SACEC|SACPCMP|NHBRC|SAQA|QCTO)"], line),
                "description": line[:250],
                "accreditation_number": first_match([r"(?:number|no\.?)\s*[:\-]?\s*([A-Z0-9\-\/]+)"], line),
                "issue_date": None,
                "expiry_date": None,
                "status": "detected",
                "verification_status": None,
            }
        )

    profile["ai_enrichment"]["summary"] = build_profile_summary_text(profile)
    profile["ai_enrichment"]["capability_keywords"] = []
    profile["ai_enrichment"]["compliance_flags"] = []
    profile["ai_enrichment"]["risk_notes"] = []
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

    params = {
        "pageNumber": page_number,
        "pageSize": page_size,
    }
    if date_from:
        params["dateFrom"] = date_from
    if date_to:
        params["dateTo"] = date_to

    response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
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
        if "tender" in title:
            score += 8
        if "bid" in title:
            score += 7
        if "rfq" in title or "rfp" in title:
            score += 6
        if "document" in title:
            score += 5
        if "specification" in title or "terms of reference" in title:
            score += 5
        if "advert" in title:
            score -= 2
        if "notice" in title:
            score -= 1

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
    scoring = find_keyword_lines(
        text,
        ["80/20", "90/10", "preference point", "specific goals", "bbbee", "functionality", "evaluation criteria", "score", "points"],
    )

    mandatory = find_keyword_lines(
        text,
        ["must submit", "mandatory", "compulsory", "required", "failure to", "tax clearance", "csd", "cidb", "proof", "attach", "non-responsive"],
    )

    specifications = extract_section(
        text,
        ["scope of work", "specification", "specifications", "terms of reference", "deliverables"],
        ["special conditions", "evaluation", "briefing", "contact person", "closing date"],
    )

    special_conditions = extract_section(
        text,
        ["special conditions", "general conditions", "conditions of tender", "conditions"],
        ["evaluation", "briefing", "scope of work", "specification", "contact person"],
    )

    briefing = find_keyword_lines(
        text,
        ["briefing", "compulsory briefing", "briefing session", "site inspection", "site briefing", "clarification meeting"],
    )

    compliance = find_keyword_lines(
        text,
        ["tax compliance", "csd", "pin", "sbd", "declaration", "proof of registration", "bank", "iso", "cidb", "good standing"],
    )

    evaluation = find_keyword_lines(
        text,
        ["pppfa", "80/20", "90/10", "specific goals", "functionality", "threshold", "minimum score", "price and preference"],
    )

    return {
        "scoring_criteria": scoring,
        "mandatory_requirements": mandatory,
        "specifications_scope": specifications,
        "special_conditions": special_conditions,
        "briefing_details": briefing,
        "compliance_cues": compliance,
        "evaluation_cues": evaluation,
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
        prompt or "",
    ]
    tender_text = " ".join(tender_text_parts).lower()

    fit_score = 40
    reasons = []
    risks = []
    readiness = []
    competitiveness = "Unknown"

    if profile_data:
        profile_keywords = infer_profile_keywords(profile_data)
        overlap = sorted({token for token in profile_keywords if len(token) > 3 and token in tender_text})
        fit_score += min(len(overlap) * 4, 24)
        if overlap:
            reasons.append(f"Capability overlap: {', '.join(overlap[:6])}")

        tax_status = (profile_data.get("tax_information", {}).get("tax_compliance_status") or "").lower()
        if "compliant" in tax_status or "valid" in tax_status:
            fit_score += 8
            readiness.append("Tax compliance signal detected")
        else:
            risks.append("Tax compliance status not clearly confirmed in profile")

        bbbee_status = (profile_data.get("bbbbee_information", {}).get("verification_status") or "").lower()
        if bbbee_status:
            readiness.append(f"B-BBEE signal: {bbbee_status}")

        profile_provinces = set(profile_data.get("provinces", []))
        tender_province = tender.get("province")
        if tender_province and tender_province in profile_provinces:
            fit_score += 6
            reasons.append(f"Province match: {tender_province}")

    mandatory_requirements = parsed_doc.get("mandatory_requirements", [])
    if mandatory_requirements:
        risks.append("Mandatory submission items detected in tender document")

    evaluation_cues = " ".join(parsed_doc.get("evaluation_cues", [])).lower()
    if "90/10" in evaluation_cues:
        competitiveness = "Likely stronger weight on price, with preference contribution"
    elif "80/20" in evaluation_cues:
        competitiveness = "Likely balanced SME-accessible preference framework"
    elif "functionality" in evaluation_cues:
        competitiveness = "Likely prequalification through functionality threshold"

    if "compulsory briefing" in " ".join(parsed_doc.get("briefing_details", [])).lower():
        risks.append("Compulsory briefing cue detected")

    if "cidb" in tender_text:
        risks.append("CIDB or construction-class compliance may be required")
    if "csd" in tender_text:
        readiness.append("CSD alignment likely relevant")
    if "sbd" in tender_text:
        risks.append("Standard bidding documents likely required")

    fit_score = max(5, min(95, fit_score))

    if fit_score >= 75:
        fit_band = "High fit"
    elif fit_score >= 55:
        fit_band = "Medium fit"
    else:
        fit_band = "Low fit"

    investment = "Medium"
    if len(mandatory_requirements) >= 8:
        investment = "High"
    elif len(mandatory_requirements) <= 2:
        investment = "Low"

    return {
        "fit_score": fit_score,
        "fit_band": fit_band,
        "fit_reasons": reasons[:5],
        "risk_flags": risks[:6],
        "competitiveness": competitiveness,
        "execution_investment": investment,
        "strategic_readiness": readiness[:6],
    }


def normalize_tender_release(item: dict[str, Any]) -> dict[str, Any]:
    tender = item.get("tender", {}) or {}
    buyer = item.get("buyer", {}) or {}
    documents = tender.get("documents", []) or []
    tender_period = tender.get("tenderPeriod", {}) or {}
    value = tender.get("value", {}) or {}

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
        "tender_period": tender_period,
        "tender_value": value,
        "documents": documents,
        "lots": tender.get("lots", []),
        "parties": item.get("parties", []),
        "awards": item.get("awards", []),
        "contracts": item.get("contracts", []),
        "related_processes": item.get("relatedProcesses", []),
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
    with Session(engine) as session:
        return session.get(Profile, profile_id)


def get_active_profile_record() -> Optional[Profile]:
    with Session(engine) as session:
        statement = select(Profile).where(Profile.is_active.is_(True)).order_by(Profile.uploaded_at.desc())
        return session.scalar(statement)


def get_profile_data(profile_id: Optional[str]) -> Optional[dict[str, Any]]:
    record = get_profile_record(profile_id) if profile_id else get_active_profile_record()
    if not record:
        return None
    return json_loads_safe(record.parsed_json, {})


@app.get("/")
def home():
    today = datetime.now(timezone.utc).date()
    date_from = (today - timedelta(days=7)).isoformat()
    date_to = (today + timedelta(days=30)).isoformat()

    tenders = []
    try:
        releases = fetch_tenders(date_from=date_from, date_to=date_to, page_number=1, page_size=10)
        for item in releases[:8]:
            tenders.append(normalize_tender_release(item))
    except Exception:
        tenders = []

    with Session(engine) as session:
        profiles = session.scalars(select(Profile).order_by(Profile.is_active.desc(), Profile.uploaded_at.desc())).all()

    return render_template("home.html", tenders=tenders, profiles=profiles)


@app.get("/profiles")
def profiles_page():
    with Session(engine) as session:
        profiles = session.scalars(select(Profile).order_by(Profile.is_active.desc(), Profile.uploaded_at.desc())).all()

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
            "time": datetime.now(timezone.utc).isoformat(),
        }
    )


@app.get("/api/profiles")
def api_profiles_list():
    with Session(engine) as session:
        profiles = session.scalars(select(Profile).order_by(Profile.is_active.desc(), Profile.uploaded_at.desc())).all()

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

    with Session(engine) as session:
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
    with Session(engine) as session:
        target = session.get(Profile, profile_id)
        if not target:
            return render_json_response({"error": "Profile not found"}, 404)

        all_profiles = session.scalars(select(Profile)).all()
        for profile in all_profiles:
            profile.is_active = profile.id == profile_id

        session.commit()

    return render_json_response({"status": "ok", "active_profile_id": profile_id})


@app.delete("/api/profiles/<profile_id>")
def api_profiles_delete(profile_id: str):
    with Session(engine) as session:
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

    return render_json_response({"status": "deleted", "id": profile_id})


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

    advice = {
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
    return render_json_response(advice)


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
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), debug=True)
