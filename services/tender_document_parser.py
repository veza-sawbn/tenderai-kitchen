from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select, text

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

from models import TenderCache, TenderDocumentCache

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
OPENAI_TIMEOUT_SECONDS = int(os.getenv("OPENAI_TIMEOUT_SECONDS", "45"))

PARSE_CHUNK_CHARS = int(os.getenv("TENDER_PARSE_CHUNK_CHARS", "1800"))
PARSE_MAX_CHUNKS = int(os.getenv("TENDER_PARSE_MAX_CHUNKS", "10"))
PARSE_CHUNK_MAX_TOKENS = int(os.getenv("TENDER_PARSE_CHUNK_MAX_TOKENS", "700"))
PARSE_CONSOLIDATE_MAX_TOKENS = int(os.getenv("TENDER_PARSE_CONSOLIDATE_MAX_TOKENS", "900"))


def utcnow():
    return datetime.now(timezone.utc)


def get_openai_client():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    if OpenAI is None:
        raise RuntimeError("The openai Python package is not installed or could not be imported.")
    return OpenAI(api_key=api_key, timeout=OPENAI_TIMEOUT_SECONDS)


def ensure_parse_table(session):
    session.execute(text("""
        CREATE TABLE IF NOT EXISTS tender_document_parsed_cache (
            id SERIAL PRIMARY KEY,
            tender_id INTEGER NOT NULL REFERENCES tender_cache(id) ON DELETE CASCADE,
            document_id INTEGER REFERENCES tender_document_cache(id) ON DELETE SET NULL,
            document_hash VARCHAR(64) NOT NULL,
            parse_status VARCHAR(50) NOT NULL DEFAULT 'pending',
            parsed_json TEXT,
            source_text_chars INTEGER DEFAULT 0,
            source_text_words INTEGER DEFAULT 0,
            chunk_count INTEGER DEFAULT 0,
            error_message TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_tender_document_parsed_cache_tender_id
        ON tender_document_parsed_cache(tender_id)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_tender_document_parsed_cache_document_hash
        ON tender_document_parsed_cache(document_hash)
    """))
    session.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_tender_document_parsed_cache_parse_status
        ON tender_document_parsed_cache(parse_status)
    """))


def safe_json_loads(value: Any, fallback=None):
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


def extract_json_object(value: str) -> Dict[str, Any]:
    if not value:
        raise ValueError("OpenAI returned an empty response.")
    value = value.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?", "", value, flags=re.I).strip()
        value = re.sub(r"```$", "", value).strip()
    try:
        return json.loads(value)
    except Exception:
        start = value.find("{")
        end = value.rfind("}")
        if start >= 0 and end > start:
            return json.loads(value[start:end + 1])
        raise


def openai_json(client, system_prompt: str, payload: Dict[str, Any], max_tokens: int = 700) -> Dict[str, Any]:
    last_error = None
    for attempt in range(2):
        token_budget = max_tokens if attempt == 0 else min(max_tokens + 400, 1600)
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=token_budget,
        )
        choice = response.choices[0]
        content = choice.message.content or ""
        try:
            return extract_json_object(content)
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                continue
    raise RuntimeError(f"OpenAI returned malformed JSON after retry: {last_error}")


def latest_document(session, tender_id: int) -> Optional[TenderDocumentCache]:
    return session.execute(
        select(TenderDocumentCache)
        .where(TenderDocumentCache.tender_id == tender_id)
        .order_by(desc(TenderDocumentCache.fetched_at), desc(TenderDocumentCache.id))
        .limit(1)
    ).scalars().first()


def document_hash(text_value: str) -> str:
    return hashlib.sha256((text_value or "").encode("utf-8", errors="ignore")).hexdigest()


def split_text(text_value: str, chunk_chars: int = PARSE_CHUNK_CHARS, max_chunks: int = PARSE_MAX_CHUNKS) -> List[str]:
    text_value = (text_value or "").strip()
    if not text_value:
        return []

    chunks = []
    pos = 0
    while pos < len(text_value) and len(chunks) < max_chunks:
        chunk = text_value[pos:pos + chunk_chars]
        if pos + chunk_chars < len(text_value):
            cut = max(chunk.rfind(". "), chunk.rfind("\n"), chunk.rfind("; "))
            if cut > int(chunk_chars * 0.55):
                chunk = chunk[:cut + 1]
        chunk = chunk.strip()
        if chunk:
            chunks.append(chunk)
        pos += max(len(chunk), chunk_chars)
    return chunks


def as_list(value: Any, limit: int = 20) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    output = []
    seen = set()
    for item in value:
        if item is None:
            continue
        s = str(item).strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(s)
    return output[:limit]


def get_latest_parsed_document(session, tender_id: int) -> Optional[Dict[str, Any]]:
    ensure_parse_table(session)
    row = session.execute(text("""
        SELECT *
        FROM tender_document_parsed_cache
        WHERE tender_id = :tender_id
          AND parse_status = 'parsed'
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
    """), {"tender_id": tender_id}).mappings().first()

    if not row:
        return None

    return {
        "id": row.get("id"),
        "tender_id": row.get("tender_id"),
        "document_id": row.get("document_id"),
        "document_hash": row.get("document_hash"),
        "parse_status": row.get("parse_status"),
        "parsed_json": safe_json_loads(row.get("parsed_json"), {}),
        "source_text_chars": row.get("source_text_chars"),
        "source_text_words": row.get("source_text_words"),
        "chunk_count": row.get("chunk_count"),
        "updated_at": row.get("updated_at").isoformat() if row.get("updated_at") else None,
    }


def create_parse_job(session, tender: TenderCache, force: bool = False) -> Dict[str, Any]:
    """
    Queue a parse job only. This route does not call OpenAI and should not timeout.
    A separate worker processes one chunk per call/run.
    """
    ensure_parse_table(session)

    doc = latest_document(session, tender.id)
    if not doc:
        return {"ok": False, "status": "no_document", "tender_id": tender.id, "error": "No fetched document found. Run fetch-documents first."}

    source_text = (doc.extracted_text or "").strip()
    if len(source_text) < 250:
        return {
            "ok": False,
            "status": "no_usable_text",
            "tender_id": tender.id,
            "document_id": doc.id,
            "fetch_status": doc.fetch_status,
            "source_text_chars": len(source_text),
            "error": "Fetched document has too little readable text for parsing.",
        }

    doc_hash = document_hash(source_text)
    chunks = split_text(source_text)

    ready = session.execute(text("""
        SELECT id
        FROM tender_document_parsed_cache
        WHERE tender_id = :tender_id
          AND document_hash = :document_hash
          AND parse_status = 'parsed'
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
    """), {"tender_id": tender.id, "document_hash": doc_hash}).mappings().first()

    if ready and not force:
        return {"ok": True, "status": "parsed", "tender_id": tender.id, "parsed_id": ready["id"], "next_action": "analysis_ready"}

    if force:
        session.execute(text("""
            UPDATE tender_document_parsed_cache
            SET parse_status = 'superseded',
                updated_at = NOW()
            WHERE tender_id = :tender_id
              AND document_hash = :document_hash
              AND parse_status IN ('pending', 'running', 'parsed', 'failed')
        """), {"tender_id": tender.id, "document_hash": doc_hash})
        session.flush()

    existing = session.execute(text("""
        SELECT *
        FROM tender_document_parsed_cache
        WHERE tender_id = :tender_id
          AND document_hash = :document_hash
          AND parse_status IN ('pending', 'running')
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
    """), {"tender_id": tender.id, "document_hash": doc_hash}).mappings().first()

    if existing:
        state = safe_json_loads(existing.get("parsed_json"), {})
        progress = state.get("_parse_progress") or {}
        return {
            "ok": True,
            "status": existing["parse_status"],
            "tender_id": tender.id,
            "parsed_id": existing["id"],
            "current_chunk_index": progress.get("current_chunk_index", 0),
            "total_chunks": progress.get("total_chunks", existing["chunk_count"]),
            "next_action": "/api/admin/parse-worker?limit=1",
        }

    initial_state = {
        "_parse_progress": {
            "mode": "queued_worker",
            "current_chunk_index": 0,
            "total_chunks": len(chunks),
            "chunks": [],
        }
    }

    row = session.execute(text("""
        INSERT INTO tender_document_parsed_cache
            (tender_id, document_id, document_hash, parse_status, parsed_json, source_text_chars, source_text_words, chunk_count, created_at, updated_at)
        VALUES
            (:tender_id, :document_id, :document_hash, 'pending', :parsed_json, :source_text_chars, :source_text_words, :chunk_count, NOW(), NOW())
        RETURNING id
    """), {
        "tender_id": tender.id,
        "document_id": doc.id,
        "document_hash": doc_hash,
        "parsed_json": json.dumps(initial_state, ensure_ascii=False),
        "source_text_chars": len(source_text),
        "source_text_words": len(re.findall(r"\w+", source_text)),
        "chunk_count": len(chunks),
    }).mappings().first()

    session.flush()
    return {
        "ok": True,
        "status": "queued",
        "tender_id": tender.id,
        "parsed_id": row["id"],
        "total_chunks": len(chunks),
        "source_text_chars": len(source_text),
        "next_action": "/api/admin/parse-worker?limit=1",
    }


def queue_live_tender_documents(session, limit: int = 10, force: bool = False) -> Dict[str, Any]:
    ensure_parse_table(session)

    limit = max(1, min(int(limit), 100))
    tenders = session.execute(
        select(TenderCache)
        .join(TenderDocumentCache, TenderDocumentCache.tender_id == TenderCache.id)
        .where(
            TenderCache.is_live.is_(True),
            TenderDocumentCache.extracted_text.is_not(None),
            TenderDocumentCache.fetch_status.in_(["fetched", "fetched_no_text"]),
        )
        .order_by(TenderCache.closing_date.asc().nulls_last(), desc(TenderDocumentCache.fetched_at))
        .limit(limit)
    ).scalars().all()

    results = []
    for tender in tenders:
        results.append(create_parse_job(session, tender, force=force))
        session.flush()

    return {
        "ok": True,
        "mode": "queue_only_no_openai",
        "limit": limit,
        "processed": len(results),
        "queued": sum(1 for r in results if r.get("status") in {"queued", "pending"}),
        "already_ready": sum(1 for r in results if r.get("status") == "parsed"),
        "items": results,
    }


def parse_chunk(client, tender: TenderCache, chunk: str, chunk_index: int, total_chunks: int) -> Dict[str, Any]:
    system_prompt = """
