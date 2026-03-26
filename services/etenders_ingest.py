
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urljoin

import requests
from sqlalchemy import select

from models import IngestRun, TenderCache

LIVE_API_URL = os.getenv("ETENDERS_OCDS_URL", "https://ocds-api.etenders.gov.za/api/OCDSReleases")
# Backward-compatible aliases
LIVE_API_URL = os.getenv("ETENDERS_OCDS_API_URL", LIVE_API_URL)

RELEASES_FILES_URL = os.getenv("ETENDERS_RELEASES_FILES_URL", "https://data.etenders.gov.za/Home/ReleasesFiles")
TIMEOUT = int(os.getenv("ETENDERS_HTTP_TIMEOUT", os.getenv("ETENDERS_TIMEOUT_SECONDS", "45")))
PAGE_SIZE = min(int(os.getenv("ETENDERS_PAGE_SIZE", "100")), 1000)
MAX_PAGES = int(os.getenv("INGEST_MAX_PAGES", "3"))
MAX_BULK_FILES = int(os.getenv("ETENDERS_BULK_FILE_LIMIT", "2"))
USER_AGENT = os.getenv("TENDERAI_USER_AGENT", "TenderAI/1.0 (+https://example.invalid)")

JSON_LINK_RE = re.compile(r'href=["\\\']([^"\\\']+?\.json(?:\?[^"\\\']*)?)["\\\']', re.IGNORECASE)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_date(value: Any):
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).date()
    except Exception:
        pass
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).date()
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


def _get(dct: Any, *path, default=None):
    current = dct
    for step in path:
        if current is None:
            return default
        if isinstance(current, list):
            try:
                current = current[step]
            except Exception:
                return default
        elif isinstance(current, dict):
            current = current.get(step)
        else:
            return default
    return current if current is not None else default


