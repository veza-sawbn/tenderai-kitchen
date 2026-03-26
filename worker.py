from dotenv import load_dotenv
import json
import os
from sqlalchemy import desc, select
from sqlalchemy.orm import selectinload

from database import get_db_session, init_db
from models import Profile, TenderCache, TenderDocumentCache
from services.document_fetcher import fetch_documents_for_tenders
from services.etenders_ingest import ingest_tenders
from services.openai_extractors import parse_supplier_profile_text, parse_tender_document_text


load_dotenv()

def run_worker() -> dict:
    init_db()
    report = {"ingest": None, "documents_fetched": 0, "documents_parsed": 0, "profiles_reparsed": 0, "errors": []}
    with get_db_session() as session:
        report["ingest"] = ingest_tenders(session=session, max_pages=int(os.getenv("INGEST_MAX_PAGES", "2")))
        tenders = session.execute(select(TenderCache).options(selectinload(TenderCache.documents)).where(TenderCache.is_live.is_(True)).order_by(TenderCache.closing_date.asc().nulls_last(), desc(TenderCache.updated_at)).limit(int(os.getenv("WORKER_FETCH_LIMIT", "25")))).scalars().all()
        fetched = fetch_documents_for_tenders(session, tenders)
        report["documents_fetched"] = len([x for x in fetched if x.get("ok")])
        docs = session.execute(select(TenderDocumentCache).options(selectinload(TenderDocumentCache.tender)).where(TenderDocumentCache.fetch_status.in_(["fetched", "fetched_no_text"]), TenderDocumentCache.parsed_json.is_(None)).order_by(desc(TenderDocumentCache.updated_at)).limit(int(os.getenv("WORKER_PARSE_LIMIT", "25")))).scalars().all()
        for doc in docs:
            try:
                parsed = parse_tender_document_text({
                    "title": doc.tender.title if doc.tender else "",
                    "buyer_name": doc.tender.buyer_name if doc.tender else "",
                    "province": doc.tender.province if doc.tender else "",
                    "closing_date": str(doc.tender.closing_date) if doc.tender and doc.tender.closing_date else "",
                    "source_url": doc.tender.source_url if doc.tender else "",
                }, doc.extracted_text or "")
                if parsed:
                    doc.parsed_json = json.dumps(parsed, ensure_ascii=False)
                    report["documents_parsed"] += 1
            except Exception as exc:
                doc.error_message = str(exc)[:5000]
                report["errors"].append(f"doc {doc.id}: {exc}")
        active_profile = session.execute(select(Profile).where(Profile.is_active.is_(True)).order_by(desc(Profile.updated_at)).limit(1)).scalars().first()
        if active_profile and active_profile.extracted_text:
            parsed_profile = parse_supplier_profile_text(active_profile.extracted_text, active_profile.original_filename)
            if parsed_profile:
                active_profile.company_name = parsed_profile.get("company_name") or active_profile.company_name
                active_profile.industry = parsed_profile.get("industry") or active_profile.industry
                active_profile.capabilities_text = ", ".join(parsed_profile.get("capabilities") or [])
                active_profile.locations_text = ", ".join(parsed_profile.get("locations") or [])
                active_profile.parsed_json = json.dumps(parsed_profile, ensure_ascii=False)
                report["profiles_reparsed"] += 1
    return report


if __name__ == "__main__":
    print(json.dumps(run_worker(), ensure_ascii=False, indent=2, default=str))
