import io
import json
import os
import re
import threading
import traceback
import uuid
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urljoin

import requests
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt
from flask import (
    Flask,
    abort,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
    flash,
)
from pypdf import PdfReader
from sqlalchemy import Boolean, DateTime, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from werkzeug.utils import secure_filename


APP_VERSION = os.getenv("APP_VERSION", "20260325-beta-20")
ETENDERS_BASE_URL = os.getenv("ETENDERS_BASE_URL", "https://ocds-api.etenders.gov.za")
ETENDERS_RELEASES_PATH = os.getenv("ETENDERS_RELEASES_PATH", "/api/OCDSReleases")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "45"))
MAX_TENDERS = int(os.getenv("MAX_TENDERS", "200"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "20"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip() or "sqlite:///tenderai.db"
LOCAL_UPLOAD_DIR = os.getenv("LOCAL_UPLOAD_DIR", "/tmp/uploads")
MAX_CONTENT_LENGTH = 20 * 1024 * 1024

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_PARSER_MODEL = os.getenv("OPENAI_PARSER_MODEL", "gpt-4o-mini").strip()
PARSER_MODE = os.getenv("PARSER_MODE", "auto").strip().lower()

ALLOWED_PROFILE_EXTENSIONS = {"pdf"}

http = requests.Session()
http.headers.update({"User-Agent": "TenderAI/1.0"})

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
app.secret_key = os.getenv("FLASK_SECRET_KEY", "tenderai-dev-secret")


AI_TELEMETRY: dict[str, Any] = {
    "configured": bool(OPENAI_API_KEY),
    "parser_mode": PARSER_MODE,
    "model": OPENAI_PARSER_MODEL,
    "attempts_total": 0,
    "success_total": 0,
    "failure_total": 0,
    "stages": {
        "supplier_extraction": {"attempts": 0, "success": 0, "failure": 0},
        "tender_extraction": {"attempts": 0, "success": 0, "failure": 0},
        "bid_assessment": {"attempts": 0, "success": 0, "failure": 0},
        "proposal_writer": {"attempts": 0, "success": 0, "failure": 0},
        "debug_ping": {"attempts": 0, "success": 0, "failure": 0},
    },
    "last_attempt_at": None,
    "last_success_at": None,
    "last_failure_at": None,
    "last_stage": None,
    "last_status_code": None,
    "last_error": None,
    "last_request_id": None,
    "recent_events": deque(maxlen=25),
}


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ai_event(stage: str, status: str, **extra: Any) -> None:
    event = {"time": iso_now(), "stage": stage, "status": status}
    event.update(extra)
    AI_TELEMETRY["recent_events"].appendleft(event)


def ai_mark_attempt(stage: str, payload_preview: Optional[str] = None) -> None:
    AI_TELEMETRY["attempts_total"] += 1
    AI_TELEMETRY["stages"].setdefault(stage, {"attempts": 0, "success": 0, "failure": 0})
    AI_TELEMETRY["stages"][stage]["attempts"] += 1
    AI_TELEMETRY["last_attempt_at"] = iso_now()
    AI_TELEMETRY["last_stage"] = stage
    AI_TELEMETRY["last_error"] = None
    ai_event(stage, "attempt", preview=(payload_preview or "")[:300])


def ai_mark_success(stage: str, status_code: int, request_id: Optional[str] = None) -> None:
    AI_TELEMETRY["success_total"] += 1
    AI_TELEMETRY["stages"][stage]["success"] += 1
    AI_TELEMETRY["last_success_at"] = iso_now()
    AI_TELEMETRY["last_status_code"] = status_code
    AI_TELEMETRY["last_request_id"] = request_id
    ai_event(stage, "success", status_code=status_code, request_id=request_id)


def ai_mark_failure(
    stage: str,
    error: str,
    status_code: Optional[int] = None,
    request_id: Optional[str] = None,
    response_excerpt: Optional[str] = None,
) -> None:
    AI_TELEMETRY["failure_total"] += 1
    AI_TELEMETRY["stages"][stage]["failure"] += 1
    AI_TELEMETRY["last_failure_at"] = iso_now()
    AI_TELEMETRY["last_status_code"] = status_code
    AI_TELEMETRY["last_request_id"] = request_id
    AI_TELEMETRY["last_error"] = error
    ai_event(
        stage,
        "failure",
        status_code=status_code,
        request_id=request_id,
        error=error[:300],
        response_excerpt=(response_excerpt or "")[:500],
    )


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


class AnalysisJob(Base):
    __tablename__ = "analysis_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tender_id: Mapped[str] = mapped_column(String(255))
    profile_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    result_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ProfileIssue(Base):
    __tablename__ = "profile_issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[str] = mapped_column(String(36), index=True)
    issue_key: Mapped[str] = mapped_column(String(255), index=True)
    issue_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    source_tender_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


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
    except Exception as exc:
        DB_INIT_ERROR = str(exc)
        engine = None
        SessionLocal = None
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
    today = datetime.now(timezone.utc).date()
    return (dt.date() - today).days


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
    url = sanitize_document_url(url)
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


def _extract_response_text(data: dict[str, Any]) -> Optional[str]:
    if isinstance(data.get("output_text"), str) and data.get("output_text"):
        return data["output_text"]
    for item in data.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text" and content.get("text"):
                    return content["text"]
    return None


def openai_responses_json_schema(
    *,
    stage: str,
    system_prompt: str,
    user_prompt: str,
    schema_name: str,
    schema: Optional[dict[str, Any]] = None,
    temperature: float = 0.1,
) -> Optional[dict[str, Any]]:
    if not OPENAI_API_KEY:
        ai_mark_failure(stage, "OPENAI_API_KEY missing")
        return None

    ai_mark_attempt(stage, payload_preview=user_prompt[:250])

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    payload: dict[str, Any] = {
        "model": OPENAI_PARSER_MODEL,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
            {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
        ],
        "temperature": temperature,
        "store": False,
    }

    if schema:
        payload["text"] = {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "schema": schema,
                "strict": True,
            }
        }

    try:
        response = http.post("https://api.openai.com/v1/responses", headers=headers, json=payload, timeout=120)
        request_id = response.headers.get("x-request-id") or response.headers.get("request-id")

        if not response.ok:
            ai_mark_failure(
                stage,
                f"OpenAI request failed with status {response.status_code}",
                status_code=response.status_code,
                request_id=request_id,
                response_excerpt=response.text[:1000],
            )
            return None

        data = response.json()
        text_output = _extract_response_text(data)
        if not text_output:
            ai_mark_failure(
                stage,
                "No structured output text returned",
                status_code=response.status_code,
                request_id=request_id,
                response_excerpt=json.dumps(data)[:1000],
            )
            return None

        if not schema:
            ai_mark_success(stage, response.status_code, request_id=request_id)
            return {"text": text_output}

        parsed = json.loads(text_output)
        if not isinstance(parsed, dict):
            ai_mark_failure(
                stage,
                "Structured output was not a JSON object",
                status_code=response.status_code,
                request_id=request_id,
                response_excerpt=text_output[:1000],
            )
            return None

        ai_mark_success(stage, response.status_code, request_id=request_id)
        return parsed
    except Exception as exc:
        ai_mark_failure(stage, f"Exception during OpenAI call: {exc}")
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
                "required": ["legal_name", "trading_name", "registration_number", "vat_number", "csd_number", "entity_type", "country", "province"],
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
                    "tax_compliance_present", "bbbee_status_present", "bbbee_level",
                    "cidb_grade", "sars_pin_present", "bank_verification_present",
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
            "document_type", "supplier_identity", "core_capabilities", "services_offered",
            "sector_tags", "commodity_tags", "geographic_coverage", "compliance_signals",
            "certifications_and_accreditations", "past_performance_evidence", "capacity_signals",
            "key_contacts", "strength_summary", "missing_or_unclear_evidence", "confidence",
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
                    "issued_date_text": {"type": ["string", "null"]},
                    "closing_date_text": {"type": ["string", "null"]},
                },
                "required": ["tender_number", "title", "buyer_name", "buyer_type", "province", "issued_date_text", "closing_date_text"],
            },
            "scope_summary": {"type": ["string", "null"]},
            "deliverables": {"type": "array", "items": {"type": "string"}},
            "required_capabilities": {"type": "array", "items": {"type": "string"}},
            "mandatory_documents": {"type": "array", "items": {"type": "string"}},
            "compliance_requirements": {"type": "array", "items": {"type": "string"}},
            "functionality_criteria": {"type": "array", "items": {"type": "string"}},
            "evaluation_criteria": {"type": "array", "items": {"type": "string"}},
            "price_preference_system": {"type": ["string", "null"], "enum": ["80/20", "90/10", "other", None]},
            "specific_goals_or_preference_cues": {"type": "array", "items": {"type": "string"}},
            "briefing": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "briefing_required": {"type": ["boolean", "null"]},
                    "briefing_compulsory": {"type": ["boolean", "null"]},
                    "briefing_date_text": {"type": ["string", "null"]},
                },
                "required": ["briefing_required", "briefing_compulsory", "briefing_date_text"],
            },
            "submission": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "deadline_text": {"type": ["string", "null"]},
                    "validity_period_text": {"type": ["string", "null"]},
                    "proposal_required": {"type": ["boolean", "null"]},
                    "proposal_format_cues": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["deadline_text", "validity_period_text", "proposal_required", "proposal_format_cues"],
            },
            "special_conditions": {"type": "array", "items": {"type": "string"}},
            "risk_flags": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number"},
        },
        "required": [
            "document_type", "tender_identity", "scope_summary", "deliverables",
            "required_capabilities", "mandatory_documents", "compliance_requirements",
            "functionality_criteria", "evaluation_criteria", "price_preference_system",
            "specific_goals_or_preference_cues", "briefing", "submission",
            "special_conditions", "risk_flags", "confidence",
        ],
    }