You are parsing one chunk of a South African tender document.

Extract only facts visible in this chunk. Do not guess.
Return valid JSON only. Keep each list short, max 5 items:
{
 "scope_items": [string],
 "requirements": [string],
 "compliance_documents": [string],
 "evaluation_criteria": [string],
 "submission_requirements": [string],
 "key_dates": [string],
 "contacts": {"emails": [string], "phones": [string]},
 "commercial_clues": [string],
 "risks": [string],
 "evidence_notes": [string]
}
""".strip()

    payload = {
        "tender_metadata": {
            "title": tender.title,
            "buyer_name": tender.buyer_name,
            "province": tender.province,
            "closing_date": tender.closing_date.isoformat() if tender.closing_date else None,
        },
        "chunk_index": chunk_index + 1,
        "total_chunks": total_chunks,
        "document_chunk": chunk,
    }
    return openai_json(client, system_prompt, payload, max_tokens=PARSE_CHUNK_MAX_TOKENS)


def regex_fallback_parse_chunk(chunk: str) -> Dict[str, Any]:
    text_value = re.sub(r"\s+", " ", (chunk or "")).strip()
    emails = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text_value)
    phones = re.findall(r"\+?\d[\d\s\-()]{7,}\d", text_value)
    dates = re.findall(r"\b(?:\d{4}[-/]\d{2}[-/]\d{2}|\d{2}[-/]\d{2}[-/]\d{4})\b", text_value)

    sentences = re.split(r"(?<=[.!?])\s+", text_value)
    scope_items, requirements, compliance, evaluation, submission, commercial, risks = [], [], [], [], [], [], []

    for sentence in sentences[:70]:
        s = sentence.strip()
        low = s.lower()
        if len(s) < 20:
            continue
        if any(k in low for k in ["scope", "supply", "deliver", "provide", "appointment", "service provider", "works"]):
            scope_items.append(s[:220])
        if any(k in low for k in ["must", "required", "shall", "compulsory", "mandatory"]):
            requirements.append(s[:220])
        if any(k in low for k in ["tax clearance", "csd", "b-bbee", "bbbee", "cidb", "certificate", "declaration", "sbd"]):
            compliance.append(s[:220])
        if any(k in low for k in ["evaluation", "functionality", "points", "price", "preference", "scoring"]):
            evaluation.append(s[:220])
        if any(k in low for k in ["submit", "submission", "closing", "tender box", "email"]):
            submission.append(s[:220])
        if any(k in low for k in ["pricing", "price", "amount", "rate", "bill", "boq", "quotation"]):
            commercial.append(s[:220])
        if any(k in low for k in ["disqual", "non-responsive", "invalid", "late", "failure"]):
            risks.append(s[:220])

    return {
        "scope_items": as_list(scope_items, 5),
        "requirements": as_list(requirements, 5),
        "compliance_documents": as_list(compliance, 5),
        "evaluation_criteria": as_list(evaluation, 5),
        "submission_requirements": as_list(submission, 5),
        "key_dates": as_list(dates, 5),
        "contacts": {"emails": as_list(emails, 5), "phones": as_list(phones, 5)},
        "commercial_clues": as_list(commercial, 5),
        "risks": as_list(risks, 5),
        "evidence_notes": as_list(sentences[:5], 5),
        "_parser": "regex_fallback",
    }


def combine_locally(parsed_chunks: List[Dict[str, Any]]) -> Dict[str, Any]:
    combined = {
        "scope_items": [],
        "deliverables": [],
        "mandatory_requirements": [],
        "evaluation_criteria": [],
        "submission_requirements": [],
        "compliance_documents": [],
        "briefing_requirements": [],
        "key_dates": [],
        "emails": [],
        "phones": [],
        "contact_names": [],
        "pricing_or_value_clues": [],
        "quantities_or_volumes": [],
        "risks_or_disqualifiers": [],
        "evidence_notes": [],
    }

    for item in parsed_chunks:
        combined["scope_items"] += as_list(item.get("scope_items"), 30)
        combined["deliverables"] += as_list(item.get("deliverables"), 30)
        combined["mandatory_requirements"] += as_list(item.get("mandatory_requirements"), 30)
        combined["mandatory_requirements"] += as_list(item.get("requirements"), 30)
        combined["evaluation_criteria"] += as_list(item.get("evaluation_criteria"), 30)
        combined["submission_requirements"] += as_list(item.get("submission_requirements"), 30)
        combined["compliance_documents"] += as_list(item.get("compliance_documents"), 30)
        combined["briefing_requirements"] += as_list(item.get("briefing_requirements"), 30)
        combined["key_dates"] += as_list(item.get("key_dates"), 30)
        contacts = item.get("contacts") or {}
        if isinstance(contacts, dict):
            combined["emails"] += as_list(contacts.get("emails"), 20)
            combined["phones"] += as_list(contacts.get("phones"), 20)
            combined["contact_names"] += as_list(contacts.get("names"), 20)
        combined["pricing_or_value_clues"] += as_list(item.get("pricing_or_value_clues"), 30)
        combined["pricing_or_value_clues"] += as_list(item.get("commercial_clues"), 30)
        combined["quantities_or_volumes"] += as_list(item.get("quantities_or_volumes"), 30)
        combined["risks_or_disqualifiers"] += as_list(item.get("risks_or_disqualifiers"), 30)
        combined["risks_or_disqualifiers"] += as_list(item.get("risks"), 30)
        combined["evidence_notes"] += as_list(item.get("evidence_notes"), 40)

    for key in list(combined.keys()):
        combined[key] = as_list(combined[key], 40)
    return combined


def consolidate_parse(client, tender: TenderCache, combined: Dict[str, Any]) -> Dict[str, Any]:
    system_prompt = """