def _coerce_releases(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("releases", "items", "value", "data", "results", "records"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _guess_document_url(release: dict[str, Any]) -> str | None:
    for doc in (_get(release, "tender", "documents", default=[]) or []):
        if isinstance(doc, dict) and doc.get("url"):
            return str(doc["url"])
    for doc in (release.get("documents") or []):
        if isinstance(doc, dict) and doc.get("url"):
            return str(doc["url"])
    return None


def _guess_source_url(release: dict[str, Any]) -> str | None:
    return _first_non_empty(
        release.get("uri"),
        release.get("url"),
        _guess_document_url(release),
    )


def _guess_province(release: dict[str, Any]) -> str | None:
    parties = release.get("parties") or []
    for party in parties:
        if not isinstance(party, dict):
            continue
        address = party.get("address") or {}
        region = _first_non_empty(address.get("region"), address.get("locality"))
        if region:
            return str(region)
    buyer = release.get("buyer") or {}
    address = buyer.get("address") or {}
    return _first_non_empty(address.get("region"), address.get("locality"))


def _map_release_to_tender(release: dict[str, Any]) -> dict[str, Any]:
    tender = release.get("tender") or {}
    ocid = release.get("ocid")
    release_id = _first_non_empty(release.get("id"), release.get("releaseID"))
    tender_uid = _first_non_empty(
        ocid and release_id and f"{ocid}::{release_id}",
        ocid,
        release_id,
        _guess_source_url(release),
        tender.get("title"),
    )
    title = _first_non_empty(tender.get("title"), release.get("title"), ocid, "Untitled tender")
    description = _first_non_empty(tender.get("description"), release.get("description"), "")
    buyer = release.get("buyer") or {}
    buyer_name = _first_non_empty(buyer.get("name"), _get(release, "parties", 0, "name"), "Unknown buyer")
    province = _guess_province(release)
    industry = _first_non_empty(
        _get(tender, "classification", "description"),
        tender.get("mainProcurementCategory"),
        release.get("mainProcurementCategory"),
        _get(release, "tag", 0),
    )
    tender_type = _first_non_empty(
        tender.get("procurementMethodDetails"),
        tender.get("procurementMethod"),
        release.get("initiationType"),
        _get(release, "tag", 0),
    )
    status = _first_non_empty(tender.get("status"), release.get("tag", [None])[0], release.get("initiationType"))
    issued_date = _parse_date(
        _first_non_empty(
            release.get("date"),
            _get(tender, "tenderPeriod", "startDate"),
            tender.get("datePublished"),
        )
    )
    closing_date = _parse_date(
        _first_non_empty(
            _get(tender, "tenderPeriod", "endDate"),
            tender.get("closingDate"),
            _get(tender, "enquiryPeriod", "endDate"),
        )
    )
    document_url = _guess_document_url(release)
    source_url = _guess_source_url(release)

    return {
        "tender_uid": str(tender_uid)[:255] if tender_uid else None,
        "ocid": str(ocid)[:255] if ocid else None,
        "source_release_id": str(release_id)[:255] if release_id else None,
        "title": str(title)[:500],
        "description": str(description)[:200000],
        "buyer_name": str(buyer_name)[:255] if buyer_name else None,
        "province": str(province)[:100] if province else None,
        "tender_type": str(tender_type)[:100] if tender_type else None,
        "industry": str(industry)[:100] if industry else None,
        "status": str(status)[:50] if status else None,
        "issued_date": issued_date,
        "closing_date": closing_date,
        "document_url": str(document_url)[:4000] if document_url else None,
        "source_url": str(source_url)[:4000] if source_url else None,
        "raw_json": json.dumps(release, ensure_ascii=False)[:2_000_000],
        "is_live": (closing_date is None) or (closing_date >= date.today()),
    }


def _find_existing_tender(session, mapped: dict[str, Any]):
    if mapped.get("tender_uid"):
        existing = session.execute(
            select(TenderCache).where(TenderCache.tender_uid == mapped["tender_uid"]).limit(1)
        ).scalars().first()
        if existing:
            return existing
    if mapped.get("ocid"):
        existing = session.execute(
            select(TenderCache).where(TenderCache.ocid == mapped["ocid"]).limit(1)
        ).scalars().first()
        if existing:
            return existing
    if mapped.get("source_url"):
        existing = session.execute(
            select(TenderCache).where(TenderCache.source_url == mapped["source_url"]).limit(1)
        ).scalars().first()
        if existing:
            return existing
    return None


def _upsert_release(session, release: dict[str, Any]) -> tuple[bool, bool]:
    mapped = _map_release_to_tender(release)
    if not mapped.get("tender_uid") or not mapped.get("title"):
        return False, False

    existing = _find_existing_tender(session, mapped)
    inserted = False
    if existing is None:
        existing = TenderCache()
        session.add(existing)
        inserted = True

    for field in (
        "tender_uid",
        "ocid",
        "source_release_id",
        "title",
        "description",
        "buyer_name",
        "province",
        "tender_type",
        "industry",
        "status",
        "issued_date",
        "closing_date",
        "document_url",
        "source_url",
        "raw_json",
        "is_live",
    ):
        if hasattr(existing, field):
            setattr(existing, field, mapped.get(field))
    if hasattr(existing, "last_seen_at"):
        existing.last_seen_at = utcnow()
    if hasattr(existing, "updated_at"):
        existing.updated_at = utcnow()

    return inserted, True


def _fetch_live_api_page(client: requests.Session, page: int, page_size: int) -> list[dict[str, Any]]:
    response = client.get(
        LIVE_API_URL,
        params={"PageNumber": page, "PageSize": page_size},
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return _coerce_releases(response.json())


def _discover_bulk_json_urls(client: requests.Session) -> list[str]:
    response = client.get(
        RELEASES_FILES_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    html = response.text
    urls = []
    for href in JSON_LINK_RE.findall(html):
        absolute = urljoin(RELEASES_FILES_URL, href)
        if absolute not in urls:
            urls.append(absolute)
    # newest files usually have later YYYY/MM or YYYY-MM in the filename; reverse-sorted is a decent fallback
    urls = sorted(urls, reverse=True)
    return urls[:MAX_BULK_FILES]


def _iter_bulk_releases(client: requests.Session) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    releases: list[dict[str, Any]] = []
    urls = _discover_bulk_json_urls(client)
    if not urls:
        raise RuntimeError("Could not discover any monthly OCDS JSON files from ReleasesFiles page.")
    for url in urls:
        r = client.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json,application/octet-stream,*/*"},
            timeout=max(TIMEOUT, 120),
        )
        r.raise_for_status()
        payload = r.json()
        chunk = _coerce_releases(payload)
        if not chunk:
            warnings.append(f"No releases found in bulk file: {url}")
            continue
        releases.extend(chunk)
    return releases, warnings


def ingest_tenders(session, max_pages: int | None = None, page_size: int | None = None) -> dict[str, Any]:
    max_pages = max_pages or MAX_PAGES
    page_size = page_size or PAGE_SIZE

    ingest_run = IngestRun(
        status="running",
        started_at=utcnow(),
        pages_attempted=0,
        pages_succeeded=0,
        tenders_seen=0,
        tenders_upserted=0,
        failure_message=None,
    )
    session.add(ingest_run)
    session.flush()

    inserted = 0
    updated = 0
    seen = 0
    pages_attempted = 0
    pages_succeeded = 0
    errors: list[str] = []
    mode = "live_api"

    try:
        with requests.Session() as client:
            live_failed = False
            for page in range(1, max_pages + 1):
                pages_attempted += 1
                try:
                    releases = _fetch_live_api_page(client, page, page_size)
                except requests.HTTPError as exc:
                    status = exc.response.status_code if exc.response is not None else "?"
                    errors.append(f"page {page}: {status} {exc}")
                    if page == 1:
                        live_failed = True
                        break
                    else:
                        raise
                except Exception as exc:
                    errors.append(f"page {page}: {exc}")
                    if page == 1:
                        live_failed = True
                        break
                    raise

                if not releases:
                    break

                pages_succeeded += 1
                for release in releases:
                    seen += 1
                    was_inserted, was_upserted = _upsert_release(session, release)
                    if was_upserted:
                        if was_inserted:
                            inserted += 1
                        else:
                            updated += 1
                session.flush()

            if live_failed:
                mode = "bulk_fallback"
                releases, warnings = _iter_bulk_releases(client)
                errors.extend(warnings)
                pages_succeeded = max(pages_succeeded, 1)
                for release in releases:
                    seen += 1
                    was_inserted, was_upserted = _upsert_release(session, release)
                    if was_upserted:
                        if was_inserted:
                            inserted += 1
                        else:
                            updated += 1
                session.flush()

        ingest_run.status = "success" if not errors else "partial_success"
        ingest_run.pages_attempted = pages_attempted
        ingest_run.pages_succeeded = pages_succeeded
        ingest_run.tenders_seen = seen
        ingest_run.tenders_upserted = inserted + updated
        ingest_run.failure_message = "; ".join(errors)[:4000] if errors else None
        ingest_run.finished_at = utcnow()

        return {
            "ok": True,
            "status": ingest_run.status,
            "mode": mode,
            "base_url": LIVE_API_URL,
            "page_size": page_size,
            "pages_attempted": pages_attempted,
            "pages_succeeded": pages_succeeded,
            "tenders_seen": seen,
            "tenders_upserted": inserted + updated,
            "inserted": inserted,
            "updated": updated,
            "errors": errors,
        }
    except Exception as exc:
        ingest_run.status = "failed"
        ingest_run.pages_attempted = pages_attempted
        ingest_run.pages_succeeded = pages_succeeded
        ingest_run.tenders_seen = seen
        ingest_run.tenders_upserted = inserted + updated
        ingest_run.failure_message = str(exc)[:4000]
        ingest_run.finished_at = utcnow()
        return {
            "ok": False,
            "status": "failed",
            "mode": mode,
            "base_url": LIVE_API_URL,
            "page_size": page_size,
            "pages_attempted": pages_attempted,
            "pages_succeeded": pages_succeeded,
            "tenders_seen": seen,
            "tenders_upserted": inserted + updated,
            "inserted": inserted,
            "updated": updated,
            "error": str(exc),
        }
