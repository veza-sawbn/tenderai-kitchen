import io
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import fitz
import pdfplumber
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import JSON, Column, DateTime, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

app = Flask(__name__)

PORT = int(os.getenv("PORT", "8080"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
ETENDERS_API_URL = os.getenv(
    "ETENDERS_API_URL",
    "https://ocds-api.etenders.gov.za/api/OCDSReleases",
)

# Production:
# DATABASE_URL=postgresql+psycopg://user:password@host:5432/tenderai
# Local fallback:
DATABASE_URL = os.getenv("DATABASE_URL", "").strip() or "sqlite:///tenderai.db"

engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()

http = requests.Session()
http.headers.update({"User-Agent": "TenderAI/1.0"})
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


# ----------------------------
# Database models
# ----------------------------
class SupplierProfile(Base):
    __tablename__ = "supplier_profiles"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=True)
    filename = Column(String(255), nullable=True)
    raw_text = Column(Text, nullable=True)
    parsed_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class TenderCache(Base):
    __tablename__ = "tender_cache"

    id = Column(Integer, primary_key=True)
    tender_ocid = Column(String(255), index=True, nullable=True)
    title = Column(String(500), nullable=True)
    buyer_name = Column(String(255), nullable=True)
    source_json = Column(JSON, nullable=True)
    parsed_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(bind=engine)


# ----------------------------
# Structured schemas
# ----------------------------
class SupplierCapability(BaseModel):
    category: str
    description: str
    confidence: float = Field(ge=0, le=1)


class ComplianceRecord(BaseModel):
    tax_pin_present: bool = False
    vat_number_present: bool = False
    ck_cipc_present: bool = False
    bbbee_level: Optional[str] = None
    cidb_grade: Optional[str] = None
    sars_compliant_reference_present: bool = False
    csd_number_present: bool = False


class SupplierProfileParsed(BaseModel):
    legal_name: Optional[str] = None
    trading_name: Optional[str] = None
    registration_number: Optional[str] = None
    vat_number: Optional[str] = None
    csd_number: Optional[str] = None
    province: Optional[str] = None
    municipality: Optional[str] = None
    industry_tags: List[str] = []
    capabilities: List[SupplierCapability] = []
    certifications: List[str] = []
    compliance: ComplianceRecord
    past_performance_signals: List[str] = []
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    confidence: float = Field(ge=0, le=1)


class TenderDocumentParsed(BaseModel):
    tender_number: Optional[str] = None
    title: Optional[str] = None
    buyer_name: Optional[str] = None
    scope_summary: str = ""
    required_capabilities: List[str] = []
    mandatory_documents: List[str] = []
    compliance_requirements: List[str] = []
    technical_evaluation_criteria: List[str] = []
    functionality_criteria: List[str] = []
    price_preference_system: Optional[str] = None
    preferential_goals: List[str] = []
    briefing_required: bool = False
    briefing_date_text: Optional[str] = None
    briefing_compulsory: Optional[bool] = None
    submission_deadline_text: Optional[str] = None
    risk_flags: List[str] = []
    source_document_name: Optional[str] = None
    source_pages: List[int] = []
    confidence: float = Field(ge=0, le=1)


# ----------------------------
# PDF helpers
# ----------------------------
def normalize_pdf_text(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_text_pymupdf(pdf_bytes: bytes) -> str:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = []
    for i, page in enumerate(doc):
        pages.append(f"\n\n--- PAGE {i + 1} ---\n{page.get_text('text')}")
    return normalize_pdf_text("\n".join(pages))


def extract_text_pdfplumber(pdf_bytes: bytes) -> str:
    pages = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            pages.append(f"\n\n--- PAGE {i + 1} ---\n{text}")
    return normalize_pdf_text("\n".join(pages))


def extract_pdf_text(pdf_bytes: bytes) -> str:
    try:
        text = extract_text_pymupdf(pdf_bytes)
        if text.strip():
            return text
    except Exception:
        pass

    try:
        text = extract_text_pdfplumber(pdf_bytes)
        if text.strip():
            return text
    except Exception:
        pass

    return ""


# ----------------------------
# HTTP / document helpers
# ----------------------------
DOC_KEYWORDS_HIGH = [
    "terms of reference",
    "tor",
    "specification",
    "scope of work",
    "bid document",
    "rfq",
    "rfp",
    "tender document",
    "statement of work",
    "pricing schedule",
    "evaluation criteria",
]

DOC_KEYWORDS_LOW = [
    "advert",
    "notice",
    "invitation",
    "cover",
    "gazette",
    "award",
]


def score_document(doc: Dict[str, Any]) -> int:
    name = (
        f"{doc.get('title', '')} {doc.get('description', '')} {doc.get('documentType', '')}"
    ).lower()

    score = 0
    for keyword in DOC_KEYWORDS_HIGH:
        if keyword in name:
            score += 10
    for keyword in DOC_KEYWORDS_LOW:
        if keyword in name:
            score -= 5

    url = (doc.get("url") or "").lower()
    if url.endswith(".pdf"):
        score += 3

    return score


def download_pdf(url: str) -> Optional[bytes]:
    try:
        response = http.get(url, timeout=45)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        if "pdf" not in content_type and not url.lower().endswith(".pdf"):
            return None
        return response.content
    except Exception:
        return None


def fetch_tenders_page(page_number: int = 1, page_size: int = 25) -> Dict[str, Any]:
    params = {"PageNumber": page_number, "PageSize": page_size}
    response = http.get(ETENDERS_API_URL, params=params, timeout=45)
    response.raise_for_status()
    return response.json()


# ----------------------------
# Fallback parsers
# ----------------------------
def heuristic_profile_fallback(text: str) -> Dict[str, Any]:
    lower = text.lower()
    return {
        "legal_name": None,
        "trading_name": None,
        "registration_number": None,
        "vat_number": None,
        "csd_number": None,
        "province": None,
        "municipality": None,
        "industry_tags": [],
        "capabilities": [],
        "certifications": [],
        "compliance": {
            "tax_pin_present": "tax" in lower,
            "vat_number_present": "vat" in lower,
            "ck_cipc_present": "cipc" in lower or "ck" in lower,
            "bbbee_level": None,
            "cidb_grade": None,
            "sars_compliant_reference_present": "sars" in lower,
            "csd_number_present": "csd" in lower,
        },
        "past_performance_signals": [],
        "contact_email": None,
        "contact_phone": None,
        "confidence": 0.25,
    }


def heuristic_tender_fallback(text: str) -> Dict[str, Any]:
    lower = text.lower()
    return {
        "tender_number": None,
        "title": None,
        "buyer_name": None,
        "scope_summary": text[:1500],
        "required_capabilities": [],
        "mandatory_documents": [],
        "compliance_requirements": [],
        "technical_evaluation_criteria": [],
        "functionality_criteria": [],
        "price_preference_system": "80/20" if "80/20" in text else ("90/10" if "90/10" in text else None),
        "preferential_goals": [],
        "briefing_required": "briefing" in lower,
        "briefing_date_text": None,
        "briefing_compulsory": True if "compulsory briefing" in lower else None,
        "submission_deadline_text": None,
        "risk_flags": [],
        "source_document_name": None,
        "source_pages": [],
        "confidence": 0.30,
    }


# ----------------------------
# OpenAI structured parsers
# ----------------------------
def parse_profile_with_openai(text: str) -> Dict[str, Any]:
    if not client:
        return heuristic_profile_fallback(text)

    prompt = f"""
Extract structured data from this South African supplier/CSD-style profile PDF.

Rules:
- Return only schema fields.
- If a value is unknown, return null or empty list.
- Do not invent registrations, VAT, CSD, CIDB, B-BBEE, or contacts.
- Capabilities must reflect actual business activities found in the document.
- confidence must reflect extraction certainty from the source text.

Text:
{text[:120000]}
"""

    try:
        response = client.responses.parse(
            model="gpt-4.1",
            input=prompt,
            text_format=SupplierProfileParsed,
        )
        return response.output_parsed.model_dump()
    except (ValidationError, Exception):
        return heuristic_profile_fallback(text)


def parse_tender_with_openai(text: str, source_document_name: Optional[str] = None) -> Dict[str, Any]:
    if not client:
        data = heuristic_tender_fallback(text)
        data["source_document_name"] = source_document_name
        return data

    prompt = f"""
Extract procurement-relevant information from this South African tender document.

Rules:
- Return only schema fields.
- If unknown, return null or empty list.
- Extract explicit scoring, functionality, briefing, compliance, and required capability cues.
- Identify 80/20 or 90/10 only if explicitly present.
- Include risk_flags when requirements appear strict, missing, ambiguous, or highly compliance-heavy.
- confidence must reflect extraction certainty.

Text:
{text[:120000]}
"""

    try:
        response = client.responses.parse(
            model="gpt-4.1",
            input=prompt,
            text_format=TenderDocumentParsed,
        )
        parsed = response.output_parsed.model_dump()
        parsed["source_document_name"] = source_document_name
        return parsed
    except (ValidationError, Exception):
        data = heuristic_tender_fallback(text)
        data["source_document_name"] = source_document_name
        return data


# ----------------------------
# Tender aggregation helpers
# ----------------------------
def merge_tender_parses(primary: Dict[str, Any], secondary: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(primary)

    for key, value in secondary.items():
        if key not in merged or merged[key] in (None, "", [], False):
            merged[key] = value
            continue

        if isinstance(merged[key], list) and isinstance(value, list):
            existing = set(str(v) for v in merged[key])
            merged[key].extend([v for v in value if str(v) not in existing])

    return merged


def extract_tender_summary(release: Dict[str, Any]) -> Dict[str, Any]:
    tender = release.get("tender", {}) or {}
    buyer = release.get("buyer", {}) or {}

    return {
        "ocid": release.get("ocid") or tender.get("id"),
        "title": tender.get("title"),
        "description": tender.get("description"),
        "buyer_name": buyer.get("name"),
        "documents": tender.get("documents", []) or [],
        "eligibilityCriteria": tender.get("eligibilityCriteria"),
        "specialConditions": tender.get("specialConditions"),
        "selectionCriteria": tender.get("selectionCriteria"),
        "briefingSession": tender.get("briefingSession"),
    }


def parse_best_tender_documents(tender_summary: Dict[str, Any]) -> Dict[str, Any]:
    docs = tender_summary.get("documents", []) or []
    ranked_docs = sorted(docs, key=score_document, reverse=True)[:3]

    aggregate = {
        "tender_number": tender_summary.get("ocid"),
        "title": tender_summary.get("title"),
        "buyer_name": tender_summary.get("buyer_name"),
        "scope_summary": tender_summary.get("description") or "",
        "required_capabilities": [],
        "mandatory_documents": [],
        "compliance_requirements": [],
        "technical_evaluation_criteria": [],
        "functionality_criteria": [],
        "price_preference_system": None,
        "preferential_goals": [],
        "briefing_required": bool(tender_summary.get("briefingSession")),
        "briefing_date_text": None,
        "briefing_compulsory": None,
        "submission_deadline_text": None,
        "risk_flags": [],
        "source_document_name": None,
        "source_pages": [],
        "confidence": 0.20,
    }

    for doc in ranked_docs:
        url = doc.get("url")
        title = doc.get("title") or doc.get("documentType") or "Tender document"
        if not url:
            continue

        pdf_bytes = download_pdf(url)
        if not pdf_bytes:
            continue

        text = extract_pdf_text(pdf_bytes)
        if not text.strip():
            continue

        parsed = parse_tender_with_openai(text, source_document_name=title)
        aggregate = merge_tender_parses(aggregate, parsed)

    return aggregate


def find_tender_by_id(tender_id: str) -> Optional[Dict[str, Any]]:
    # Searches first page set only. Increase pages later if needed.
    payload = fetch_tenders_page(page_number=1, page_size=100)
    releases = payload.get("releases") or payload.get("value") or payload.get("data") or []

    for release in releases:
        summary = extract_tender_summary(release)
        if str(summary.get("ocid")) == str(tender_id):
            return summary
    return None


# ----------------------------
# Scoring
# ----------------------------
def compute_fit_score(profile: Dict[str, Any], tender: Dict[str, Any]) -> Dict[str, Any]:
    capability_score = 0
    compliance_score = 0
    experience_score = 0
    competitiveness_score = 0
    effort_score = 0
    readiness_score = 0
    risk_flags: List[str] = []

    profile_caps = " ".join(
        [
            c.get("description", "") if isinstance(c, dict) else str(c)
            for c in profile.get("capabilities", [])
        ]
    ).lower()

    required_caps = " ".join(tender.get("required_capabilities", [])).lower()

    if required_caps and profile_caps:
        required_tokens = [t for t in re.split(r"[\s,;/]+", required_caps) if len(t) > 3]
        hits = sum(1 for token in required_tokens if token in profile_caps)
        if hits >= 4:
            capability_score = 35
        elif hits >= 2:
            capability_score = 25
        elif hits >= 1:
            capability_score = 18
        else:
            capability_score = 10
    elif profile_caps:
        capability_score = 15

    compliance = profile.get("compliance", {}) or {}
    if compliance.get("tax_pin_present"):
        compliance_score += 5
    if compliance.get("csd_number_present"):
        compliance_score += 5
    if compliance.get("vat_number_present"):
        compliance_score += 5
    if compliance.get("bbbee_level"):
        compliance_score += 5

    if profile.get("past_performance_signals"):
        experience_score = 10

    if tender.get("price_preference_system") in ("80/20", "90/10"):
        competitiveness_score = 10
    else:
        competitiveness_score = 5

    mandatory_count = len(tender.get("mandatory_documents", []))
    effort_score = max(0, 10 - min(10, mandatory_count))

    if compliance_score >= 15 and capability_score >= 20:
        readiness_score = 15
    elif compliance_score >= 10:
        readiness_score = 10
    elif compliance_score >= 5:
        readiness_score = 6

    compliance_requirements = [str(x).lower() for x in tender.get("compliance_requirements", [])]
    functionality_criteria = tender.get("functionality_criteria", []) or []

    if tender.get("briefing_compulsory") and not tender.get("briefing_date_text"):
        risk_flags.append("Compulsory briefing indicated but date not clearly extracted")

    if any("cidb" in item for item in compliance_requirements) and not compliance.get("cidb_grade"):
        risk_flags.append("Tender may require CIDB but profile has no CIDB cue")

    if any("csd" in item for item in compliance_requirements) and not compliance.get("csd_number_present"):
        risk_flags.append("Tender appears to require CSD but profile has no CSD cue")

    if functionality_criteria and not profile.get("past_performance_signals"):
        risk_flags.append("Functionality criteria present but limited past performance evidence in profile")

    total = capability_score + compliance_score + experience_score + competitiveness_score + effort_score + readiness_score

    return {
        "fit_score": min(100, total),
        "competitiveness": competitiveness_score,
        "execution_effort": effort_score,
        "strategic_readiness": readiness_score,
        "risk_flags": risk_flags,
        "breakdown": {
            "capability_fit": capability_score,
            "compliance_readiness": compliance_score,
            "experience_signals": experience_score,
            "competitiveness": competitiveness_score,
            "execution_effort": effort_score,
            "strategic_readiness": readiness_score,
        },
    }


# ----------------------------
# Page routes
# ----------------------------
@app.get("/")
def home():
    return render_template("index.html")


@app.get("/profiles")
def profiles_page():
    return render_template("profiles.html")


@app.get("/tenders")
def tenders_page():
    return render_template("tenders.html")


@app.get("/tender/<tender_id>")
def tender_page(tender_id: str):
    profile_id = request.args.get("profile_id")
    return render_template("tender_detail.html", tender_id=tender_id, profile_id=profile_id)


# ----------------------------
# Health
# ----------------------------
@app.get("/health")
def health():
    return {"status": "ok"}, 200


# ----------------------------
# APIs
# ----------------------------
@app.route("/api/profiles", methods=["GET", "POST"])
def api_profiles():
    db = SessionLocal()
    try:
        if request.method == "GET":
            rows = db.query(SupplierProfile).order_by(SupplierProfile.created_at.desc()).all()
            return jsonify(
                [
                    {
                        "id": row.id,
                        "name": row.name,
                        "filename": row.filename,
                        "parsed_json": row.parsed_json,
                        "created_at": row.created_at.isoformat() if row.created_at else None,
                    }
                    for row in rows
                ]
            )

        uploaded = request.files.get("file")
        if not uploaded:
            return jsonify({"error": "No file uploaded"}), 400

        pdf_bytes = uploaded.read()
        if not pdf_bytes:
            return jsonify({"error": "Empty upload"}), 400

        text = extract_pdf_text(pdf_bytes)
        parsed = parse_profile_with_openai(text)

        profile = SupplierProfile(
            name=parsed.get("legal_name") or uploaded.filename,
            filename=uploaded.filename,
            raw_text=text,
            parsed_json=parsed,
        )
        db.add(profile)
        db.commit()
        db.refresh(profile)

        return (
            jsonify(
                {
                    "id": profile.id,
                    "name": profile.name,
                    "filename": profile.filename,
                    "parsed_json": profile.parsed_json,
                }
            ),
            201,
        )
    except Exception as exc:
        db.rollback()
        return jsonify({"error": f"Profile processing failed: {str(exc)}"}), 500
    finally:
        db.close()


@app.get("/api/tenders")
def api_tenders():
    try:
        page = int(request.args.get("page", 1))
        payload = fetch_tenders_page(page_number=page, page_size=25)

        releases = payload.get("releases") or payload.get("value") or payload.get("data") or []
        items = [extract_tender_summary(release) for release in releases]

        return jsonify(items)
    except Exception as exc:
        return jsonify({"error": f"Failed to fetch tenders: {str(exc)}"}), 500


@app.get("/api/tender/<tender_id>")
def api_tender_detail(tender_id: str):
    db = SessionLocal()
    try:
        cached = db.query(TenderCache).filter(TenderCache.tender_ocid == str(tender_id)).first()
        if cached:
            return jsonify(
                {
                    "summary": cached.source_json or {},
                    "parsed": cached.parsed_json or {},
                }
            )

        target = find_tender_by_id(tender_id)
        if not target:
            return jsonify({"error": "Tender not found"}), 404

        parsed = parse_best_tender_documents(target)

        cache_row = TenderCache(
            tender_ocid=str(tender_id),
            title=target.get("title"),
            buyer_name=target.get("buyer_name"),
            source_json=target,
            parsed_json=parsed,
        )
        db.add(cache_row)
        db.commit()

        return jsonify({"summary": target, "parsed": parsed})
    except Exception as exc:
        db.rollback()
        return jsonify({"error": f"Failed to load tender detail: {str(exc)}"}), 500
    finally:
        db.close()


@app.post("/api/score")
def api_score():
    db = SessionLocal()
    try:
        payload = request.get_json(force=True)
        profile_id = payload.get("profile_id")
        tender_id = payload.get("tender_id")

        if not profile_id:
            return jsonify({"error": "profile_id is required"}), 400
        if not tender_id:
            return jsonify({"error": "tender_id is required"}), 400

        profile = db.query(SupplierProfile).filter(SupplierProfile.id == int(profile_id)).first()
        if not profile:
            return jsonify({"error": "Profile not found"}), 404

        cached = db.query(TenderCache).filter(TenderCache.tender_ocid == str(tender_id)).first()

        if cached:
            tender_summary = cached.source_json or {}
            parsed_tender = cached.parsed_json or {}
        else:
            target = find_tender_by_id(str(tender_id))
            if not target:
                return jsonify({"error": "Tender not found"}), 404

            parsed_tender = parse_best_tender_documents(target)
            tender_summary = target

            cache_row = TenderCache(
                tender_ocid=str(tender_id),
                title=target.get("title"),
                buyer_name=target.get("buyer_name"),
                source_json=target,
                parsed_json=parsed_tender,
            )
            db.add(cache_row)
            db.commit()

        result = compute_fit_score(profile.parsed_json or {}, parsed_tender)

        return jsonify(
            {
                "profile_id": profile.id,
                "tender_id": tender_id,
                "result": result,
                "tender": {
                    "summary": tender_summary,
                    "parsed": parsed_tender,
                },
            }
        )
    except Exception as exc:
        db.rollback()
        return jsonify({"error": f"Scoring failed: {str(exc)}"}), 500
    finally:
        db.close()


@app.post("/api/advise")
def api_advise():
    try:
        payload = request.get_json(force=True)
        profile = payload.get("profile") or {}
        tender = payload.get("tender") or {}
        score = payload.get("score") or {}

        if client:
            prompt = f"""
You are TenderAI, a South African procurement intelligence assistant.

Given the supplier profile, tender parse, and score result below, produce concise advice in JSON with:
- recommendation: one of ["go", "go_with_caution", "no_go"]
- rationale: short paragraph
- next_actions: array of action strings
- key_risks: array of risk strings

Supplier Profile:
{profile}

Tender:
{tender}

Score:
{score}
"""
            response = client.responses.create(
                model="gpt-4.1",
                input=prompt,
            )
            text = response.output_text.strip()
            return jsonify({"advice": text})

        fit = score.get("fit_score", 0)
        recommendation = "go" if fit >= 70 else ("go_with_caution" if fit >= 45 else "no_go")

        return jsonify(
            {
                "advice": {
                    "recommendation": recommendation,
                    "rationale": "Fallback advice generated without OpenAI.",
                    "next_actions": [
                        "Review mandatory documents",
                        "Confirm compliance readiness",
                        "Validate scope against supplier capability",
                    ],
                    "key_risks": score.get("risk_flags", []),
                }
            }
        )
    except Exception as exc:
        return jsonify({"error": f"Advice generation failed: {str(exc)}"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=True)
