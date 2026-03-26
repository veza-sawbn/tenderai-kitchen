from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from sqlalchemy import select

from database import get_db_session
from models import IngestRun, TenderCache

DEFAULT_TIMEOUT = int(os.getenv("ETENDERS_HTTP_TIMEOUT", "60"))
DEFAULT_PAGE_SIZE = int(os.getenv("ETENDERS_PAGE_SIZE", "25"))
DEFAULT_MAX_PAGES = int(os.getenv("INGEST_MAX_PAGES", "10"))
DEFAULT_BASE_URL = os.getenv(
    "ETENDERS_OCDS_URL",
    "https://ocds-api.etenders.gov.za/api/OCDSReleases",
)
DEFAULT_DAYS_BACK = int(os.getenv("ETENDERS_DAYS_BACK", "14"))
DEFAULT_USER_AGENT = os.getenv(
    "ETENDERS_USER_AGENT",
    "TenderAI/1.0 (+https://visitdrakensberg.com; procurement intelligence)"
)


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _safe_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return str(value)


def _safe_json(data: Any) -> str:
    try:
        return json.dumps(data, ensure_ascii=False)
    except Exception:
        return json.dumps({"unserializable": True}, ensure_ascii=False)


def _extract_release_list(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    for key in ("releases", "Releases", "data", "Data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _first_identifier(release: Dict[str, Any]) -> Optional[str]:
    identifiers = release.get("tender", {}).get("id")
    if identifiers:
        return _safe_str(identifiers)
    for key in ("id", "ocid"):
        value = release.get(key)
        if value:
            return _safe_str(value)
    return None


def _extract_buyer_name(release: Dict[str, Any]) -> Optional[str]:
    buyer = release.get("buyer") or {}
    if isinstance(buyer, dict):
        return _safe_str(buyer.get("name"))
    return None


def _extract_title(release: Dict[str, Any]) -> Optional[str]:
    tender = release.get("tender") or {}
    if isinstance(tender, dict):
        return _safe_str(tender.get("title")) or _safe_str(tender.get("description"))
    return None


def _extract_description(release: Dict[str, Any]) -> Optional[str]:
    tender = release.get("tender") or {}
    if isinstance(tender, dict):
        return _safe_str(tender.get("description"))
    return None


def _extract_closing_date(release: Dict[str, Any]) -> Optional[datetime]:
    tender = release.get("tender") or {}
    candidates = [
        tender.get("tenderPeriod", {}).get("endDate") if isinstance(tender.get("tenderPeriod"), dict) else None,
        tender.get("bidOpening", {}).get("date") if isinstance(tender.get("bidOpening"), dict) else None,
        release.get("date"),
    ]
    for value in candidates:
        s = _safe_str(value)
        if not s:
            continue
        try:
            s = s.replace("Z", "+00:00")
            return datetime.fromisoformat(s)
        except Exception:
            continue
    return None


def _extract_source_url(release: Dict[str, Any], ocid: Optional[str]) -> Optional[str]:
    links = release.get("links") or {}
    if isinstance(links, dict):
        for key in ("self", "compiledRelease", "releasePackage"):
            value = links.get(key)
            if isinstance(value, dict):
                href = _safe_str(value.get("href"))
                if href:
                    return href
            elif isinstance(value, str):
                href = _safe_str(value)
                if href:
                    return href
    if ocid:
        return f"https://ocds-api.etenders.gov.za/api/OCDSReleases/release/{ocid}"
    return None


def _extract_document_url(release: Dict[str, Any]) -> Optional[str]:
    tender = release.get("tender") or {}
    docs = tender.get("documents") or []
    if isinstance(docs, list):
        for doc in docs:
            if isinstance(doc, dict):
                url = _safe_str(doc.get("url")) or _safe_str(doc.get("downloadUrl"))
                if url:
                    return url
    return None


def _extract_procurement_category(release: Dict[str, Any]) -> Optional[str]:
    tender = release.get("tender") or {}
    items = tender.get("items") or []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                classification = item.get("classification") or {}
                if isinstance(classification, dict):
                    desc = _safe_str(classification.get("description"))
                    if desc:
                        return desc
    return None


def _build_uid(ocid: Optional[str], release_id: Optional[str], tender_id: Optional[str], title: Optional[str]) -> str:
    return (
        ocid
        or release_id
        or tender_id
        or (title[:150] if title else f"generated-{datetime.now(timezone.utc).timestamp()}")
    )


def fetch_release_page(
    page_number: int,
    page_size: int,
    date_from: str,
    date_to: str,
    timeout: int = DEFAULT_TIMEOUT,
    base_url: str = DEFAULT_BASE_URL,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], requests.Response]:
    headers = {
        "Accept": "application/json",
        "User-Agent": DEFAULT_USER_AGENT,
    }
    params = {
        "dateFrom": date_from,
        "dateTo": date_to,
        "PageNumber": page_number,
        "PageSize": page_size,
    }
    response = requests.get(base_url, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    releases = _extract_release_list(payload)
    return payload, releases, response


def upsert_release(session, release: Dict[str, Any]) -> Tuple[str, str]:
    ocid = _safe_str(release.get("ocid"))
    release_id = _safe_str(release.get("id"))
    tender_id = _first_identifier(release)
    title = _extract_title(release)
    description = _extract_description(release)
    buyer_name = _extract_buyer_name(release)
    closing_at = _extract_closing_date(release)
    source_url = _extract_source_url(release, ocid)
    document_url = _extract_document_url(release)
    category = _extract_procurement_category(release)
    tender_uid = _build_uid(ocid, release_id, tender_id, title)

    stmt = select(TenderCache).where(TenderCache.tender_uid == tender_uid)
    existing = session.execute(stmt).scalar_one_or_none()

    values = {
        "tender_uid": tender_uid,
        "ocid": ocid,
        "source_release_id": release_id,
        "title": title or tender_uid,
        "description": description,
        "buyer_name": buyer_name,
        "published_at": datetime.now(timezone.utc),
        "closing_at": closing_at,
        "source_url": source_url,
        "document_url": document_url,
        "industry": category,
        "procurement_category": category,
        "raw_json": _safe_json(release),
        "is_live": True,
    }

    if existing is None:
        tender = TenderCache(**values)
        session.add(tender)
        return "inserted", tender_uid

    for key, value in values.items():
        setattr(existing, key, value)
    return "updated", tender_uid


def run_ingest(
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    base_url: str = DEFAULT_BASE_URL,
) -> Dict[str, Any]:
    today = _today()
    resolved_date_to = date_to or today.isoformat()
    resolved_date_from = date_from or (today - timedelta(days=DEFAULT_DAYS_BACK)).isoformat()

    stats: Dict[str, Any] = {
        "ok": True,
        "status": "success",
        "mode": "live_api",
        "base_url": base_url,
        "page_size": page_size,
        "max_pages": max_pages,
        "date_from": resolved_date_from,
        "date_to": resolved_date_to,
        "pages_attempted": 0,
        "pages_succeeded": 0,
        "tenders_seen": 0,
        "tenders_upserted": 0,
        "inserted": 0,
        "updated": 0,
        "error": None,
    }

    with get_db_session() as session:
        ingest_run = IngestRun(
            started_at=datetime.now(timezone.utc),
            status="running",
            source=base_url,
            pages_attempted=0,
            pages_succeeded=0,
            tenders_seen=0,
            tenders_upserted=0,
            inserted=0,
            updated=0,
        )
        session.add(ingest_run)
        session.flush()

        try:
            for page_number in range(1, max_pages + 1):
                stats["pages_attempted"] += 1
                ingest_run.pages_attempted = stats["pages_attempted"]

                payload, releases, _response = fetch_release_page(
                    page_number=page_number,
                    page_size=page_size,
                    date_from=resolved_date_from,
                    date_to=resolved_date_to,
                    timeout=timeout,
                    base_url=base_url,
                )

                if not releases:
                    break

                stats["pages_succeeded"] += 1
                ingest_run.pages_succeeded = stats["pages_succeeded"]
                stats["tenders_seen"] += len(releases)
                ingest_run.tenders_seen = stats["tenders_seen"]

                for release in releases:
                    action, _uid = upsert_release(session, release)
                    stats["tenders_upserted"] += 1
                    ingest_run.tenders_upserted = stats["tenders_upserted"]
                    if action == "inserted":
                        stats["inserted"] += 1
                        ingest_run.inserted = stats["inserted"]
                    else:
                        stats["updated"] += 1
                        ingest_run.updated = stats["updated"]

                session.commit()

            ingest_run.status = "success"
            ingest_run.finished_at = datetime.now(timezone.utc)
            session.commit()
            return stats
        except Exception as exc:
            stats["ok"] = False
            stats["status"] = "failed"
            stats["error"] = str(exc)
            ingest_run.status = "failed"
            ingest_run.error_message = str(exc)
            ingest_run.finished_at = datetime.now(timezone.utc)
            session.commit()
            return stats
