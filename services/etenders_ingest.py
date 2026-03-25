import json
import logging
import os
import re
from datetime import date, datetime, timezone
from typing import Any

import requests
from sqlalchemy import select

from models import IngestRun, TenderCache

logger = logging.getLogger(__name__)

DEFAULT_ETENDERS_URL = os.getenv(
    "ETENDERS_API_URL",
    "https://ocds-api.etenders.gov.za/api/OCDSReleases",
)

PROVINCES = [
    "eastern cape",
    "free state",
    "gauteng",
    "kwazulu-natal",
    "limpopo",
    "mpumalanga",
    "north west",
    "northern cape",
    "western cape",
]


def utcnow():
    return datetime.now(timezone.utc)


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None

    patterns = [
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
    ]
    for fmt in patterns:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except Exception:
        return None


def days_left(closing_date: date | None) -> int | None:
    if not closing_date:
        return None
    return (closing_date - date.today()).days


def safe_json_dumps(payload: Any) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False)
    except Exception:
        return "{}"


def fetch_page(page_number: int, page_size: int = 100, timeout: int = 35) -> tuple[dict | None, str | None]:
    """
    Tries multiple parameter variants because the endpoint has been inconsistent.
    Returns (payload, error_message).
    """
    variants = [
        {"pageNumber": page_number, "pageSize": page_size},
        {"PageNumber": page_number, "PageSize": page_size},
        {"page": page_number, "size": page_size},
    ]

    last_error = None
    for params in variants:
        try:
            response = requests.get(DEFAULT_ETENDERS_URL, params=params, timeout=timeout)
            if response.status_code == 400:
                last_error = f"400 Bad Request for params={params}"
                continue
            response.raise_for_status()
            data = response.json()
            return data, None
        except Exception as exc:
            last_error = str(exc)
            continue

    return None, last_error


def extract_releases(payload: dict) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("releases"), list):
        return payload["releases"]
    if isinstance(payload.get("data"), list):
        return payload["data"]
    if isinstance(payload.get("value"), list):
        return payload["value"]
    if isinstance(payload.get("results"), list):
        return payload["results"]
    return []


def text_blob_from_release(release: dict) -> str:
    parts = [
        release.get("title", ""),
        release.get("description", ""),
        safe_json_dumps(release.get("tender")),
        safe_json_dumps(release.get("parties")),
        safe_json_dumps(release.get("buyer")),
    ]
    return " ".join([p for p in parts if p]).lower()


def is_live_tender_release(release: dict) -> bool:
    blob = text_blob_from_release(release)
    tender = release.get("tender") or {}
    status = (tender.get("status") or release.get("status") or "").lower()
    tag_values = [str(x).lower() for x in (release.get("tag") or [])]

    if "award" in blob or "awarded" in blob:
        return False
    if any("award" in tag for tag in tag_values):
        return False
    if status in {"complete", "cancelled", "unsuccessful", "withdrawn", "terminated"}:
        return False

    close_date = parse_date(
        tender.get("tenderPeriod", {}).get("endDate")
        or tender.get("closingDate")
        or release.get("closingDate")
    )
    if close_date and close_date < date.today():
        return False

    return True


def infer_province(release: dict) -> str | None:
    blob = text_blob_from_release(release)
    for province in PROVINCES:
        if province in blob:
            return province.title()
    return None


def infer_industry(release: dict) -> str | None:
    blob = text_blob_from_release(release)

    rules = [
        ("Construction", ["construction", "building", "civil works", "infrastructure", "contractor"]),
        ("ICT", ["ict", "software", "system", "it service", "digital", "technology", "network"]),
        ("Transport", ["transport", "shuttle", "fleet", "vehicle", "logistics", "mobility"]),
        ("Professional Services", ["consulting", "professional service", "advisory", "training", "facilitation"]),
        ("Security", ["security", "guarding", "surveillance"]),
        ("Facilities", ["maintenance", "cleaning", "facilities", "repairs"]),
        ("Medical", ["medical", "health", "clinic", "hospital"]),
        ("Education", ["education", "school", "training provider", "learning"]),
        ("Tourism", ["tourism", "travel", "destination", "hospitality", "adventure"]),
        ("Energy", ["energy", "solar", "electrical", "power"]),
    ]

    for industry, keywords in rules:
        if any(keyword in blob for keyword in keywords):
            return industry
    return None


def infer_tender_type(release: dict) -> str | None:
    blob = text_blob_from_release(release)
    tender = release.get("tender") or {}
    procurement_method = (tender.get("procurementMethod") or "").strip()

    if procurement_method:
        return procurement_method.title()

    if "rfq" in blob:
        return "RFQ"
    if "rfp" in blob:
        return "RFP"
    if "bid" in blob:
        return "Bid"
    if "quotation" in blob:
        return "Quotation"
    return "Tender"


def infer_buyer_name(release: dict) -> str | None:
    buyer = release.get("buyer") or {}
    if buyer.get("name"):
        return buyer["name"]

    parties = release.get("parties") or []
    for party in parties:
        roles = [str(r).lower() for r in party.get("roles", [])]
        if "buyer" in roles and party.get("name"):
            return party["name"]
    return None


