from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import desc, select, text

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

from models import TenderCache, TenderDocumentCache

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
OPENAI_TIMEOUT_SECONDS = int(os.getenv("OPENAI_TIMEOUT_SECONDS", "45"))
PARSE_CHUNK_CHARS = int(os.getenv("TENDER_PARSE_CHUNK_CHARS", "3500"))
PARSE_MAX_CHUNKS = int(os.getenv("TENDER_PARSE_MAX_CHUNKS", "5"))


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


def compact_text(value: str, max_chars: int) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()[:max_chars]


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


def openai_json(client, system_prompt: str, payload: Dict[str, Any], max_tokens: int = 900) -> Dict[str, Any]:
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
        max_tokens=max_tokens,
    )
    return extract_json_object(response.choices[0].message.content or "")


def latest_document(session, tender_id: int) -> Optional[TenderDocumentCache]:
    return session.execute(
        select(TenderDocumentCache)
        .where(TenderDocumentCache.tender_id == tender_id)
        .order_by(desc(TenderDocumentCache.fetched_at), desc(TenderDocumentCache.id))
        .limit(1)
    ).scalars().first()


def document_hash(text_value: str) -> str:
    return hashlib.sha256((text_value or "").encode("utf-8", errors="ignore")).hexdigest()


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

    parsed_json = row.get("parsed_json")
    try:
        parsed = json.loads(parsed_json) if parsed_json else {}
    except Exception:
        parsed = {}

    return {
        "id": row.get("id"),
        "tender_id": row.get("tender_id"),
        "document_id": row.get("document_id"),
        "document_hash": row.get("document_hash"),
        "parse_status": row.get("parse_status"),
        "parsed_json": parsed,
        "source_text_chars": row.get("source_text_chars"),
        "source_text_words": row.get("source_text_words"),
        "chunk_count": row.get("chunk_count"),
        "updated_at": row.get("updated_at").isoformat() if row.get("updated_at") else None,
    }


def split_text(text_value: str, chunk_chars: int, max_chunks: int) -> List[str]:
    text_value = (text_value or "").strip()
    if not text_value:
        return []
    chunks = []
    pos = 0
    while pos < len(text_value) and len(chunks) < max_chunks:
        chunk = text_value[pos:pos + chunk_chars]
        # try to cut cleanly near a sentence or line break
        if pos + chunk_chars < len(text_value):
            cut = max(chunk.rfind(". "), chunk.rfind("\n"), chunk.rfind("; "))
            if cut > int(chunk_chars * 0.55):
                chunk = chunk[:cut + 1]
        chunks.append(chunk.strip())
        pos += len(chunk)
    return [c for c in chunks if c]


def parse_chunk(client, tender: TenderCache, chunk: str, chunk_index: int) -> Dict[str, Any]:
    system_prompt = """
You are parsing a South African tender document chunk.

Extract only what is visible in this chunk. Do not guess.
Return valid JSON only:
{
 "scope_items": [string],
 "deliverables": [string],
 "mandatory_requirements": [string],
 "evaluation_criteria": [string],
 "submission_requirements": [string],
 "compliance_documents": [string],
 "briefing_requirements": [string],
 "key_dates": [string],
 "contacts": {"emails": [string], "phones": [string], "names": [string]},
 "pricing_or_value_clues": [string],
 "quantities_or_volumes": [string],
 "location_clues": [string],
 "risks_or_disqualifiers": [string],
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
        "chunk_index": chunk_index,
        "document_chunk": chunk,
    }
    return openai_json(client, system_prompt, payload, max_tokens=850)


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
        "location_clues": [],
        "risks_or_disqualifiers": [],
        "evidence_notes": [],
    }

    for item in parsed_chunks:
        combined["scope_items"] += as_list(item.get("scope_items"), 30)
        combined["deliverables"] += as_list(item.get("deliverables"), 30)
        combined["mandatory_requirements"] += as_list(item.get("mandatory_requirements"), 30)
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
        combined["quantities_or_volumes"] += as_list(item.get("quantities_or_volumes"), 30)
        combined["location_clues"] += as_list(item.get("location_clues"), 30)
        combined["risks_or_disqualifiers"] += as_list(item.get("risks_or_disqualifiers"), 30)
        combined["evidence_notes"] += as_list(item.get("evidence_notes"), 40)

    for key in list(combined.keys()):
        combined[key] = as_list(combined[key], 40)

    return combined


def consolidate_parse(client, tender: TenderCache, combined: Dict[str, Any]) -> Dict[str, Any]:
    system_prompt = """