Consolidate extracted tender facts into a compact tender intelligence record.

Use only extracted facts. Do not invent.
Return valid JSON only:
{
 "scope_summary": string,
 "deliverables": [string],
 "requirements_and_criteria": {
   "mandatory_requirements": [string],
   "evaluation_criteria": [string],
   "submission_requirements": [string],
   "compliance_documents": [string],
   "briefing_requirements": [string]
 },
 "commercial_clues": {
   "pricing_or_value_clues": [string],
   "quantities_or_volumes": [string],
   "estimated_contract_value_visible": string|null
 },
 "key_dates": [string],
 "contact_email": string|null,
 "contact_phone": string|null,
 "contact_names": [string],
 "location": string|null,
 "risks_or_disqualifiers": [string],
 "evidence_notes": [string],
 "parse_confidence": "high" | "medium" | "low"
}
""".strip()

    payload = {
        "tender_metadata": {
            "title": tender.title,
            "description": tender.description,
            "buyer_name": tender.buyer_name,
            "province": tender.province,
            "closing_date": tender.closing_date.isoformat() if tender.closing_date else None,
        },
        "extracted_facts": combined,
    }
    parsed = openai_json(client, system_prompt, payload, max_tokens=PARSE_CONSOLIDATE_MAX_TOKENS)

    requirements = parsed.get("requirements_and_criteria") or {}
    commercial = parsed.get("commercial_clues") or {}

    return {
        "scope_summary": parsed.get("scope_summary") or tender.description or tender.title,
        "deliverables": as_list(parsed.get("deliverables"), 20),
        "requirements_and_criteria": {
            "mandatory_requirements": as_list(requirements.get("mandatory_requirements"), 20),
            "evaluation_criteria": as_list(requirements.get("evaluation_criteria"), 20),
            "submission_requirements": as_list(requirements.get("submission_requirements"), 20),
            "compliance_documents": as_list(requirements.get("compliance_documents"), 20),
            "briefing_requirements": as_list(requirements.get("briefing_requirements"), 12),
        },
        "commercial_clues": {
            "pricing_or_value_clues": as_list(commercial.get("pricing_or_value_clues"), 20),
            "quantities_or_volumes": as_list(commercial.get("quantities_or_volumes"), 20),
            "estimated_contract_value_visible": commercial.get("estimated_contract_value_visible"),
        },
        "key_dates": as_list(parsed.get("key_dates"), 20),
        "contact_email": parsed.get("contact_email"),
        "contact_phone": parsed.get("contact_phone"),
        "contact_names": as_list(parsed.get("contact_names"), 12),
        "location": parsed.get("location") or tender.province,
        "risks_or_disqualifiers": as_list(parsed.get("risks_or_disqualifiers"), 20),
        "evidence_notes": as_list(parsed.get("evidence_notes"), 30),
        "parse_confidence": parsed.get("parse_confidence") or "medium",
    }


def local_consolidate(tender: TenderCache, combined: Dict[str, Any], error: str | None = None) -> Dict[str, Any]:
    return {
        "scope_summary": "; ".join(as_list(combined.get("scope_items"), 5)) or tender.description or tender.title,
        "deliverables": as_list(combined.get("deliverables"), 20),
        "requirements_and_criteria": {
            "mandatory_requirements": as_list(combined.get("mandatory_requirements"), 20),
            "evaluation_criteria": as_list(combined.get("evaluation_criteria"), 20),
            "submission_requirements": as_list(combined.get("submission_requirements"), 20),
            "compliance_documents": as_list(combined.get("compliance_documents"), 20),
            "briefing_requirements": as_list(combined.get("briefing_requirements"), 12),
        },
        "commercial_clues": {
            "pricing_or_value_clues": as_list(combined.get("pricing_or_value_clues"), 20),
            "quantities_or_volumes": as_list(combined.get("quantities_or_volumes"), 20),
            "estimated_contract_value_visible": None,
        },
        "key_dates": as_list(combined.get("key_dates"), 20),
        "contact_email": (as_list(combined.get("emails"), 1) or [None])[0],
        "contact_phone": (as_list(combined.get("phones"), 1) or [None])[0],
        "contact_names": as_list(combined.get("contact_names"), 12),
        "location": tender.province,
        "risks_or_disqualifiers": as_list(combined.get("risks_or_disqualifiers"), 20),
        "evidence_notes": as_list(combined.get("evidence_notes"), 30),
        "parse_confidence": "medium",
        "_consolidation_fallback": True,
        "_consolidation_error": error,
    }


def process_parse_worker(session, limit: int = 1) -> Dict[str, Any]:
    """
    Queue-safe parse worker.

    Fixes DB deadlocks by claiming jobs atomically using FOR UPDATE SKIP LOCKED.
    This allows multiple Lambda invocations to run without grabbing the same row.
    """
    ensure_parse_table(session)
    limit = max(1, min(int(limit), 5))

    claimed_rows = session.execute(text("""
        WITH claimed AS (
            SELECT id
            FROM tender_document_parsed_cache
            WHERE parse_status IN ('pending', 'running')
            ORDER BY updated_at ASC, id ASC
            FOR UPDATE SKIP LOCKED
            LIMIT :limit
        )
        UPDATE tender_document_parsed_cache p
        SET parse_status = 'running',
            updated_at = NOW()
        FROM claimed
        WHERE p.id = claimed.id
        RETURNING p.*
    """), {"limit": limit}).mappings().all()

    session.flush()

    items = []

    for row in claimed_rows:
        parsed_id = row["id"]

        try:
            tender = session.get(TenderCache, row["tender_id"])
            doc = session.get(TenderDocumentCache, row["document_id"]) if row["document_id"] else None

            if not tender or not doc:
                session.execute(text("""
                    UPDATE tender_document_parsed_cache
                    SET parse_status = 'failed',
                        error_message = :error_message,
                        updated_at = NOW()
                    WHERE id = :id
                """), {"id": parsed_id, "error_message": "Tender or document row is missing."})
                items.append({"ok": False, "parsed_id": parsed_id, "status": "failed", "error": "missing tender or document"})
                continue

            source_text = (doc.extracted_text or "").strip()
            chunks = split_text(source_text)
            state = safe_json_loads(row.get("parsed_json"), {})
            progress = state.get("_parse_progress") or {}
            parsed_chunks = progress.get("chunks") or []
            current_index = int(progress.get("current_chunk_index") or 0)
            total_chunks = len(chunks)

            if total_chunks == 0:
                session.execute(text("""
                    UPDATE tender_document_parsed_cache
                    SET parse_status = 'failed',
                        error_message = :error_message,
                        updated_at = NOW()
                    WHERE id = :id
                """), {"id": parsed_id, "error_message": "No usable document text chunks available for parsing."})
                items.append({"ok": False, "parsed_id": parsed_id, "tender_id": tender.id, "status": "failed", "error": "no usable chunks"})
                continue

            if current_index < total_chunks:
                try:
                    client = get_openai_client()
                    chunk_result = parse_chunk(client, tender, chunks[current_index], current_index, total_chunks)
                except Exception as exc:
                    chunk_result = regex_fallback_parse_chunk(chunks[current_index])
                    chunk_result["_openai_error"] = str(exc)[:1000]

                parsed_chunks.append(chunk_result)
                current_index += 1

                progress = {
                    "mode": "queued_worker_skip_locked",
                    "current_chunk_index": current_index,
                    "total_chunks": total_chunks,
                    "chunks": parsed_chunks,
                }

                session.execute(text("""
                    UPDATE tender_document_parsed_cache
                    SET parsed_json = :parsed_json,
                        chunk_count = :chunk_count,
                        parse_status = 'running',
                        updated_at = NOW(),
                        error_message = NULL
                    WHERE id = :id
                """), {
                    "id": parsed_id,
                    "parsed_json": json.dumps({"_parse_progress": progress}, ensure_ascii=False, default=str),
                    "chunk_count": total_chunks,
                })

                items.append({
                    "ok": True,
                    "parsed_id": parsed_id,
                    "tender_id": tender.id,
                    "status": "running",
                    "chunk_parsed": current_index,
                    "total_chunks": total_chunks,
                    "remaining_chunks": max(total_chunks - current_index, 0),
                })
                continue

            combined = combine_locally(parsed_chunks)
            try:
                client = get_openai_client()
                final_parse = consolidate_parse(client, tender, combined)
            except Exception as exc:
                final_parse = local_consolidate(tender, combined, error=str(exc)[:1000])

            final_parse["_parse_meta"] = {
                "tender_id": tender.id,
                "document_id": doc.id,
                "document_hash": row["document_hash"],
                "source_text_chars": len(source_text),
                "source_text_words": len(re.findall(r"\w+", source_text)),
                "chunk_count": total_chunks,
                "parse_model": OPENAI_MODEL,
                "parse_mode": "queued_worker_skip_locked",
            }

            session.execute(text("""
                UPDATE tender_document_parsed_cache
                SET parse_status = 'parsed',
                    parsed_json = :parsed_json,
                    chunk_count = :chunk_count,
                    error_message = NULL,
                    updated_at = NOW()
                WHERE id = :id
            """), {
                "id": parsed_id,
                "parsed_json": json.dumps(final_parse, ensure_ascii=False, default=str),
                "chunk_count": total_chunks,
            })

            items.append({"ok": True, "parsed_id": parsed_id, "tender_id": tender.id, "status": "parsed", "chunk_count": total_chunks})

        except Exception as exc:
            try:
                session.execute(text("""
                    UPDATE tender_document_parsed_cache
                    SET parse_status = 'failed',
                        error_message = :error_message,
                        updated_at = NOW()
                    WHERE id = :id
                """), {"id": parsed_id, "error_message": str(exc)[:5000]})
            except Exception:
                pass

            items.append({"ok": False, "parsed_id": parsed_id, "status": "failed", "error": str(exc)})

    return {
        "ok": True,
        "mode": "queued_parse_worker_skip_locked",
        "limit": limit,
        "processed": len(items),
        "items": items,
    }


# Backward-compatible names used by app.py routes.
def parse_tender_document(session, tender: TenderCache, force: bool = False) -> Dict[str, Any]:
    return create_parse_job(session, tender, force=force)


def parse_live_tender_documents(session, limit: int = 10, force: bool = False) -> Dict[str, Any]:
    return queue_live_tender_documents(session, limit=limit, force=force)
