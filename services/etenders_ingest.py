from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone

from dotenv import load_dotenv
from typing import Any

import requests
from sqlalchemy import select

from models import IngestRun, TenderCache

load_dotenv()

BASE_URL = os.getenv("ETENDERS_OCDS_URL", os.getenv("ETENDERS_OCDS_API_URL", "https://ocds-api.etenders.gov.za/api/OCDSReleases"))
TIMEOUT = int(os.getenv("ETENDERS_HTTP_TIMEOUT", os.getenv("ETENDERS_TIMEOUT_SECONDS", "45")))
PAGE_SIZE = min(int(os.getenv("ETENDERS_PAGE_SIZE", "100")), 1000)
USER_AGENT = os.getenv("TENDERAI_USER_AGENT", "TenderAI/1.0")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_date(value: Any):
    if not value:
        return None
    if hasattr(value, "date") and callable(getattr(value, "date")):
        try:
            return value.date()
        except Exception:
            pass
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except Exception:
        pass
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(text.replace("Z", "+0000"), fmt).date()
        except Exception:
            continue
    return None


def _first_non_empty(*values):
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _get(obj: Any, *path, default=None):
    current = obj
    for step in path:
        if isinstance(current, dict):
            current = current.get(step)
        elif isinstance(current, list):
            try:
                current = current[step]
            except Exception:
                return default
        else:
            return default
        if current is None:
            return default
    return current


def _coerce_releases(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("releases", "items", "value", "data", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    return []


def _guess_document_url(release: dict[str, Any]) -> str | None:
    for doc in _get(release, "tender", "documents", default=[]) or []:
        if isinstance(doc, dict) and doc.get("url"):
            return str(doc["url"])
    for doc in release.get("documents", []) or []:
        if isinstance(doc, dict) and doc.get("url"):
            return str(doc["url"])
    return None


def _guess_province(release: dict[str, Any]) -> str | None:
    for party in release.get("parties") or []:
        if not isinstance(party, dict):
            continue
        address = party.get("address") or {}
        region = _first_non_empty(address.get("region"), address.get("locality"))
        if region:
            return str(region)
    buyer = release.get("buyer") or {}
    address = buyer.get("address") or {}
    region = _first_non_empty(address.get("region"), address.get("locality"))
    return str(region) if region else None


def _map_release_to_tender(release: dict[str, Any]) -> dict[str, Any]:
    tender = release.get("tender") or {}
    ocid = release.get("ocid")
    release_id = release.get("id")
    tender_uid = str(_first_non_empty(release_id, ocid, _get(tender, "id"), _get(tender, "title"), f"unknown-{utcnow().timestamp()}"))
    title = _first_non_empty(tender.get("title"), release.get("title"), ocid, "Untitled tender")
    description = _first_non_empty(tender.get("description"), release.get("description"), "")
    buyer = release.get("buyer") or {}
    buyer_name = _first_non_empty(buyer.get("name"), _get(release, "parties", 0, "name"), "Unknown buyer")
    document_url = _guess_document_url(release)
    source_url = _first_non_empty(release.get("uri"), release.get("url"), document_url)
    closing_date = _parse_date(_first_non_empty(_get(tender, "tenderPeriod", "endDate"), _get(tender, "closingDate"), _get(tender, "enquiryPeriod", "endDate")))
    issued_date = _parse_date(_first_non_empty(release.get("date"), _get(tender, "tenderPeriod", "startDate"), _get(tender, "datePublished")))
    industry = _first_non_empty(_get(tender, "classification", "description"), _get(tender, "mainProcurementCategory"), _get(release, "mainProcurementCategory"), _get(release, "tag", 0))
    tender_type = _first_non_empty(_get(tender, "procurementMethodDetails"), release.get("initiationType"), _get(tender, "procurementMethod"), _get(release, "tag", 0))
    return {
        "tender_uid": tender_uid[:255],
        "ocid": str(ocid)[:255] if ocid else None,
        "source_release_id": str(release_id)[:255] if release_id else None,
        "title": str(title)[:500],
        "description": str(description)[:200000] if description else None,
        "buyer_name": str(buyer_name)[:255] if buyer_name else None,
        "province": _guess_province(release),
        "tender_type": str(tender_type)[:100] if tender_type else None,
        "industry": str(industry)[:100] if industry else None,
        "status": str(_first_non_empty(tender.get("status"), release.get("tag"), release.get("initiationType")))[:50] if _first_non_empty(tender.get("status"), release.get("tag"), release.get("initiationType")) else None,
        "issued_date": issued_date,
        "closing_date": closing_date,
        "document_url": str(document_url)[:5000] if document_url else None,
        "source_url": str(source_url)[:5000] if source_url else None,
        "raw_json": json.dumps(release, ensure_ascii=False)[:500000],
        "is_live": (closing_date is None) or (closing_date >= date.today()),
        "last_seen_at": utcnow(),
    }


def _find_existing_tender(session, mapped: dict[str, Any]):
    existing = session.execute(select(TenderCache).where(TenderCache.tender_uid == mapped["tender_uid"]).limit(1)).scalars().first()
    if existing:
        return existing
    if mapped.get("ocid"):
        existing = session.execute(select(TenderCache).where(TenderCache.ocid == mapped["ocid"]).limit(1)).scalars().first()
        if existing:
            return existing
    if mapped.get("source_url"):
        existing = session.execute(select(TenderCache).where(TenderCache.source_url == mapped["source_url"]).limit(1)).scalars().first()
        if existing:
            return existing
    return None


def ingest_tenders(session, max_pages: int = 1, page_size: int | None = None) -> dict[str, Any]:
    page_size = page_size or PAGE_SIZE
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    run = IngestRun(status="running", started_at=utcnow(), pages_attempted=0, pages_succeeded=0, tenders_seen=0, tenders_upserted=0)
    session.add(run)
    session.flush()

    inserted = 0
    updated = 0
    errors: list[str] = []

    try:
        with requests.Session() as client:
            for page in range(1, max_pages + 1):
                run.pages_attempted += 1
                try:
                    response = client.get(BASE_URL, params={"PageNumber": page, "PageSize": page_size}, headers=headers, timeout=TIMEOUT)
                    response.raise_for_status()
                except Exception as exc:
                    errors.append(f"page {page}: {exc}")
                    break
                payload = response.json()
                releases = _coerce_releases(payload)
                if not releases:
                    break
                run.pages_succeeded += 1
                for release in releases:
                    run.tenders_seen += 1
                    mapped = _map_release_to_tender(release)
                    tender = _find_existing_tender(session, mapped)
                    if tender is None:
                        tender = TenderCache(tender_uid=mapped["tender_uid"], title=mapped["title"])
                        session.add(tender)
                        inserted += 1
                    else:
                        updated += 1
                    for key, value in mapped.items():
                        setattr(tender, key, value)
                    run.tenders_upserted += 1
        run.status = "success" if not errors else "partial_success"
        run.failure_message = " | ".join(errors)[:5000] if errors else None
    except Exception as exc:
        run.status = "failed"
        run.failure_message = str(exc)[:5000]
    finally:
        run.finished_at = utcnow()

    return {
        "ok": run.status in {"success", "partial_success"},
        "status": run.status,
        "inserted": inserted,
        "updated": updated,
        "pages_attempted": run.pages_attempted,
        "pages_succeeded": run.pages_succeeded,
        "tenders_seen": run.tenders_seen,
        "tenders_upserted": run.tenders_upserted,
        "error": run.failure_message,
    }