def bid_assessment_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "decision_summary": {"type": "string"},
            "qualification_status": {"type": "string", "enum": ["likely_qualifies", "partially_qualifies", "unlikely_to_qualify"]},
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
            "decision_summary", "qualification_status", "fit_score", "win_probability_band",
            "bid_recommendation", "capability_strengths", "compliance_strengths",
            "gaps_or_disqualifiers", "competitiveness_assessment", "execution_burden",
            "strategic_readiness", "improvement_actions", "critical_unknowns", "confidence",
        ],
    }


def llm_extract_supplier_profile(text: str) -> Optional[dict[str, Any]]:
    return openai_responses_json_schema(
        stage="supplier_extraction",
        system_prompt=(
            "You are TenderAI's supplier-profile interpreter for South African procurement. "
            "Extract only facts supported by the supplier profile. Do not guess. "
            "Use null or empty arrays when evidence is missing. "
            "Focus on capabilities, compliance cues, accreditations, geographic fit, and past performance signals. "
            "Return only valid JSON matching the schema."
        ),
        user_prompt=f"Interpret this supplier profile for procurement intelligence.\n\n{text[:30000]}",
        schema_name="supplier_profile_extraction",
        schema=supplier_extraction_schema(),
        temperature=0.1,
    )


def llm_extract_tender_document(text: str) -> Optional[dict[str, Any]]:
    return openai_responses_json_schema(
        stage="tender_extraction",
        system_prompt=(
            "You are TenderAI's tender-document interpreter for South African procurement. "
            "Extract only facts supported by the tender document. Do not guess. "
            "Use null or empty arrays when evidence is missing. "
            "Focus on scope, required capabilities, mandatory documents, compliance obligations, functionality criteria, "
            "preference point cues, briefing obligations, special conditions, and whether a written proposal or technical response appears required. "
            "Return only valid JSON matching the schema."
        ),
        user_prompt=f"Interpret this tender document for procurement intelligence.\n\n{text[:30000]}",
        schema_name="tender_document_extraction",
        schema=tender_extraction_schema(),
        temperature=0.1,
    )


