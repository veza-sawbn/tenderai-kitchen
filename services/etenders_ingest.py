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

DEFAULT_PAGE_SIZE = int(os.getenv("ETENDERS_PAGE_SIZE", "1000"))
DEFAULT_LOOKBACK_DAYS = int(os.getenv("ETENDERS_LOOKBACK_DAYS", "120"))
DEFAULT_LOOKAHEAD_DAYS = int(os.getenv("ETENDERS_LOOKAHEAD_DAYS", "180"))

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


def parse_date(value: Any) -> date | None:
    if not value:
        return None

    if isinstance(value, date) and not isinstance(value, datetime):
        return value

    text = str(value).strip()
    if not text:
        return None

    candidates = [
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
    ]

    for fmt in candidates:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except Exception:
        return None


def build_date_window() -> tuple[str, str]:
    today = date.today()
    date_from = today - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    date_to = today + timedelta(days=DEFAULT_LOOKAHEAD_DAYS)
    return date_from.isoformat(), date_to.isoformat()


def fetch_page(page_number: int, page_size: int = DEFAULT_PAGE_SIZE, timeout: int = 40) -> tuple[Any | None, str | None]:
    """
    The public API is sensitive to parameter names.
    Swagger shows PageNumber and PageSize, and public portal download examples
    commonly include dateFrom and dateTo.
    """
    date_from, date_to = build_date_window()

    param_variants = [
        {
            "PageNumber": page_number,
            "PageSize": page_size,
            "dateFrom": date_from,
            "dateTo": date_to,
        },
        {
            "PageNumber": page_number,
            "PageSize": page_size,
        },
        {
            "pageNumber": page_number,
            "pageSize": page_size,
            "dateFrom": date_from,
            "dateTo": date_to,
        },
        {
            "pageNumber": page_number,
            "pageSize": page_size,
        },
    ]

    last_error = None

    for params in param_variants:
        try:
            response = requests.get(DEFAULT_ETENDERS_URL, params=params, timeout=timeout)
            if response.status_code == 400:
                last_error = f"400 Bad Request for params={params}"
                continue
            response.raise_for_status()
            return response.json(), None
        except Exception as exc:
            last_error = str(exc)

    return None, last_error


def extract_releases(payload: Any) -> list[dict]:
    if payload is None:
        return []

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if not isinstance(payload, dict):
        return []

    for key in ["releases", "data", "value", "results", "items"]:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]

    # Some APIs return a wrapper like {"data": {"results": [...]}}
    for key in ["data", "value", "result"]:
        nested = payload.get(key)
        if isinstance(nested, dict):
            for child_key in ["releases", "results", "items"]:
                child = nested.get(child_key)
                if isinstance(child, list):
                    return [item for item in child if isinstance(item, dict)]

    return []


def text_blob_from_release(release: dict) -> str:
    parts = [
        release.get("title", ""),
        release.get("description", ""),
        safe_json_dumps(release.get("tender")),
        safe_json_dumps(release.get("planning")),
        safe_json_dumps(release.get("buyer")),
        safe_json_dumps(release.get("parties")),
        safe_json_dumps(release.get("documents")),
    ]
    return " ".join([str(p) for p in parts if p]).lower()


def infer_buyer_name(release: dict) -> str | None:
    buyer = release.get("buyer") or {}
    if isinstance(buyer, dict) and buyer.get("name"):
        return str(buyer["name"])

    parties = release.get("parties") or []
    for party in parties:
        if not isinstance(party, dict):
            continue
        roles = [str(r).lower() for r in party.get("roles", [])]
        if "buyer" in roles and party.get("name"):
            return str(party["name"])

    return None


def infer_province(release: dict) -> str | None:
    blob = text_blob_from_release(release)
    for province in PROVINCES:
        if province in blob:
            return province.title()
    return None


def infer_tender_type(release: dict) -> str | None:
    blob = text_blob_from_release(release)
    tender = release.get("tender") or {}

    procurement_method = tender.get("procurementMethod")
    if procurement_method:
        return str(procurement_method).title()

    main_procurement_category = tender.get("mainProcurementCategory")
    if main_procurement_category:
        return str(main_procurement_category).title()

    if "rfq" in blob:
        return "RFQ"
    if "rfp" in blob:
        return "RFP"
    if "quotation" in blob:
        return "Quotation"
    if "bid" in blob:
        return "Bid"

    return "Tender"