def infer_document_url(release: dict) -> str | None:
    candidates = []

    tender = release.get("tender") or {}
    for d in tender.get("documents", []) or []:
        if d.get("url"):
            candidates.append(d["url"])

    for d in release.get("documents", []) or []:
        if d.get("url"):
            candidates.append(d["url"])

    for candidate in candidates:
        if isinstance(candidate, str) and candidate.startswith("http"):
            return candidate
    return None


def infer_source_url(release: dict) -> str | None:
    links = release.get("links") or []
    for link in links:
        if isinstance(link, dict) and link.get("href"):
            return link["href"]
    return infer_document_url(release)


def normalize_release(release: dict) -> dict | None:
    if not is_live_tender_release(release):
        return None

    tender = release.get("tender") or {}
    release_id = release.get("id")
    ocid = release.get("ocid")
    tender_uid = ocid or release_id
    if not tender_uid:
        return None

    title = (
        tender.get("title")
        or release.get("title")
        or "Untitled Tender"
    )
    description = (
        tender.get("description")
        or release.get("description")
        or ""
    )

    issued_date = parse_date(
        tender.get("tenderPeriod", {}).get("startDate")
        or tender.get("datePublished")
        or release.get("date")
        or release.get("publishedDate")
    )
    closing_date = parse_date(
        tender.get("tenderPeriod", {}).get("endDate")
        or tender.get("closingDate")
        or release.get("closingDate")
    )

    return {
        "tender_uid": str(tender_uid),
        "ocid": str(ocid) if ocid else None,
        "source_release_id": str(release_id) if release_id else None,
        "title": str(title)[:500],
        "description": description,
        "buyer_name": infer_buyer_name(release),
        "province": infer_province(release),
        "tender_type": infer_tender_type(release),
        "industry": infer_industry(release),
        "status": (tender.get("status") or release.get("status") or "active"),
        "issued_date": issued_date,
        "closing_date": closing_date,
        "document_url": infer_document_url(release),
        "source_url": infer_source_url(release),
        "raw_json": safe_json_dumps(release),
        "is_live": True,
    }


def upsert_tender(session, normalized: dict) -> bool:
    existing = session.execute(
        select(TenderCache).where(TenderCache.tender_uid == normalized["tender_uid"])
    ).scalar_one_or_none()

    if existing:
        for key, value in normalized.items():
            setattr(existing, key, value)
        existing.last_seen_at = utcnow()
        return False

    tender = TenderCache(**normalized, last_seen_at=utcnow())
    session.add(tender)
    return True


def ingest_tenders(session, max_pages: int = 20, page_size: int = 100) -> dict:
    run = IngestRun(status="running")
    session.add(run)
    session.flush()

    tenders_upserted = 0
    tenders_seen = 0

    try:
        seen_this_run: set[str] = set()

        for page_number in range(1, max_pages + 1):
            run.pages_attempted += 1

            payload, error = fetch_page(page_number=page_number, page_size=page_size)
            if error:
                run.status = "partial_success" if run.pages_succeeded > 0 else "failed"
                run.failure_message = f"Stopped on page {page_number}: {error}"
                break

            releases = extract_releases(payload)
            if not releases:
                run.status = "completed"
                break

            page_had_valid_tenders = False

            for release in releases:
                normalized = normalize_release(release)
                if not normalized:
                    continue

                page_had_valid_tenders = True
                tenders_seen += 1
                seen_this_run.add(normalized["tender_uid"])
                created = upsert_tender(session, normalized)
                if created:
                    tenders_upserted += 1

            run.pages_succeeded += 1
            session.flush()

            if not page_had_valid_tenders:
                # Graceful early stop if page contains no active tenders.
                run.status = "completed"
                break

        # Mark stale tenders as not live if they were not refreshed on this run.
        existing_live = session.execute(select(TenderCache).where(TenderCache.is_live.is_(True))).scalars().all()
        for tender in existing_live:
            if tender.tender_uid not in seen_this_run and tender.last_seen_at.date() < date.today():
                tender.is_live = False

        if run.status == "running":
            run.status = "completed"

        run.tenders_seen = tenders_seen
        run.tenders_upserted = tenders_upserted
        run.finished_at = utcnow()
        session.flush()

        return {
            "run_id": run.id,
            "status": run.status,
            "pages_attempted": run.pages_attempted,
            "pages_succeeded": run.pages_succeeded,
            "tenders_seen": tenders_seen,
            "tenders_upserted": tenders_upserted,
            "failure_message": run.failure_message,
        }

    except Exception as exc:
        logger.exception("Ingest failed")
        run.status = "failed"
        run.failure_message = str(exc)
        run.finished_at = utcnow()
        session.flush()
        return {
            "run_id": run.id,
            "status": run.status,
            "pages_attempted": run.pages_attempted,
            "pages_succeeded": run.pages_succeeded,
            "tenders_seen": tenders_seen,
            "tenders_upserted": tenders_upserted,
            "failure_message": run.failure_message,
        }