def get_profile_issue_context(profile_id: str) -> dict[str, list[str]]:
    with db_session() as session:
        issues = session.scalars(select(ProfileIssue).where(ProfileIssue.profile_id == profile_id)).all()
    fixed = [i.issue_text for i in issues if i.status == "fixed"]
    pending = [i.issue_text for i in issues if i.status == "pending"]
    return {"fixed": fixed, "pending": pending}


def llm_assess_bid(
    supplier_obj: dict[str, Any],
    tender_obj: dict[str, Any],
    issue_context: Optional[dict[str, list[str]]] = None,
) -> Optional[dict[str, Any]]:
    fixed = (issue_context or {}).get("fixed", [])
    pending = (issue_context or {}).get("pending", [])
    return openai_responses_json_schema(
        stage="bid_assessment",
        system_prompt=(
            "You are TenderAI's bid assessment engine for South African procurement. "
            "Compare the supplier profile against the tender requirements. "
            "Do not invent strengths or qualifications that are not supported by the extracted objects. "
            "Be conservative when evidence is incomplete. "
            "If certain profile issues are marked FIXED, treat them as remedied for opportunity-planning purposes, "
            "but still be transparent that verification may still be needed. "
            "Pending issues remain active risks. "
            "Return only valid JSON matching the schema."
        ),
        user_prompt=(
            f"Assess whether this supplier is a strong candidate for this tender.\n\n"
            f"Supplier object:\n{json.dumps(supplier_obj, ensure_ascii=False)}\n\n"
            f"Tender object:\n{json.dumps(tender_obj, ensure_ascii=False)}\n\n"
            f"Profile issues marked FIXED:\n{json.dumps(fixed, ensure_ascii=False)}\n\n"
            f"Profile issues marked PENDING:\n{json.dumps(pending, ensure_ascii=False)}\n\n"
            f"Required output:\n"
            f"- determine likely qualification status\n"
            f"- assign fit score\n"
            f"- estimate win probability band\n"
            f"- recommend go, go_with_caution, or no_go\n"
            f"- explain key strengths, gaps, and improvements"
        ),
        schema_name="bid_assessment",
        schema=bid_assessment_schema(),
        temperature=0.1,
    )


def llm_write_proposal(
    supplier_obj: dict[str, Any],
    tender_obj: dict[str, Any],
    assessment_obj: dict[str, Any],
) -> Optional[str]:
    result = openai_responses_json_schema(
        stage="proposal_writer",
        system_prompt=(
            "You are TenderAI's proposal writer for South African tenders. "
            "Write a practical first-draft tender proposal in clear professional English. "
            "Use only facts supported by the supplier extraction, tender extraction, and bid assessment. "
            "Do not invent company credentials, project history, or compliance details. "
            "If certain evidence is missing, leave placeholders in square brackets. "
            "Structure the proposal so it can be edited and submitted by the supplier."
        ),
        user_prompt=(
            f"Write a tender proposal draft.\n\n"
            f"Supplier extraction:\n{json.dumps(supplier_obj, ensure_ascii=False)}\n\n"
            f"Tender extraction:\n{json.dumps(tender_obj, ensure_ascii=False)}\n\n"
            f"Bid assessment:\n{json.dumps(assessment_obj, ensure_ascii=False)}\n\n"
            f"Draft requirements:\n"
            f"- executive cover/introduction\n"
            f"- supplier understanding of the scope\n"
            f"- approach and methodology\n"
            f"- capability alignment\n"
            f"- compliance and documentation checklist\n"
            f"- project team / capacity section with placeholders where needed\n"
            f"- risk and mitigation section\n"
            f"- closing statement"
        ),
        schema_name="",
        schema=None,
        temperature=0.2,
    )
    return result.get("text") if result else None


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