def infer_industry(release: dict) -> str | None:
    blob = text_blob_from_release(release)

    rules = [
        ("Construction", ["construction", "building", "civil works", "infrastructure", "contractor"]),
        ("ICT", ["ict", "software", "system", "it service", "digital", "technology", "network"]),
        ("Transport", ["transport", "logistics", "fleet", "vehicle", "shuttle", "mobility"]),
        ("Professional Services", ["consulting", "advisory", "facilitation", "professional services", "training"]),
        ("Security", ["security", "guarding", "surveillance"]),
        ("Facilities", ["maintenance", "cleaning", "facilities", "repairs"]),
        ("Medical", ["medical", "health", "clinic", "hospital"]),
        ("Education", ["education", "school", "training provider", "learnership"]),
        ("Tourism", ["tourism", "travel", "destination", "hospitality", "adventure"]),
        ("Energy", ["energy", "solar", "electrical", "power"]),
    ]

    for label, words in rules:
        if any(word in blob for word in words):
            return label

    return None


def infer_document_url(release: dict) -> str | None:
    candidates = []

    tender = release.get("tender") or {}
    tender_docs = tender.get("documents") or []
    for doc in tender_docs:
        if isinstance(doc, dict) and doc.get("url"):
            candidates.append(doc["url"])

    release_docs = release.get("documents") or []
    for doc in release_docs:
        if isinstance(doc, dict) and doc.get("url"):
            candidates.append(doc["url"])

    planning = release.get("planning") or {}
    planning_docs = planning.get("documents") or []
    for doc in planning_docs:
        if isinstance(doc, dict) and doc.get("url"):
            candidates.append(doc["url"])

    for candidate in candidates:
        if isinstance(candidate, str) and candidate.startswith("http"):
            return candidate

    return None


def infer_source_url(release: dict) -> str | None:
    links = release.get("links") or []
    for link in links:
        if isinstance(link, dict) and link.get("href"):
            href = str(link["href"])
            if href.startswith("http"):
                return href

    # Fallback to the public release endpoint if ocid exists
    ocid = release.get("ocid")
    if ocid:
        return f"https://ocds-api.etenders.gov.za/api/OCDSReleases/release/{ocid}"

    return infer_document_url(release)


def is_live_tender_release(release: dict) -> bool:
    """
    Be conservative about excluding records.
    We only exclude obvious award/closed/cancelled records.
    """
    blob = text_blob_from_release(release)
    tender = release.get("tender") or {}

    status = str(tender.get("status") or release.get("status") or "").strip().lower()
    tags = [str(t).lower() for t in (release.get("tag") or [])]

    obvious_award_signals = [
        "award notice",
        "awarded",
        "contract award",
        "contract awarded",
    ]
    if any(signal in blob for signal in obvious_award_signals):
        return False

    if any("award" == tag or "awardupdate" == tag for tag in tags):
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

    title = (
        tender.get("title")
        or release.get("title")
        or ""
    ).strip()
    description = (
        tender.get("description")
        or release.get("description")
        or ""
    ).strip()

    if not title and not description:
        return False

    return True


def normalize_release(release: dict) -> dict | None:
    if not isinstance(release, dict):
        return None

    if not is_live_tender_release(release):
        return None

    tender = release.get("tender") or {}
    planning = release.get("planning") or {}

    release_id = release.get("id")
    ocid = release.get("ocid")
    tender_uid = ocid or release_id
    if not tender_uid:
        return None

    title = (
        tender.get("title")
        or release.get("title")
        or planning.get("rationale")
        or "Untitled Tender"
    )

    description = (
        tender.get("description")
        or release.get("description")
        or planning.get("rationale")
        or ""
    )

    issued_date = parse_date(
        tender.get("tenderPeriod", {}).get("startDate")
        or tender.get("datePublished")
        or release.get("date")
        or release.get("publishedDate")
        or release.get("datePublished")
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


def ingest_tenders(session, max_pages: int = 10, page_size: int = DEFAULT_PAGE_SIZE) -> dict:
    run = IngestRun(status="running")
    session.add(run)
    session.flush()

    tenders_seen = 0
    tenders_upserted = 0
    seen_this_run = set()

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

            valid_found_on_page = 0

            for release in releases:
                normalized = normalize_release(release)
                if not normalized:
                    continue

                tenders_seen += 1
                valid_found_on_page += 1
                seen_this_run.add(normalized["tender_uid"])

                created = upsert_tender(session, normalized)
                if created:
                    tenders_upserted += 1

            run.pages_succeeded += 1
            session.flush()

            # If the page responded but we found no valid current tenders,
            # stop instead of hammering further pages.
            if valid_found_on_page == 0:
                run.status = "completed"
                break

        # Only mark older cache rows stale if this run actually found tenders
        if seen_this_run:
            live_rows = session.execute(
                select(TenderCache).where(TenderCache.is_live.is_(True))
            ).scalars().all()

            for tender in live_rows:
                if tender.tender_uid not in seen_this_run:
                    if tender.closing_date and tender.closing_date < date.today():
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
        logger.exception("Tender ingest failed")
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
