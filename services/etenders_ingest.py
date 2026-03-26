import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests
from sqlalchemy import select

from models import IngestRun, TenderCache

logger = logging.getLogger(__name__)

DEFAULT_ETENDERS_URL = os.getenv(
    "ETENDERS_API_URL",
    "https://ocds-api.etenders.gov.za/api/OCDSReleases",
)

DEFAULT_PAGE_SIZE = int(os.getenv("ETENDERS_PAGE_SIZE", "25"))
DEFAULT_LOOKBACK_DAYS = int(os.getenv("ETENDERS_LOOKBACK_DAYS", "14"))
DEFAULT_LOOKAHEAD_DAYS = int(os.getenv("ETENDERS_LOOKAHEAD_DAYS", "30"))
DEFAULT_CONNECT_TIMEOUT = int(os.getenv("ETENDERS_CONNECT_TIMEOUT_SECONDS", "8"))
DEFAULT_READ_TIMEOUT = int(os.getenv("ETENDERS_READ_TIMEOUT_SECONDS", "30"))

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


def safe_json_dumps(payload: Any) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False)
    except Exception:
        return "{}"


def parse_date(value: Any):
    if not value:
        return None

    text = str(value).strip()
    if not text:
        return None

    formats = [
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except Exception:
        return None


def build_date_window():
    today = date.today()
    return (
        (today - timedelta(days=DEFAULT_LOOKBACK_DAYS)).isoformat(),
        (today + timedelta(days=DEFAULT_LOOKAHEAD_DAYS)).isoformat(),
    )


def fetch_page(page_number: int, page_size: int = DEFAULT_PAGE_SIZE):
    date_from, date_to = build_date_window()

    variants = [
        {"PageNumber": page_number, "PageSize": page_size, "dateFrom": date_from, "dateTo": date_to},
        {"pageNumber": page_number, "pageSize": page_size, "dateFrom": date_from, "dateTo": date_to},
        {"PageNumber": page_number, "PageSize": page_size, "DateFrom": date_from, "DateTo": date_to},
        {"pageNumber": page_number, "pageSize": page_size, "DateFrom": date_from, "DateTo": date_to},
    ]

    headers = {
        "Accept": "application/json",
        "User-Agent": "TenderAI/1.0",
    }

    errors = []

    for params in variants:
        try:
            response = requests.get(
                DEFAULT_ETENDERS_URL,
                params=params,
                timeout=(DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT),
                headers=headers,
            )

            if response.status_code == 200:
                return response.json(), None

            body_preview = response.text[:300]
            errors.append(f"{response.status_code} for params={params} body={body_preview}")
        except Exception as exc:
            errors.append(f"error for params={params}: {exc}")

    return None, " | ".join(errors)


def extract_releases(payload: Any):
    if payload is None:
        return []

    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]

    if not isinstance(payload, dict):
        return []

    for key in ["releases", "data", "value", "results", "items"]:
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]

    for key in ["data", "value", "result"]:
        nested = payload.get(key)
        if isinstance(nested, dict):
            for child_key in ["releases", "results", "items"]:
                child = nested.get(child_key)
                if isinstance(child, list):
                    return [x for x in child if isinstance(x, dict)]

    return []


def text_blob(release: dict) -> str:
    parts = [
        release.get("title", ""),
        release.get("description", ""),
        safe_json_dumps(release.get("tender")),
        safe_json_dumps(release.get("buyer")),
        safe_json_dumps(release.get("parties")),
        safe_json_dumps(release.get("documents")),
    ]
    return " ".join([str(x) for x in parts if x]).lower()


def infer_buyer_name(release: dict):
    buyer = release.get("buyer") or {}
    if isinstance(buyer, dict) and buyer.get("name"):
        return str(buyer["name"])

    for party in release.get("parties", []) or []:
        if not isinstance(party, dict):
            continue
        roles = [str(r).lower() for r in party.get("roles", [])]
        if "buyer" in roles and party.get("name"):
            return str(party["name"])

    return None


def infer_document_url(release: dict):
    candidates = []
    tender = release.get("tender") or {}

    for doc in tender.get("documents", []) or []:
        if isinstance(doc, dict) and doc.get("url"):
            candidates.append(doc["url"])

    for doc in release.get("documents", []) or []:
        if isinstance(doc, dict) and doc.get("url"):
            candidates.append(doc["url"])

    for item in candidates:
        if isinstance(item, str) and item.startswith("http"):
            return item

    return None


def infer_province(release: dict):
    blob = text_blob(release)
    for province in PROVINCES:
        if province in blob:
            return province.title()
    return None