def parse_profile_pdf_text_heuristic(text: str) -> dict[str, Any]:
    profile = build_empty_profile_schema()
    text_lower = text.lower()
    identity = profile["supplier_identity"]
    compliance = profile["compliance_signals"]

    identity["legal_name"] = first_match(
        [r"(?:legal\s*name|supplier\s*name|enterprise\s*name)\s*[:\-]\s*(.+)", r"registered\s*name\s*[:\-]\s*(.+)"], text
    )
    identity["trading_name"] = first_match([r"(?:trading\s*name|business\s*name)\s*[:\-]\s*(.+)"], text)
    identity["registration_number"] = first_match([r"(?:registration\s*(?:number|no\.?))\s*[:\-]\s*([A-Z0-9/\-]+)"], text)
    identity["vat_number"] = first_match([r"(?:vat\s*(?:number|no\.?))\s*[:\-]\s*([A-Z0-9/\-]+)"], text)
    identity["csd_number"] = first_match([r"(?:csd\s*(?:number|no\.?))\s*[:\-]?\s*([A-Z0-9/\-]+)"], text)
    identity["entity_type"] = first_match([r"(?:supplier\s*type|entity\s*type)\s*[:\-]\s*(.+)"], text)

    provinces = ["Eastern Cape", "Free State", "Gauteng", "KwaZulu-Natal", "Limpopo", "Mpumalanga", "Northern Cape", "North West", "Western Cape"]
    found_provinces = [province for province in provinces if province.lower() in text_lower]
    identity["province"] = found_provinces[0] if found_provinces else None
    profile["geographic_coverage"] = found_provinces

    profile["core_capabilities"] = find_keyword_lines(
        text,
        ["services", "supply", "maintenance", "construction", "consulting", "training", "installation"],
        limit=8,
    )
    profile["services_offered"] = profile["core_capabilities"][:]
    profile["sector_tags"] = [token for token in ["construction", "engineering", "it", "consulting", "logistics", "cleaning", "training", "security", "catering"] if token in text_lower]
    profile["commodity_tags"] = [token for token in ["equipment", "software", "transport", "furniture", "security", "catering", "civil", "electrical"] if token in text_lower]

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
        "core_capabilities", "services_offered", "sector_tags", "commodity_tags",
        "geographic_coverage", "certifications_and_accreditations",
        "past_performance_evidence", "capacity_signals", "key_contacts",
        "strength_summary", "missing_or_unclear_evidence",
    ]:
        if not isinstance(normalized.get(key), list):
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
    bits = []
    if identity.get("legal_name"):
        bits.append(identity["legal_name"])
    if profile_data.get("core_capabilities"):
        bits.append(", ".join(profile_data["core_capabilities"][:3]))
    if compliance.get("bbbee_level"):
        bits.append(f"B-BBEE {compliance['bbbee_level']}")

    return {
        "company_name": identity.get("legal_name") or identity.get("trading_name"),
        "summary_text": " | ".join(bits) or "Profile processed and stored.",
        "industry_main_groups": profile_data.get("sector_tags", []),
        "industry_divisions": profile_data.get("commodity_tags", []),
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


def fetch_tender_page(page_number: int, page_size: int = 100) -> list[dict[str, Any]]:
    url = urljoin(ETENDERS_BASE_URL, ETENDERS_RELEASES_PATH)
    response = http.get(url, params={"pageNumber": page_number, "pageSize": page_size}, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return extract_releases(response.json())


def fetch_all_current_tenders(max_pages: int = MAX_PAGES, page_size: int = 100) -> list[dict[str, Any]]:
    seen: set[str] = set()
    all_items: list[dict[str, Any]] = []

    for page in range(1, max_pages + 1):
        try:
            items = fetch_tender_page(page, page_size=page_size)
        except Exception:
            traceback.print_exc()
            break

        if not items:
            break

        live_items = [item for item in items if is_live_tender_release(item)]
        for item in live_items:
            key = str(item.get("id") or item.get("ocid") or uuid.uuid4())
            if key not in seen:
                seen.add(key)
                all_items.append(item)

    return all_items


def pick_best_tender_document(documents: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not documents:
        return None
    scored = []
    for doc in documents:
        title = " ".join(str(doc.get(key, "")) for key in ["title", "description", "documentType"]).lower()
        url = (sanitize_document_url(doc.get("url")) or sanitize_document_url(doc.get("downloadUrl")) or sanitize_document_url(doc.get("uri")) or sanitize_document_url(doc.get("href")) or "").lower()
        score = 0
        if "pdf" in url or url.endswith(".pdf"):
            score += 10
        if "terms of reference" in title or "tor" in title:
            score += 10
        if "specification" in title or "scope of work" in title or "statement of work" in title:
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
    pattern = rf"(?is)\b(?:{pattern_headings})\b[:\s\-]*(.*?)(?=(?:\n[A-Z][A-Z0-9 /()\-]{{3,}})|(?:\b(?:{pattern_next})\b)|\Z)"
    matches = re.findall(pattern, text)
    results = []
    for match in matches[:3]:
        for line in chunk_lines(match)[:20]:
            if 4 < len(line) < 300:
                results.append(line)
    return list(dict.fromkeys(results))[:12]


def find_keyword_lines(text: str, keywords: list[str], limit: int = 12) -> list[str]:
    results = []
    for line in chunk_lines(text):
        ll = line.lower()
        if any(keyword.lower() in ll for keyword in keywords):
            results.append(line)
        if len(results) >= limit:
            break
    return results


def parse_tender_document_text_heuristic(text: str) -> dict[str, Any]:
    if not text:
        return {
            "document_type": "tender_document",
            "tender_identity": {
                "tender_number": None, "title": None, "buyer_name": None, "buyer_type": None,
                "province": None, "issued_date_text": None, "closing_date_text": None,
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
            "briefing": {"briefing_required": None, "briefing_compulsory": None, "briefing_date_text": None},
            "submission": {"deadline_text": None, "validity_period_text": None, "proposal_required": None, "proposal_format_cues": []},
            "special_conditions": [],
            "risk_flags": [],
            "confidence": 0.0,
        }

    scoring = find_keyword_lines(text, ["80/20", "90/10", "functionality", "points", "specific goals"], limit=8)
    mandatory = find_keyword_lines(text, ["must submit", "mandatory", "required", "csd", "cidb", "tax", "sbd"], limit=10)
    briefing = find_keyword_lines(text, ["briefing", "site inspection", "clarification meeting", "compulsory briefing"], limit=6)
    compliance = find_keyword_lines(text, ["tax compliance", "csd", "pin", "declaration", "cidb", "bbbee"], limit=10)
    evaluation = find_keyword_lines(text, ["pppfa", "80/20", "90/10", "specific goals", "functionality", "evaluation"], limit=10)
    proposal_cues = find_keyword_lines(text, ["proposal", "technical proposal", "methodology", "approach", "implementation plan"], limit=8)
    scope = extract_section(text, ["scope of work", "specification", "specifications", "terms of reference", "deliverables", "project scope"], ["special conditions", "evaluation", "briefing", "contact person", "closing date", "mandatory requirements"])[:10]
    special = extract_section(text, ["special conditions", "conditions of tender", "conditions", "special condition"], ["evaluation", "briefing", "scope of work", "specification", "contact person", "closing date"])[:8]

    return {
        "document_type": "tender_document",
        "tender_identity": {
            "tender_number": None, "title": None, "buyer_name": None, "buyer_type": None,
            "province": None, "issued_date_text": None, "closing_date_text": None,
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
            "proposal_required": bool(proposal_cues),
            "proposal_format_cues": proposal_cues,
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
        normalized["tender_identity"]["issued_date_text"] = normalized["tender_identity"].get("issued_date_text") or tender.get("issue_date_display")
        normalized["tender_identity"]["closing_date_text"] = normalized["tender_identity"].get("closing_date_text") or tender.get("closing_date_display")

    for key in [
        "deliverables", "required_capabilities", "mandatory_documents", "compliance_requirements",
        "functionality_criteria", "evaluation_criteria", "specific_goals_or_preference_cues",
        "special_conditions", "risk_flags",
    ]:
        if not isinstance(normalized.get(key), list):
            normalized[key] = []

    if not isinstance(normalized.get("briefing"), dict):
        normalized["briefing"] = {"briefing_required": None, "briefing_compulsory": None, "briefing_date_text": None}
    if not isinstance(normalized.get("submission"), dict):
        normalized["submission"] = {"deadline_text": None, "validity_period_text": None, "proposal_required": None, "proposal_format_cues": []}
    if not isinstance(normalized["submission"].get("proposal_format_cues"), list):
        normalized["submission"]["proposal_format_cues"] = []
    if not isinstance(normalized.get("confidence"), (int, float)):
        normalized["confidence"] = 0.5

    return normalized


def parse_tender_document_text(text: str, tender: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    if PARSER_MODE in {"auto", "llm"} and OPENAI_API_KEY and text:
        parsed = llm_extract_tender_document(text)
        if parsed:
            return normalize_tender_extraction(parsed, tender=tender)
    return normalize_tender_extraction(parse_tender_document_text_heuristic(text), tender=tender)


def prefit_score_from_profile(tender: dict[str, Any], profile_data: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not profile_data:
        return {"score": None, "band": "No profile", "reasons": []}

    profile_terms = set()
    for key in ["sector_tags", "commodity_tags", "core_capabilities", "services_offered"]:
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


def upsert_profile_issues(profile_id: str, tender_id: str, issues: list[str]) -> None:
    cleaned = [issue.strip() for issue in issues if issue and issue.strip()]
    if not cleaned:
        return

    with db_session() as session:
        for issue_text in cleaned:
            issue_key = issue_text.lower()[:255]
            existing = session.scalar(
                select(ProfileIssue).where(
                    ProfileIssue.profile_id == profile_id,
                    ProfileIssue.issue_key == issue_key,
                )
            )
            if existing:
                existing.issue_text = issue_text
                existing.source_tender_id = tender_id
                existing.updated_at = datetime.now(timezone.utc)
            else:
                session.add(
                    ProfileIssue(
                        profile_id=profile_id,
                        issue_key=issue_key,
                        issue_text=issue_text,
                        status="pending",
                        source_tender_id=tender_id,
                        updated_at=datetime.now(timezone.utc),
                    )
                )
        session.commit()


def get_profile_issues(profile_id: str) -> list[dict[str, Any]]:
    with db_session() as session:
        rows = session.scalars(select(ProfileIssue).where(ProfileIssue.profile_id == profile_id).order_by(ProfileIssue.updated_at.desc())).all()
    return [
        {
            "id": row.id,
            "issue_key": row.issue_key,
            "issue_text": row.issue_text,
            "status": row.status,
            "source_tender_id": row.source_tender_id,
            "updated_at": row.updated_at.isoformat(),
        }
        for row in rows
    ]


def score_fit(tender: dict[str, Any], parsed_doc: dict[str, Any], profile_data: Optional[dict[str, Any]], prompt: str, profile_id: Optional[str] = None) -> dict[str, Any]:
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

    issue_context = get_profile_issue_context(profile_id) if profile_id else {"fixed": [], "pending": []}
    assessment = None
    if PARSER_MODE in {"auto", "llm"} and OPENAI_API_KEY:
        assessment = llm_assess_bid(profile_data, parsed_doc, issue_context=issue_context)

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

    prefit = prefit_score_from_profile(tender, profile_data)
    return {
        "fit_score": prefit["score"],
        "fit_band": "Fallback only",
        "fit_reasons": [f"Profile/tender overlap: {', '.join(prefit['reasons'])}"] if prefit["reasons"] else ["No strong overlap detected."],
        "risk_flags": ["Structured bid assessment was unavailable."],
        "competitiveness": "Uncertain",
        "execution_investment": "Medium",
        "strategic_readiness": ["Retry assessment once supplier and tender extraction are verified."],
        "analysis_ready": False,
        "decision_summary": "Fallback analysis only.",
        "bid_recommendation": "go_with_caution",
        "win_probability_band": "low",
        "improvement_actions": ["Retry the analysis", "Verify supplier profile parsing", "Verify tender PDF parsing"],
        "critical_unknowns": [],
        "qualification_status": "partially_qualifies",
        "confidence": 0.2,
    }


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


def filter_tenders(tenders: list[dict[str, Any]], province: str = "", tender_type: str = "", industry: str = "", date_from: str = "") -> list[dict[str, Any]]:
    province = province.strip().lower()
    tender_type = tender_type.strip().lower()
    industry = industry.strip().lower()
    date_from_dt = parse_iso_datetime(date_from) if date_from else None

    filtered = []
    for tender in tenders:
        if province:
            if province not in (tender.get("province") or "").lower():
                continue
        if tender_type:
            type_text = " ".join([str(tender.get("tender_type") or ""), str(tender.get("main_procurement_category") or "")]).lower()
            if tender_type not in type_text:
                continue
        if industry:
            industry_text = " ".join([str(tender.get("title") or ""), str(tender.get("description") or ""), str(tender.get("main_procurement_category") or ""), str(tender.get("tender_type") or "")]).lower()
            if industry not in industry_text:
                continue
        issue_dt = parse_iso_datetime(tender.get("issue_date"))
        if date_from_dt and issue_dt and issue_dt.date() < date_from_dt.date():
            continue
        filtered.append(tender)

    return filtered


def all_current_tender_records() -> list[dict[str, Any]]:
    items = fetch_all_current_tenders(max_pages=MAX_PAGES, page_size=100)
    return [normalize_tender_release(item) for item in items]


def find_tender_item(identifier: str) -> Optional[dict[str, Any]]:
    items = fetch_all_current_tenders(max_pages=MAX_PAGES, page_size=100)
    for item in items:
        tender = normalize_tender_release(item)
        if identifier in {str(tender.get("tender_id")), str(tender.get("ocid")), str(tender.get("release_id"))}:
            return item
    return None


def compute_insights(tenders: list[dict[str, Any]]) -> dict[str, Any]:
    province_counts = Counter((t.get("province") or "Unknown") for t in tenders)
    type_counts = Counter((t.get("tender_type") or t.get("main_procurement_category") or "Unknown") for t in tenders)
    urgent = sum(1 for t in tenders if t.get("days_left") is not None and t["days_left"] >= 0 and t["days_left"] <= 7)
    live = sum(1 for t in tenders if t.get("days_left") is None or t["days_left"] >= 0)
    return {
        "live_count": live,
        "urgent_count": urgent,
        "top_provinces": province_counts.most_common(4),
        "top_types": type_counts.most_common(4),
    }


def enrich_tender(item: dict[str, Any], profile: Optional[dict[str, Any]] = None, prompt: str = "", profile_id: Optional[str] = None) -> dict[str, Any]:
    tender = normalize_tender_release(item)
    best_doc = pick_best_tender_document(tender.get("documents", []))
    best_doc_url = None
    if best_doc:
        best_doc_url = sanitize_document_url(best_doc.get("url")) or sanitize_document_url(best_doc.get("downloadUrl")) or sanitize_document_url(best_doc.get("uri")) or sanitize_document_url(best_doc.get("href"))

    parsed_doc = normalize_tender_extraction(parse_tender_document_text_heuristic(""), tender=tender)
    if best_doc_url:
        parsed_text = download_pdf_text_from_url(best_doc_url)
        if parsed_text:
            parsed_doc = parse_tender_document_text(parsed_text, tender=tender)

    fit = score_fit(tender, parsed_doc, profile, prompt, profile_id=profile_id)
    tender["document_url"] = best_doc_url
    tender["document_title"] = (best_doc or {}).get("title")
    tender["parsed_document"] = parsed_doc
    tender["document_parser_confidence"] = parsed_doc.get("confidence")
    tender["analysis"] = fit
    tender["proposal_required"] = parsed_doc.get("submission", {}).get("proposal_required")
    return tender


def run_analysis_job(job_id: str, tender_id: str, profile_id: str, prompt: str = ""):
    try:
        with db_session() as session:
            job = session.get(AnalysisJob, job_id)
            if not job:
                return
            job.status = "running"
            session.commit()

        profile_data = get_profile_data(profile_id)
        if not profile_data:
            raise RuntimeError("Selected profile not found")

        matched = find_tender_item(tender_id)
        if not matched:
            raise RuntimeError("Tender not found")

        enriched = enrich_tender(matched, profile=profile_data, prompt=prompt, profile_id=profile_id)
        upsert_profile_issues(profile_id, tender_id, enriched.get("analysis", {}).get("risk_flags", []))

        with db_session() as session:
            job = session.get(AnalysisJob, job_id)
            if not job:
                return
            job.status = "completed"
            job.result_json = json.dumps(enriched)
            session.commit()

    except Exception as exc:
        traceback.print_exc()
        with db_session() as session:
            job = session.get(AnalysisJob, job_id)
            if not job:
                return
            job.status = "failed"
            job.error_text = str(exc)
            session.commit()


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
            "issues": get_profile_issues(record.id),
        }
    )
    return summary


def build_proposal_docx_bytes(title: str, company_name: str, proposal_text: str) -> io.BytesIO:
    doc = Document()
    styles = doc.styles
    styles["Normal"].font.name = "Calibri"
    styles["Normal"].font.size = Pt(11)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("TenderAI Proposal Draft")
    run.bold = True
    run.font.size = Pt(18)

    p2 = doc.add_paragraph()
    p2.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p2.add_run(title or "Tender Proposal Draft").bold = True

    p3 = doc.add_paragraph()
    p3.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p3.add_run(company_name or "Supplier")

    doc.add_paragraph("")

    for block in proposal_text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        if len(block) < 90 and block.endswith(":"):
            para = doc.add_paragraph()
            run = para.add_run(block)
            run.bold = True
        else:
            doc.add_paragraph(block)

    bio = io.BytesIO()
    doc.save(bio)
    bio.seek(0)
    return bio


@app.get("/")
def home():
    tenders = []
    active_profile = get_profile_summary(None)
    try:
        tenders = all_current_tender_records()[:8]
    except Exception:
        traceback.print_exc()
    return render_template("home.html", tenders=tenders, active_profile=active_profile)


@app.get("/profiles")
def profiles_page():
    profiles = []
    try:
        with db_session() as session:
            records = session.scalars(select(Profile).order_by(Profile.is_active.desc(), Profile.uploaded_at.desc())).all()
    except Exception:
        traceback.print_exc()
        records = []

    for profile in records:
        summary = json_loads_safe(profile.summary_json, {})
        summary.update(
            {
                "id": profile.id,
                "file_name": profile.file_name,
                "company_name": profile.company_name,
                "is_active": profile.is_active,
                "uploaded_at": profile.uploaded_at.isoformat(),
                "issues": get_profile_issues(profile.id),
            }
        )
        profiles.append(summary)

    active_profile = next((p for p in profiles if p.get("is_active")), None)
    return render_template("profiles.html", profiles=profiles, active_profile=active_profile)


@app.get("/tenders")
def tenders_page():
    prompt = request.args.get("prompt", "").strip()
    profile_id = request.args.get("profile_id", "").strip() or None
    province = request.args.get("province", "").strip()
    tender_type = request.args.get("tender_type", "").strip()
    industry = request.args.get("industry", "").strip()
    date_from = request.args.get("date_from", "")

    active_profile = get_profile_summary(profile_id)
    profile_data = get_profile_data(profile_id)
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


@app.get("/debug/ai-status")
def debug_ai_status():
    payload = dict(AI_TELEMETRY)
    payload["recent_events"] = list(AI_TELEMETRY["recent_events"])
    return render_json_response(payload)


@app.get("/api/profiles")
def api_profiles_list():
    try:
        with db_session() as session:
            profiles = session.scalars(select(Profile).order_by(Profile.is_active.desc(), Profile.uploaded_at.desc())).all()
    except Exception as exc:
        return render_json_response({"error": str(exc)}, 500)

    payload = []
    for profile in profiles:
        summary = json_loads_safe(profile.summary_json, {})
        summary.update(
            {
                "id": profile.id,
                "file_name": profile.file_name,
                "is_active": profile.is_active,
                "uploaded_at": profile.uploaded_at.isoformat(),
                "company_name": profile.company_name,
                "issues": get_profile_issues(profile.id),
            }
        )
        payload.append(summary)
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


@app.get("/api/profile-issues/<profile_id>")
def api_profile_issues(profile_id: str):
    if not get_profile_record(profile_id):
        return render_json_response({"error": "Profile not found"}, 404)
    return render_json_response(get_profile_issues(profile_id))


@app.post("/api/profile-issues/<profile_id>/<int:issue_id>")
def api_update_profile_issue(profile_id: str, issue_id: int):
    payload = request.get_json(silent=True) or {}
    new_status = (payload.get("status") or "").strip().lower()
    if new_status not in {"fixed", "pending"}:
        return render_json_response({"error": "status must be fixed or pending"}, 400)

    try:
        with db_session() as session:
            issue = session.get(ProfileIssue, issue_id)
            if not issue or issue.profile_id != profile_id:
                return render_json_response({"error": "Issue not found"}, 404)
            issue.status = new_status
            issue.updated_at = datetime.now(timezone.utc)
            session.commit()
    except Exception as exc:
        traceback.print_exc()
        return render_json_response({"error": str(exc)}, 500)

    return render_json_response({"status": "ok", "issue_id": issue_id, "new_status": new_status})


@app.get("/api/tenders")
def api_tenders():
    province = request.args.get("province", "")
    tender_type = request.args.get("tender_type", "")
    industry = request.args.get("industry", "")
    date_from = request.args.get("date_from", "")
    tenders = all_current_tender_records()
    tenders = filter_tenders(tenders, province=province, tender_type=tender_type, industry=industry, date_from=date_from)
    return render_json_response(tenders)


@app.get("/api/tender/<path:tender_id>")
def api_tender_detail(tender_id: str):
    matched = find_tender_item(tender_id)
    if not matched:
        return render_json_response({"error": "Tender not found"}, 404)
    return render_json_response(normalize_tender_release(matched))


@app.post("/api/analyze-tender")
def api_analyze_tender():
    payload = request.get_json(silent=True) or {}
    tender_id = (payload.get("tender_id") or "").strip()
    profile_id = (payload.get("profile_id") or "").strip()
    prompt = (payload.get("prompt") or "").strip()

    if not tender_id:
        return render_json_response({"error": "tender_id is required"}, 400)
    if not profile_id:
        return render_json_response({"error": "profile_id is required"}, 400)
    if not get_profile_data(profile_id):
        return render_json_response({"error": "Selected profile not found or not active"}, 404)

    job_id = str(uuid.uuid4())

    try:
        with db_session() as session:
            session.add(AnalysisJob(id=job_id, tender_id=tender_id, profile_id=profile_id, status="queued"))
            session.commit()

        thread = threading.Thread(target=run_analysis_job, args=(job_id, tender_id, profile_id, prompt), daemon=True)
        thread.start()

        return render_json_response({"job_id": job_id, "status": "queued"}, 202)
    except Exception as exc:
        traceback.print_exc()
        return render_json_response({"error": str(exc)}, 500)


@app.get("/api/analyze-status/<job_id>")
def api_analyze_status(job_id: str):
    try:
        with db_session() as session:
            job = session.get(AnalysisJob, job_id)
            if not job:
                return render_json_response({"error": "Job not found"}, 404)
            return render_json_response(
                {
                    "job_id": job.id,
                    "status": job.status,
                    "error": job.error_text,
                    "result": json_loads_safe(job.result_json, None),
                }
            )
    except Exception as exc:
        traceback.print_exc()
        return render_json_response({"error": str(exc)}, 500)


@app.post("/api/write-proposal-docx")
def api_write_proposal_docx():
    payload = request.get_json(silent=True) or {}
    tender_id = (payload.get("tender_id") or "").strip()
    profile_id = (payload.get("profile_id") or "").strip()

    if not tender_id or not profile_id:
        return render_json_response({"error": "tender_id and profile_id are required"}, 400)

    profile_data = get_profile_data(profile_id)
    profile_summary = get_profile_summary(profile_id)
    if not profile_data:
        return render_json_response({"error": "Profile not found"}, 404)

    matched = find_tender_item(tender_id)
    if not matched:
        return render_json_response({"error": "Tender not found"}, 404)

    enriched = enrich_tender(matched, profile=profile_data, profile_id=profile_id)
    proposal_text = llm_write_proposal(
        profile_data,
        enriched.get("parsed_document", {}),
        enriched.get("analysis", {}),
    )

    if not proposal_text:
        return render_json_response({"error": "Proposal draft could not be generated"}, 500)

    company_name = (profile_summary or {}).get("company_name") or "Supplier"
    filename = secure_filename(f"{company_name}_{tender_id}_proposal_draft.docx")
    fileobj = build_proposal_docx_bytes(enriched.get("title") or "Tender Proposal Draft", company_name, proposal_text)

    return send_file(
        fileobj,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
