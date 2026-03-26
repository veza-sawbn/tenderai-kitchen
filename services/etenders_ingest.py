import json
import os
from datetime import date, datetime, timezone
from typing import Any

import requests
from sqlalchemy import and_, select

from models import IngestRun, TenderCache


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if not text:
        return None

    # Try ISO-style first (handles YYYY-MM-DD and full datetime with Z)
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).date()
    except Exception:
        pass

    # Fallback common formats
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except Exception:
            continue

    return None


def clean_text(value: Any, max_len: int | None = None) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split()).strip()
    if not text:
        return None
    if max_len is not None:
        return text[:max_len]
    return text


def first_non_empty(*values: Any) -> Any:
    for v in values:
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        return v
    return None


def dig(data: dict, *path: str) -> Any:
    cur = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
        if cur is None:
            return None
    return cur


def listify(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def normalize_tender_from_release(release: dict) -> dict | None:
    """
    Normalize one OCDS release/record into canonical TenderCache fields.
    Handles variability across OCDS publishers.
    """
    if not isinstance(release, dict):
        return None

    ocid = clean_text(first_non_empty(release.get("ocid"), release.get("id")), 255)
    if not ocid:
        return None

    tender = release.get("tender") or {}
    planning = release.get("planning") or {}
    buyer = release.get("buyer") or {}
    parties = listify(release.get("parties"))
    tag_list = listify(release.get("tag"))

    # Basic identifiers
    source_identifier = ocid
    notice_number = clean_text(
        first_non_empty(
            tender.get("id"),
            release.get("id"),
            dig(release, "tender", "procuringEntity", "id"),
        ),
        255,
    )

    # Title/description
    title = clean_text(
        first_non_empty(
            tender.get("title"),
            release.get("title"),
            dig(planning, "budget", "description"),
            "Untitled tender",
        ),
        500,
    )

    description = clean_text(
        first_non_empty(
            tender.get("description"),
            release.get("description"),
            dig(planning, "rationale"),
        )
    )

    # Buyer
    buyer_name = clean_text(
        first_non_empty(
            buyer.get("name"),
            dig(tender, "procuringEntity", "name"),
        ),
        255,
    )

    if not buyer_name and parties:
        for p in parties:
            roles = listify((p or {}).get("roles"))
            if "buyer" in roles:
                buyer_name = clean_text((p or {}).get("name"), 255)
                if buyer_name:
                    break

    # Category / type / industry-ish fields
    tender_type = clean_text(first_non_empty(tender.get("procurementMethodDetails"), tender.get("mainProcurementCategory"), tender.get("procurementMethod")), 120)
    category = clean_text(
        first_non_empty(
            tender.get("mainProcurementCategory"),
            dig(tender, "classification", "description"),
            tender_type,
        ),
        255,
    )
    industry = category  # keep existing app behavior compatible

    # Dates
    issued_date = parse_date(
        first_non_empty(
            tender.get("datePublished"),
            release.get("date"),
            tender.get("publicationDate"),
        )
    )
    closing_date = parse_date(
        first_non_empty(
            dig(tender, "tenderPeriod", "endDate"),
            tender.get("submissionDeadline"),
            tender.get("closingDate"),
        )
    )

    # URLs
    detail_url = clean_text(
        first_non_empty(
            release.get("url"),
            tender.get("url"),
        ),
        2000,
    )

    document_url = None
    docs = listify(tender.get("documents"))
    for d in docs:
        if not isinstance(d, dict):
            continue
        maybe = clean_text(first_non_empty(d.get("url"), d.get("documentUrl")), 2000)
        if maybe:
            document_url = maybe
            break

    # Province/location (best effort)
    province = None
    delivery_addresses = listify(dig(tender, "deliveryAddresses"))
    if delivery_addresses:
        for addr in delivery_addresses:
            region = clean_text(first_non_empty((addr or {}).get("region"), (addr or {}).get("locality")), 120)
            if region:
                province = region
                break
    if not province:
        province = clean_text(first_non_empty(dig(tender, "items", 0, "deliveryLocation", "description")), 120) if isinstance(dig(tender, "items"), list) else None

    # Live state
    status = (clean_text(tender.get("status"), 50) or "").lower()
    is_live = status not in {"cancelled", "unsuccessful", "complete", "withdrawn"}

    return {
        "source": "etenders-ocds",
        "source_identifier": source_identifier,
        "notice_number": notice_number,
        "title": title,
        "description": description,
        "buyer_name": buyer_name,
        "category": category,
        "industry": industry,
        "province": province,
        "tender_type": tender_type,
        "detail_url": detail_url,
        "source_url": detail_url,  # compatibility with existing templates
        "document_url": document_url,
        "issued_date": issued_date,
        "closing_date": closing_date,
        "is_live": is_live,
        "raw_payload": release,
        "tags": tag_list,
    }


def extract_releases(payload: dict) -> list[dict]:
    """
    Supports common OCDS response shapes:
    - { "releases": [...] }
    - { "records": [ { "releases": [...] }, ... ] }
    - { "data": [...] } (publisher-specific)
    """
    if not isinstance(payload, dict):
        return []

    releases: list[dict] = []

    direct = payload.get("releases")
    if isinstance(direct, list):
        releases.extend([r for r in direct if isinstance(r, dict)])

    records = payload.get("records")
    if isinstance(records, list):
        for rec in records:
            if not isinstance(rec, dict):
                continue
            rec_releases = rec.get("releases")
            if isinstance(rec_releases, list):
                releases.extend([r for r in rec_releases if isinstance(r, dict)])
            # Some record structures also include compiledRelease
            compiled = rec.get("compiledRelease")
            if isinstance(compiled, dict):
                releases.append(compiled)

    data = payload.get("data")
    if isinstance(data, list):
        releases.extend([r for r in data if isinstance(r, dict)])

    # De-duplicate by (ocid,id)
    deduped = {}
    for r in releases:
        key = f"{r.get('ocid','')}::{r.get('id','')}"
        deduped[key] = r
    return list(deduped.values())


def upsert_tender(session, normalized: dict, now: datetime) -> str:
    """
    Returns one of: created, updated, unchanged
    """
    source = normalized["source"]
    source_identifier = normalized["source_identifier"]

    existing = session.execute(
        select(TenderCache).where(
            and_(
                TenderCache.source == source,
                TenderCache.source_identifier == source_identifier,
            )
        )
    ).scalars().first()

    if existing is None:
        row = TenderCache(
            source=source,
            source_identifier=source_identifier,
            notice_number=normalized.get("notice_number"),
            title=normalized.get("title"),
            description=normalized.get("description"),
            buyer_name=normalized.get("buyer_name"),
            category=normalized.get("category"),
            industry=normalized.get("industry"),
            province=normalized.get("province"),
            tender_type=normalized.get("tender_type"),
            detail_url=normalized.get("detail_url"),
            source_url=normalized.get("source_url"),
            document_url=normalized.get("document_url"),
            issued_date=normalized.get("issued_date"),
            closing_date=normalized.get("closing_date"),
            is_live=bool(normalized.get("is_live", True)),
            raw_payload=json.dumps(normalized.get("raw_payload") or {}, ensure_ascii=False),
            last_seen_at=now,
            updated_at=now,
        )
        session.add(row)
        return "created"

    changed = False
    assign_fields = [
        "notice_number",
        "title",
        "description",
        "buyer_name",
        "category",
        "industry",
        "province",
        "tender_type",
        "detail_url",
        "source_url",
        "document_url",
        "issued_date",
        "closing_date",
    ]
    for f in assign_fields:
        new_val = normalized.get(f)
        if getattr(existing, f, None) != new_val:
            setattr(existing, f, new_val)
            changed = True

    new_is_live = bool(normalized.get("is_live", True))
    if existing.is_live != new_is_live:
        existing.is_live = new_is_live
        changed = True

    new_raw = json.dumps(normalized.get("raw_payload") or {}, ensure_ascii=False)
    if getattr(existing, "raw_payload", None) != new_raw:
        existing.raw_payload = new_raw
        changed = True

    existing.last_seen_at = now
    existing.updated_at = now

    return "updated" if changed else "unchanged"


def build_request_params(page: int, page_size: int) -> dict:
    # Common pagination conventions across OCDS endpoints
    return {
        "page": page,
        "size": page_size,
    }


def request_page(base_url: str, page: int, page_size: int, timeout: int = 30) -> requests.Response:
    params = build_request_params(page=page, page_size=page_size)
    return requests.get(base_url, params=params, timeout=timeout)


def ingest_tenders(session, max_pages: int = 1) -> dict:
    """
    Main ingest entrypoint used by app.py /api/admin/run-ingest.
    """
    base_url = os.getenv("ETENDERS_OCDS_URL", "").strip()
    if not base_url:
        return {"ok": False, "error": "ETENDERS_OCDS_URL is not set"}

    page_size = int(os.getenv("ETENDERS_PAGE_SIZE", "100"))
    timeout = int(os.getenv("ETENDERS_HTTP_TIMEOUT", "30"))
    source_name = "etenders-ocds"

    run = IngestRun(
        source=source_name,
        status="running",
        started_at=utcnow(),
        completed_at=None,
        fetched_count=0,
        created_count=0,
        updated_count=0,
        unchanged_count=0,
        expired_count=0,
        failed_count=0,
        error_message=None,
    )
    session.add(run)
    session.flush()

    now = utcnow()
    fetched_total = 0
    created_total = 0
    updated_total = 0
    unchanged_total = 0
    failed_total = 0
    page = 1
    seen_any = False

    try:
        while page <= max_pages:
            try:
                resp = request_page(base_url=base_url, page=page, page_size=page_size, timeout=timeout)
            except Exception as exc:
                failed_total += 1
                run.status = "failed"
                run.error_message = f"Request failed on page {page}: {exc}"
                break

            # Stop 400 loop and record clear reason
            if resp.status_code == 400:
                failed_total += 1
                run.status = "failed"
                run.error_message = f"400 Bad Request on page {page}. URL={resp.url} body={resp.text[:1500]}"
                break

            if resp.status_code >= 500:
                failed_total += 1
                run.status = "failed"
                run.error_message = f"Server error {resp.status_code} on page {page}. URL={resp.url}"
                break

            if resp.status_code >= 300:
                failed_total += 1
                run.status = "failed"
                run.error_message = f"HTTP {resp.status_code} on page {page}. URL={resp.url} body={resp.text[:1000]}"
                break

            try:
                payload = resp.json()
            except Exception as exc:
                failed_total += 1
                run.status = "failed"
                run.error_message = f"Invalid JSON on page {page}: {exc}"
                break

            releases = extract_releases(payload)
            if not releases:
                # No more data
                break

            seen_any = True

            for rel in releases:
                fetched_total += 1
                normalized = normalize_tender_from_release(rel)
                if not normalized:
                    failed_total += 1
                    continue

                result = upsert_tender(session=session, normalized=normalized, now=now)
                if result == "created":
                    created_total += 1
                elif result == "updated":
                    updated_total += 1
                else:
                    unchanged_total += 1

            # If fewer than page_size, likely last page
            if len(releases) < page_size:
                break

            page += 1

        # Mark expired only if we successfully saw at least one page of data
        expired_total = 0
        if seen_any:
            stale_rows = session.execute(
                select(TenderCache).where(
                    and_(
                        TenderCache.source == source_name,
                        TenderCache.is_live.is_(True),
                        TenderCache.last_seen_at < now,
                    )
                )
            ).scalars().all()

            for row in stale_rows:
                row.is_live = False
                row.updated_at = now
                expired_total += 1
        else:
            expired_total = 0

        # finalize run
        if run.status != "failed":
            run.status = "success"

        run.fetched_count = fetched_total
        run.created_count = created_total
        run.updated_count = updated_total
        run.unchanged_count = unchanged_total
        run.expired_count = expired_total
        run.failed_count = failed_total
        run.completed_at = utcnow()

        session.flush()

        return {
            "ok": run.status == "success",
            "status": run.status,
            "run_id": run.id,
            "fetched": fetched_total,
            "created": created_total,
            "updated": updated_total,
            "unchanged": unchanged_total,
            "expired": expired_total,
            "failed": failed_total,
            "error": run.error_message,
            "pages_processed": page if seen_any else (page - 1 if page > 1 else 0),
        }

    except Exception as exc:
        run.status = "failed"
        run.error_message = f"Unexpected ingest failure: {exc}"
        run.fetched_count = fetched_total
        run.created_count = created_total
        run.updated_count = updated_total
        run.unchanged_count = unchanged_total
        run.expired_count = 0
        run.failed_count = failed_total + 1
        run.completed_at = utcnow()
        session.flush()
        return {
            "ok": False,
            "status": "failed",
            "run_id": run.id,
            "fetched": fetched_total,
            "created": created_total,
            "updated": updated_total,
            "unchanged": unchanged_total,
            "expired": 0,
            "failed": failed_total + 1,
            "error": run.error_message,
        }