You are consolidating extracted tender facts into a compact tender intelligence record.

Use only the extracted facts. Do not invent requirements.
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
    parsed = openai_json(client, system_prompt, payload, max_tokens=1200)

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


def parse_tender_document(session, tender: TenderCache, force: bool = False) -> Dict[str, Any]:
    ensure_parse_table(session)

    doc = latest_document(session, tender.id)
    if not doc:
        return {
            "ok": False,
            "status": "no_document",
            "tender_id": tender.id,
            "error": "No fetched document found. Run fetch-documents first.",
        }

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

    existing = session.execute(text("""
        SELECT *
        FROM tender_document_parsed_cache
        WHERE tender_id = :tender_id
          AND document_hash = :document_hash
          AND parse_status = 'parsed'
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
    """), {"tender_id": tender.id, "document_hash": doc_hash}).mappings().first()

    if existing and not force:
        return {
            "ok": True,
            "status": "already_parsed",
            "tender_id": tender.id,
            "parsed_id": existing["id"],
            "source_text_chars": existing["source_text_chars"],
        }

    row = session.execute(text("""
        INSERT INTO tender_document_parsed_cache
            (tender_id, document_id, document_hash, parse_status, source_text_chars, source_text_words, chunk_count, created_at, updated_at)
        VALUES
            (:tender_id, :document_id, :document_hash, 'running', :source_text_chars, :source_text_words, 0, NOW(), NOW())
        RETURNING id
    """), {
        "tender_id": tender.id,
        "document_id": doc.id,
        "document_hash": doc_hash,
        "source_text_chars": len(source_text),
        "source_text_words": len(re.findall(r"\w+", source_text)),
    }).mappings().first()
    parsed_id = row["id"]
    session.flush()

    try:
        client = get_openai_client()
        chunks = split_text(source_text, PARSE_CHUNK_CHARS, PARSE_MAX_CHUNKS)
        parsed_chunks = [parse_chunk(client, tender, chunk, idx + 1) for idx, chunk in enumerate(chunks)]
        combined = combine_locally(parsed_chunks)
        final_parse = consolidate_parse(client, tender, combined)

        final_parse["_parse_meta"] = {
            "tender_id": tender.id,
            "document_id": doc.id,
            "document_hash": doc_hash,
            "source_text_chars": len(source_text),
            "source_text_words": len(re.findall(r"\w+", source_text)),
            "chunk_count": len(chunks),
            "parse_model": OPENAI_MODEL,
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
            "chunk_count": len(chunks),
        })

        return {
            "ok": True,
            "status": "parsed",
            "tender_id": tender.id,
            "document_id": doc.id,
            "parsed_id": parsed_id,
            "source_text_chars": len(source_text),
            "chunk_count": len(chunks),
        }

    except Exception as exc:
        session.execute(text("""
            UPDATE tender_document_parsed_cache
            SET parse_status = 'failed',
                error_message = :error_message,
                updated_at = NOW()
            WHERE id = :id
        """), {"id": parsed_id, "error_message": str(exc)[:5000]})
        raise


def parse_live_tender_documents(session, limit: int = 5, force: bool = False) -> Dict[str, Any]:
    ensure_parse_table(session)

    limit = max(1, min(int(limit), 25))
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
        try:
            results.append(parse_tender_document(session, tender, force=force))
            session.flush()
        except Exception as exc:
            results.append({"ok": False, "tender_id": tender.id, "status": "failed", "error": str(exc)})

    return {
        "ok": True,
        "limit": limit,
        "processed": len(results),
        "parsed": sum(1 for r in results if r.get("ok")),
        "failed": sum(1 for r in results if not r.get("ok")),
        "items": results,
    }
