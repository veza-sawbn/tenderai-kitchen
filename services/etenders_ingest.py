from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from sqlalchemy import select

from models import IngestRun, SystemSetting, TenderCache

DEFAULT_TIMEOUT = int(os.getenv("ETENDERS_HTTP_TIMEOUT", "90"))
DEFAULT_PAGE_SIZE = int(os.getenv("ETENDERS_PAGE_SIZE", "10"))
DEFAULT_BASE_URL = os.getenv(
    "ETENDERS_OCDS_URL",
    "https://ocds-api.etenders.gov.za/api/OCDSReleases",
)
MAINTENANCE_DAYS_BACK = int(os.getenv("ETENDERS_MAINTENANCE_DAYS_BACK", "14"))
MAINTENANCE_MAX_PAGES = int(os.getenv("ETENDERS_MAINTENANCE_MAX_PAGES", "1"))
BACKFILL_DAYS_BACK = int(os.getenv("ETENDERS_BACKFILL_DAYS_BACK", "365"))
BACKFILL_PAGES_PER_RUN = int(os.getenv("ETENDERS_BACKFILL_PAGES_PER_RUN", "2"))
BACKFILL_START_PAGE_DEFAULT = int(os.getenv("ETENDERS_BACKFILL_START_PAGE_DEFAULT", "1"))
DEFAULT_USER_AGENT = os.getenv(
    "ETENDERS_USER_AGENT",
    "TenderAI/1.0 (+https://visitdrakensberg.com)"
)
DEFAULT_RETRIES = int(os.getenv("ETENDERS_RETRIES", "3"))
DEFAULT_RETRY_SLEEP = float(os.getenv("ETENDERS_RETRY_SLEEP", "2"))
BACKFILL_CURSOR_KEY = "etenders_backfill_next_page"


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
        return json.dumps(data, ensure_ascii=False, default=str)
    except Exception:
        return json.dumps({"unserializable": True}, ensure_ascii=False)


def _parse_date(value: Any) -> Optional[date]:
    s = _safe_str(value)
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    for candidate in (s, s[:10]):
        try:
            if len(candidate) == 10:
                return datetime.strptime(candidate, "%Y-%m-%d").date()
            return datetime.fromisoformat(candidate).date()
        except Exception:
            continue
    return None