def infer_tender_type(release: dict):
    tender = release.get("tender") or {}
    procurement_method = tender.get("procurementMethod")
    if procurement_method:
        return str(procurement_method).title()

    blob = text_blob(release)
    if "rfq" in blob:
        return "RFQ"
    if "rfp" in blob:
        return "RFP"
    if "quotation" in blob:
        return "Quotation"
    if "bid" in blob:
        return "Bid"
    return "Tender"


def infer_industry(release: dict):
    blob = text_blob(release)

    rules = [
        ("Construction", ["construction", "building", "civil works", "infrastructure", "contractor"]),
        ("ICT", ["ict", "software", "system", "it service", "digital", "technology", "network"]),
        ("Transport", ["transport", "logistics", "fleet", "vehicle", "shuttle"]),
        ("Professional Services", ["consulting", "advisory", "professional services", "training"]),
        ("Security", ["security", "guarding", "surveillance"]),
        ("Facilities", ["maintenance", "cleaning", "facilities"]),
        ("Medical", ["medical", "health", "clinic", "hospital"]),
        ("Education", ["education", "school", "training provider"]),
        ("Tourism", ["tourism", "travel", "hospitality", "destination"]),
        ("Energy", ["energy", "electrical", "power", "solar"]),
    ]

    for label, words in rules:
        if any(word in blob for word in words):
            return label

    return None


def is_live_tender_release(release: dict):
    tender = release.get("tender") or {}
    status = str(tender.get("status") or release.get("status") or "").lower()
    tags = [str(t).lower() for t in (release.get("tag") or [])]
    blob = text_blob(release)

    if any(tag in {"award", "awardupdate"} for tag in tags):
        return False
    if "award notice" in blob or "contract awarded" in blob:
        return False
    if status in {"complete", "completed", "cancelled", "withdrawn", "terminated", "unsuccessful"}:
        return False

    closing_date = parse_date(
        tender.get("tenderPeriod", {}).get("endDate")
        or tender.get("closingDate")
        or release.get("closingDate")
    )
    if closing_date and closing_date < date.today():
        return False

    title = tender.get("title") or release.get("title")
    description = tender.get("description") or release.get("description")
    if not title and not description:
        return False

    return True


def normalize_release(release: dict):
    if not isinstance(release, dict):
        return None
    if not is_live_tender_release(release):
        return None

    tender = release.get("tender") or {}
    release_id = release.get("id")
    ocid = release.get("ocid")
    tender_uid = ocid or release_id
    if not tender_uid:
        return None

    title = tender.get("title") or release.get("title") or "Untitled Tender"
    description = tender.get("description") or release.get("description") or ""

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
        "description": str(description) if description else None,
        "buyer_name": infer_buyer_name(release),
        "province": infer_province(release),
        "tender_type": infer_tender_type(release),
        "industry": infer_industry(release),
        "status": str(tender.get("status") or release.get("status") or "active"),
        "issued_date": issued_date,
        "closing_date": closing_date,
        "document_url": infer_document_url(release),
        "source_url": infer_document_url(release),
        "raw_json": safe_json_dumps(release),
        "is_live": True,
    }


def upsert_tender(session, normalized: dict):
    existing = session.execute(
        select(TenderCache).where(TenderCache.tender_uid == normalized["tender_uid"])
    ).scalar_one_or_none()

    if existing:
        for key, value in normalized.items():
            setattr(existing, key, value)
        existing.last_seen_at = utcnow()
        return False

    session.add(TenderCache(**normalized, last_seen_at=utcnow()))
    return True


def ingest_tenders(session, max_pages: int = 2, page_size: int = DEFAULT_PAGE_SIZE):
    run = IngestRun(status="running")
    session.add(run)
    session.flush()

    seen_this_run = set()
    tenders_seen = 0
    tenders_upserted = 0

    try:
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

            valid_count = 0
            for release in releases:
                normalized = normalize_release(release)
                if not normalized:
                    continue

                valid_count += 1
                tenders_seen += 1
                seen_this_run.add(normalized["tender_uid"])

                created = upsert_tender(session, normalized)
                if created:
                    tenders_upserted += 1

            run.pages_succeeded += 1
            session.flush()

            if valid_count == 0:
                run.status = "completed"
                break

        if seen_this_run:
            existing_live = session.execute(
                select(TenderCache).where(TenderCache.is_live.is_(True))
            ).scalars().all()

            for tender in existing_live:
                if tender.tender_uid not in seen_this_run and tender.closing_date and tender.closing_date < date.today():
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
            "tenders_seen": run.tenders_seen,
            "tenders_upserted": run.tenders_upserted,
            "failure_message": run.failure_message,
        }

    except Exception as exc:
        logger.exception("Ingest failed")
        run.status = "failed"
        run.failure_message = str(exc)
        run.finished_at = utcnow()
        run.tenders_seen = tenders_seen
        run.tenders_upserted = tenders_upserted
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
