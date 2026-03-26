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
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()

    text = str(value).strip()
    if not text:
        return None

    try:
        # supports YYYY-MM-DD and many ISO datetime forms
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).date()
    except Exception:
        pass

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


def listify(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def first_non_empty(*values: Any) -> Any:
    for v in values:
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        return v
    return None


def get_deep(obj: Any, path: list[Any]) -> Any:
    cur = obj
    for part in path:
        if isinstance(part, int):
            if not isinstance(cur, list) or part >= len(cur):
                return None
            cur = cur[part]
        else:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(part)
        if cur is None:
            return None
    return cur


def extract_releases(payload: dict) -> list[dict]:
    """
    Supports common OCDS response shapes:
    - {"releases":[...]}
    - {"records":[{"releases":[...]}]}
    - {"records":[{"compiledRelease":{...}}]}
    - {"data":[...]}   (fallback for non-standard wrappers)
    """
    releases: list[dict] = []
    if not isinstance(payload, dict):
        return releases

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
            compiled = rec.get("compiledRelease")
            if isinstance(compiled, dict):
                releases.append(compiled)

    data = payload.get("data")
    if isinstance(data, list):
        releases.extend([r for r in data if isinstance(r, dict)])

    # de-duplicate by (ocid,id)
    dedup = {}
    for r in releases:
        key = f"{r.get('ocid','')}::{r.get('id','')}"
        dedup[key] = r
    return list(dedup.values())


def normalize_release(release: dict) -> dict | None:
    if not isinstance(release, dict):
        return None

    tender = release.get("tender") or {}
    planning = release.get("planning") or {}
    buyer = release.get("buyer") or {}
    parties = listify(release.get("parties"))

    ocid = clean_text(release.get("ocid"), 255)
    release_id = clean_text(release.get("id"), 255)
    if not ocid and not release_id:
        return None

    # stable unique key for upsert
    tender_uid = clean_text(first_non_empty(ocid, release_id), 255)
    if not tender_uid:
        return None

    title = clean_text(
        first_non_empty(
            tender.get("title"),
            release.get("title"),
            get_deep(planning, ["budget", "description"]),
            "Untitled tender",
        ),
        500,
    )

    description = clean_text(
        first_non_empty(
            tender.get("description"),
            release.get("description"),
            planning.get("rationale"),
        )
    )

    buyer_name = clean_text(
        first_non_empty(
            buyer.get("name"),
            get_deep(tender, ["procuringEntity", "name"]),
        ),
        255,
    )

    if not buyer_name:
        for p in parties:
            if not isinstance(p, dict):
                continue
            roles = listify(p.get("roles"))
            if "buyer" in roles:
                buyer_name = clean_text(p.get("name"), 255)
                if buyer_name:
                    break

    tender_type = clean_text(
        first_non_empty(
            tender.get("procurementMethodDetails"),
            tender.get("mainProcurementCategory"),
            tender.get("procurementMethod"),
        ),
        100,
    )

    industry = clean_text(
        first_non_empty(
            tender.get("mainProcurementCategory"),
            get_deep(tender, ["classification", "description"]),
            tender_type,
        ),
        100,
    )

    status = clean_text(first_non_empty(tender.get("status"), release.get("tag")), 50)

    issued_date = parse_date(
        first_non_empty(
            tender.get("datePublished"),
            release.get("date"),
            tender.get("publicationDate"),
            get_deep(tender, ["tenderPeriod", "startDate"]),
        )
    )

    closing_date = parse_date(
        first_non_empty(
            get_deep(tender, ["tenderPeriod", "endDate"]),
            tender.get("submissionDeadline"),
            tender.get("closingDate"),
        )
    )

    source_url = clean_text(
        first_non_empty(
            release.get("url"),
            tender.get("url"),
        )
    )

    # pick first available tender document URL
    document_url = None
    for d in listify(tender.get("documents")):
        if not isinstance(d, dict):
            continue
        maybe = clean_text(first_non_empty(d.get("url"), d.get("documentUrl")))
        if maybe:
            document_url = maybe
            break

    # best effort province from delivery or locality-like fields
    province = clean_text(
        first_non_empty(
            get_deep(tender, ["deliveryAddress", "region"]),
            get_deep(tender, ["deliveryAddress", "locality"]),
            get_deep(tender, ["procuringEntity", "address", "region"]),
        ),
        100,
    )

    # decide live status
    lowered_status = (status or "").lower()
    is_live = lowered_status not in {"cancelled", "canceled", "unsuccessful", "complete", "completed", "withdrawn"}

    return {
        "tender_uid": tender_uid,
        "ocid": ocid,
        "source_release_id": release_id,
        "title": title or "Untitled tender",
        "description": description,
        "buyer_name": buyer_name,
        "province": province,
        "tender_type": tender_type,
        "industry": industry,
        "status": status,
        "issued_date": issued_date,
        "closing_date": closing_date,
        "document_url": document_url,
        "source_url": source_url,
        "raw_json": json.dumps(release, ensure_ascii=False),
        "is_live": is_live,
    }


def build_request_attempts(page: int, page_size: int) -> list[dict]:
    """
    Try multiple common pagination parameter shapes to reduce 400 failures.
    """
    return [
        {"page": page, "size": page_size},
        {"page": page, "limit": page_size},
        {"offset": (page - 1) * page_size, "limit": page_size},
    ]


def fetch_page(base_url: str, page: int, page_size: int, timeout: int = 30) -> tuple[dict | None, str | None]:
    last_error = None
    for params in build_request_attempts(page=page, page_size=page_size):
        try:
            resp = requests.get(base_url, params=params, timeout=timeout)
        except Exception as exc:
            last_error = f"Request exception with params={params}: {exc}"
            continue

        if resp.status_code == 400:
            last_error = f"400 Bad Request with params={params}; url={resp.url}; body={resp.text[:700]}"
            continue

        if resp.status_code >= 300:
            last_error = f"HTTP {resp.status_code} with params={params}; url={resp.url}; body={resp.text[:700]}"
            continue

        try:
            return resp.json(), None
        except Exception as exc:
            last_error = f"Invalid JSON with params={params}: {exc}"
            continue

    return None, last_error


def upsert_tender(session, data: dict, now: datetime) -> bool:
    """
    Returns True if inserted/updated, False if unchanged.
    """
    existing = session.execute(
        select(TenderCache).where(TenderCache.tender_uid == data["tender_uid"])
    ).scalars().first()

    if existing is None:
        row = TenderCache(
            tender_uid=data["tender_uid"],
            ocid=data.get("ocid"),
            source_release_id=data.get("source_release_id"),
            title=data["title"],
            description=data.get("description"),
            buyer_name=data.get("buyer_name"),
            province=data.get("province"),
            tender_type=data.get("tender_type"),
            industry=data.get("industry"),
            status=data.get("status"),
            issued_date=data.get("issued_date"),
            closing_date=data.get("closing_date"),
            document_url=data.get("document_url"),
            source_url=data.get("source_url"),
            raw_json=data.get("raw_json"),
            is_live=bool(data.get("is_live", True)),
            last_seen_at=now,
            updated_at=now,
        )
        session.add(row)
        return True

    changed = False
    fields = [
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
    ]
    for field in fields:
        new_value = data.get(field)
        if getattr(existing, field) != new_value:
            setattr(existing, field, new_value)
            changed = True

    # always refresh last_seen_at when encountered
    if existing.last_seen_at != now:
        existing.last_seen_at = now
        changed = True

    existing.updated_at = now
    return changed


def mark_expired(session, now: datetime) -> int:
    """
    Mark tenders not seen in this run as not live.
    """
    stale = session.execute(
        select(TenderCache).where(
            and_(
                TenderCache.is_live.is_(True),
                TenderCache.last_seen_at < now,
            )
        )
    ).scalars().all()

    count = 0
    for row in stale:
        row.is_live = False
        row.status = row.status or "expired"
        row.updated_at = now
        count += 1
    return count


def ingest_tenders(session, max_pages: int = 1) -> dict:
    """
    Ingest tender releases from OCDS API into TenderCache with upsert + run tracking.
    Compatible with current models.py.
    """
    base_url = os.getenv("ETENDERS_OCDS_URL", "").strip()
    page_size = int(os.getenv("ETENDERS_PAGE_SIZE", "100"))
    timeout = int(os.getenv("ETENDERS_HTTP_TIMEOUT", "30"))

    if not base_url:
        return {"ok": False, "error": "ETENDERS_OCDS_URL is not set"}

    run = IngestRun(
        status="running",
        pages_attempted=0,
        pages_succeeded=0,
        tenders_seen=0,
        tenders_upserted=0,
        failure_message=None,
        started_at=utcnow(),
        finished_at=None,
    )
    session.add(run)
    session.flush()

    run_started_marker = utcnow()
    last_error = None

    try:
        for page in range(1, max_pages + 1):
            run.pages_attempted += 1
            payload, err = fetch_page(base_url=base_url, page=page, page_size=page_size, timeout=timeout)

            if err:
                last_error = f"Page {page}: {err}"
                break

            releases = extract_releases(payload or {})
            if not releases:
                # normal end of pages
                break

            run.pages_succeeded += 1

            page_seen = 0
            for release in releases:
                normalized = normalize_release(release)
                if not normalized:
                    continue

                page_seen += 1
                run.tenders_seen += 1

                changed = upsert_tender(session, normalized, run_started_marker)
                if changed:
                    run.tenders_upserted += 1

            # if data smaller than page size, probably last page
            if len(releases) < page_size:
                break

        # expire only if at least one page succeeded
        expired_count = 0
        if run.pages_succeeded > 0:
            expired_count = mark_expired(session, run_started_marker)

        run.status = "success" if run.pages_succeeded > 0 else "failed"
        run.failure_message = None if run.status == "success" else (last_error or "No pages succeeded")
        run.finished_at = utcnow()
        session.flush()

        return {
            "ok": run.status == "success",
            "status": run.status,
            "run_id": run.id,
            "pages_attempted": run.pages_attempted,
            "pages_succeeded": run.pages_succeeded,
            "tenders_seen": run.tenders_seen,
            "tenders_upserted": run.tenders_upserted,
            "expired_marked": expired_count,
            "failure_message": run.failure_message,
        }

    except Exception as exc:
        run.status = "failed"
        run.failure_message = f"Unexpected ingest error: {exc}"
        run.finished_at = utcnow()
        session.flush()
        return {
            "ok": False,
            "status": "failed",
            "run_id": run.id,
            "pages_attempted": run.pages_attempted,
            "pages_succeeded": run.pages_succeeded,
            "tenders_seen": run.tenders_seen,
            "tenders_upserted": run.tenders_upserted,
            "failure_message": run.failure_message,
        }