def _extract_release_list(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("releases", "Releases", "data", "Data", "value", "Value"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    return []


def _tender_obj(release: Dict[str, Any]) -> Dict[str, Any]:
    tender = release.get("tender")
    return tender if isinstance(tender, dict) else {}


def _extract_status(release: Dict[str, Any]) -> Optional[str]:
    tender = _tender_obj(release)
    value = _safe_str(tender.get("status"))
    if value:
        return value.lower()
    tag = release.get("tag")
    if isinstance(tag, list) and tag:
        first = _safe_str(tag[0])
        return first.lower() if first else None
    tag_val = _safe_str(tag)
    return tag_val.lower() if tag_val else None


def _build_uid(release: Dict[str, Any]) -> str:
    tender = _tender_obj(release)
    for candidate in (
        tender.get("id"),
        release.get("id"),
        release.get("ocid"),
        tender.get("title"),
    ):
        value = _safe_str(candidate)
        if value:
            return value[:255]
    return f"generated-{datetime.now(timezone.utc).timestamp()}"[:255]


def _extract_title(release: Dict[str, Any]) -> Optional[str]:
    tender = _tender_obj(release)
    return _safe_str(tender.get("title")) or _safe_str(tender.get("description")) or _safe_str(release.get("ocid"))


def _extract_description(release: Dict[str, Any]) -> Optional[str]:
    tender = _tender_obj(release)
    return _safe_str(tender.get("description"))


def _extract_buyer_name(release: Dict[str, Any]) -> Optional[str]:
    buyer = release.get("buyer")
    if isinstance(buyer, dict):
        return _safe_str(buyer.get("name"))
    return None


def _extract_province(release: Dict[str, Any]) -> Optional[str]:
    tender = _tender_obj(release)
    for key in ("province", "region"):
        value = _safe_str(tender.get(key))
        if value:
            return value
    buyer = release.get("buyer")
    if isinstance(buyer, dict):
        address = buyer.get("address")
        if isinstance(address, dict):
            region = _safe_str(address.get("region"))
            if region:
                return region
    return None


def _extract_tender_type(release: Dict[str, Any]) -> Optional[str]:
    tender = _tender_obj(release)
    for key in ("procurementMethodDetails", "mainProcurementCategory", "procurementMethod"):
        value = _safe_str(tender.get(key))
        if value:
            return value
    return None


def _extract_industry(release: Dict[str, Any]) -> Optional[str]:
    tender = _tender_obj(release)
    items = tender.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            classification = item.get("classification")
            if isinstance(classification, dict):
                value = _safe_str(classification.get("description"))
                if value:
                    return value
    return _safe_str(tender.get("mainProcurementCategory"))


def _extract_issued_date(release: Dict[str, Any]) -> Optional[date]:
    tender = _tender_obj(release)
    return _parse_date(tender.get("datePublished")) or _parse_date(release.get("date"))


def _extract_closing_date(release: Dict[str, Any]) -> Optional[date]:
    tender = _tender_obj(release)
    tp = tender.get("tenderPeriod")
    if isinstance(tp, dict):
        parsed = _parse_date(tp.get("endDate"))
        if parsed:
            return parsed
    bo = tender.get("bidOpening")
    if isinstance(bo, dict):
        parsed = _parse_date(bo.get("date"))
        if parsed:
            return parsed
    return None


def _extract_source_url(release: Dict[str, Any]) -> Optional[str]:
    links = release.get("links")
    if isinstance(links, dict):
        for key in ("self", "compiledRelease", "releasePackage"):
            val = links.get(key)
            if isinstance(val, dict):
                href = _safe_str(val.get("href"))
                if href:
                    return href
            else:
                href = _safe_str(val)
                if href:
                    return href
    ocid = _safe_str(release.get("ocid"))
    if ocid:
        return f"{DEFAULT_BASE_URL}/release/{ocid}"
    return None


def _extract_document_url(release: Dict[str, Any]) -> Optional[str]:
    tender = _tender_obj(release)
    docs = tender.get("documents")
    if isinstance(docs, list):
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            for key in ("url", "downloadUrl"):
                value = _safe_str(doc.get(key))
                if value:
                    return value
    return None


def _is_live_tender(release: Dict[str, Any], closing_date: Optional[date]) -> bool:
    today = _today()
    status = _extract_status(release)
    if closing_date is not None:
        return closing_date >= today
    return status in {"active", "planning", "planned", "published", "tender", "open"}


def fetch_release_page(page_number: int, page_size: int, date_from: str, date_to: str, timeout: int = DEFAULT_TIMEOUT, base_url: str = DEFAULT_BASE_URL, retries: int = DEFAULT_RETRIES) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    headers = {"Accept": "application/json", "User-Agent": DEFAULT_USER_AGENT}
    params = {"dateFrom": date_from, "dateTo": date_to, "PageNumber": page_number, "PageSize": page_size}
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(base_url, params=params, headers=headers, timeout=timeout)
            response.raise_for_status()
            payload = response.json()
            return payload, _extract_release_list(payload)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(DEFAULT_RETRY_SLEEP * attempt)
                continue
            raise
    raise last_exc


def upsert_release(session, release: Dict[str, Any]) -> Tuple[str, str]:
    tender_uid = _build_uid(release)
    closing_date = _extract_closing_date(release)
    existing = session.execute(select(TenderCache).where(TenderCache.tender_uid == tender_uid)).scalar_one_or_none()
    values = {
        "tender_uid": tender_uid,
        "title": _extract_title(release) or tender_uid,
        "description": _extract_description(release),
        "buyer_name": _extract_buyer_name(release),
        "province": _extract_province(release),
        "tender_type": _extract_tender_type(release),
        "industry": _extract_industry(release),
        "issued_date": _extract_issued_date(release),
        "closing_date": closing_date,
        "document_url": _extract_document_url(release),
        "source_url": _extract_source_url(release),
        "is_live": _is_live_tender(release, closing_date),
    }
    if existing is None:
        session.add(TenderCache(**values))
        return "inserted", tender_uid
    for key, value in values.items():
        setattr(existing, key, value)
    return "updated", tender_uid


def _create_ingest_run(session) -> IngestRun:
    ingest_run = IngestRun(status="running")
    session.add(ingest_run)
    session.flush()
    return ingest_run


def _mark_expired_tenders_not_live(session):
    today = _today()
    stale = session.execute(select(TenderCache).where(TenderCache.closing_date.is_not(None), TenderCache.closing_date < today, TenderCache.is_live.is_(True))).scalars().all()
    count = 0
    for tender in stale:
        tender.is_live = False
        count += 1
    return count


def _get_setting(session, key: str, default: str) -> str:
    row = session.get(SystemSetting, key)
    if not row:
        row = SystemSetting(key=key, value=default)
        session.add(row)
        session.flush()
        return default
    return row.value or default


def _set_setting(session, key: str, value: str) -> None:
    row = session.get(SystemSetting, key)
    if not row:
        row = SystemSetting(key=key, value=value)
        session.add(row)
    else:
        row.value = value


def _run_window(session, *, date_from: str, date_to: str, start_page: int, pages_to_run: int, page_size: int, timeout: int, base_url: str) -> Dict[str, Any]:
    stats = {"start_page": start_page, "pages_requested": pages_to_run, "pages_attempted": 0, "pages_succeeded": 0, "tenders_seen": 0, "tenders_upserted": 0, "inserted": 0, "updated": 0, "ended_on_page": start_page - 1, "next_page": start_page, "hit_end": False}
    current_page = start_page
    for _ in range(pages_to_run):
        stats["pages_attempted"] += 1
        _payload, releases = fetch_release_page(page_number=current_page, page_size=page_size, date_from=date_from, date_to=date_to, timeout=timeout, base_url=base_url)
        if not releases:
            stats["hit_end"] = True
            stats["next_page"] = BACKFILL_START_PAGE_DEFAULT
            break
        stats["pages_succeeded"] += 1
        stats["tenders_seen"] += len(releases)
        stats["ended_on_page"] = current_page
        for release in releases:
            action, _uid = upsert_release(session, release)
            stats["tenders_upserted"] += 1
            if action == "inserted":
                stats["inserted"] += 1
            else:
                stats["updated"] += 1
        current_page += 1
        stats["next_page"] = current_page
        if len(releases) < page_size:
            stats["hit_end"] = True
            stats["next_page"] = BACKFILL_START_PAGE_DEFAULT
            break
    return stats


def run_ingest(session, page_size: int = DEFAULT_PAGE_SIZE, timeout: int = DEFAULT_TIMEOUT, base_url: str = DEFAULT_BASE_URL) -> Dict[str, Any]:
    today = _today()
    maintenance_date_from = (today - timedelta(days=MAINTENANCE_DAYS_BACK)).isoformat()
    maintenance_date_to = today.isoformat()
    backfill_date_from = (today - timedelta(days=BACKFILL_DAYS_BACK)).isoformat()
    backfill_date_to = today.isoformat()
    next_backfill_page = int(_get_setting(session, BACKFILL_CURSOR_KEY, str(BACKFILL_START_PAGE_DEFAULT)))
    stats: Dict[str, Any] = {"ok": True, "status": "success", "page_size": page_size, "maintenance": {}, "backfill": {}, "expired_marked_not_live": 0, "backfill_cursor_before": next_backfill_page, "backfill_cursor_after": next_backfill_page, "error": None}
    ingest_run = _create_ingest_run(session)
    try:
        maintenance_stats = _run_window(session, date_from=maintenance_date_from, date_to=maintenance_date_to, start_page=1, pages_to_run=MAINTENANCE_MAX_PAGES, page_size=page_size, timeout=timeout, base_url=base_url)
        stats["maintenance"] = maintenance_stats
        backfill_stats = _run_window(session, date_from=backfill_date_from, date_to=backfill_date_to, start_page=next_backfill_page, pages_to_run=BACKFILL_PAGES_PER_RUN, page_size=page_size, timeout=timeout, base_url=base_url)
        stats["backfill"] = backfill_stats
        _set_setting(session, BACKFILL_CURSOR_KEY, str(backfill_stats["next_page"]))
        stats["backfill_cursor_after"] = backfill_stats["next_page"]
        stats["expired_marked_not_live"] = _mark_expired_tenders_not_live(session)
        ingest_run.status = "success"
        ingest_run.finished_at = datetime.now(timezone.utc)
        ingest_run.result_json = _safe_json(stats)
        session.add(ingest_run)
        return stats
    except Exception as exc:
        stats["ok"] = False
        stats["status"] = "failed"
        stats["error"] = str(exc)
        try:
            ingest_run.status = "failed"
            ingest_run.finished_at = datetime.now(timezone.utc)
            ingest_run.result_json = _safe_json(stats)
            session.add(ingest_run)
        except Exception:
            pass
        raise


def ingest_tenders(session, page_size: int = DEFAULT_PAGE_SIZE, timeout: int = DEFAULT_TIMEOUT, base_url: str = DEFAULT_BASE_URL, **_: Any) -> Dict[str, Any]:
    return run_ingest(session=session, page_size=page_size, timeout=timeout, base_url=base_url)
