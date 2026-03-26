from __future__ import annotations

import io
import os
import re
from datetime import datetime, timezone

from dotenv import load_dotenv
from html import unescape
from urllib.parse import urljoin

import requests

from models import TenderCache, TenderDocumentCache

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

try:
    from pypdf import PdfReader
except Exception:
    try:
        from PyPDF2 import PdfReader
    except Exception:
        PdfReader = None

try:
    from docx import Document
except Exception:
    Document = None

USER_AGENT = os.getenv("TENDERAI_USER_AGENT", "TenderAI/1.0")
TIMEOUT = int(os.getenv("TENDER_FETCH_TIMEOUT_SECONDS", "35"))
MAX_BYTES = int(os.getenv("TENDER_FETCH_MAX_BYTES", str(15 * 1024 * 1024)))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_filename(url: str | None, content_type: str | None) -> str | None:
    if not url:
        return None
    name = url.rstrip("/").split("/")[-1].split("?")[0].strip()
    if name:
        return name[:255]
    if content_type and "pdf" in content_type.lower():
        return "document.pdf"
    return None


def _extract_pdf_text(binary: bytes) -> str:
    if PdfReader is None:
        return ""
    try:
        reader = PdfReader(io.BytesIO(binary))
        pages = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(pages).strip()
    except Exception:
        return ""


def _extract_docx_text(binary: bytes) -> str:
    if Document is None:
        return ""
    try:
        doc = Document(io.BytesIO(binary))
        return "\n".join([p.text for p in doc.paragraphs if p.text]).strip()
    except Exception:
        return ""


def _html_to_text(html: str) -> str:
    if BeautifulSoup is not None:
        try:
            return BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
        except Exception:
            pass
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.I | re.S)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.I | re.S)
    html = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", unescape(html)).strip()


def _find_download_link_from_html(url: str, html: str) -> str | None:
    if BeautifulSoup is not None:
        try:
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                text = (a.get_text(" ", strip=True) or "").lower()
                href_l = href.lower()
                if any(token in href_l for token in [".pdf", ".doc", ".docx"]) or any(token in text for token in ["download", "tender document", "bid document"]):
                    return urljoin(url, href)
        except Exception:
            return None
    return None


def _get_or_create_doc(tender: TenderCache, url: str) -> TenderDocumentCache:
    for doc in tender.documents or []:
        if doc.document_url == url:
            return doc
    doc = TenderDocumentCache(tender_id=tender.id, document_url=url, fetch_status="pending")
    tender.documents.append(doc)
    return doc


def fetch_document_for_tender(session, tender: TenderCache) -> dict:
    candidate_urls = []
    for u in [tender.document_url, tender.source_url]:
        if u and u not in candidate_urls:
            candidate_urls.append(u)
    if not candidate_urls:
        return {"ok": False, "tender_id": tender.id, "error": "no_source_url"}

    headers = {"User-Agent": USER_AGENT, "Accept": "application/pdf,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/html,application/xhtml+xml,*/*"}
    last_error = None
    with requests.Session() as client:
        for url in candidate_urls:
            doc = _get_or_create_doc(tender, url)
            session.add(doc)
            try:
                response = client.get(url, headers=headers, timeout=TIMEOUT, stream=True, allow_redirects=True)
                response.raise_for_status()
                content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                binary = response.content[:MAX_BYTES]
                final_url = response.url or url
                text = ""
                if "pdf" in content_type or final_url.lower().endswith(".pdf"):
                    text = _extract_pdf_text(binary)
                elif "word" in content_type or final_url.lower().endswith(".docx"):
                    text = _extract_docx_text(binary)
                elif "html" in content_type or "text/" in content_type or final_url.lower().endswith(".html"):
                    html = binary.decode("utf-8", errors="ignore")
                    resolved = _find_download_link_from_html(final_url, html)
                    if resolved and resolved != url and resolved not in candidate_urls:
                        candidate_urls.append(resolved)
                    text = _html_to_text(html)
                doc.document_url = final_url
                doc.filename = _safe_filename(final_url, content_type)
                doc.content_type = content_type[:100] if content_type else None
                doc.binary_content = binary
                doc.extracted_text = (text or "")[:500000]
                doc.fetch_status = "fetched" if text else "fetched_no_text"
                doc.error_message = None
                doc.fetched_at = utcnow()
                return {"ok": True, "tender_id": tender.id, "document_id": doc.id, "document_url": doc.document_url, "fetch_status": doc.fetch_status}
            except Exception as exc:
                doc.fetch_status = "failed"
                doc.error_message = str(exc)[:5000]
                last_error = str(exc)
    return {"ok": False, "tender_id": tender.id, "error": last_error or "fetch_failed"}


def fetch_documents_for_tenders(session, tenders: list[TenderCache]) -> list[dict]:
    results = []
    for tender in tenders:
        results.append(fetch_document_for_tender(session, tender))
    return results
