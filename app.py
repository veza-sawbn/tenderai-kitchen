from flask import Flask, jsonify, request
import requests
import re
import io
from pypdf import PdfReader

app = Flask(__name__)


@app.get("/")
def home():
    return {"message": "TenderAI kitchen home v8"}


@app.get("/health")
def health():
    return {"status": "ok"}


def extract_releases(payload):
    if isinstance(payload, list):
        return payload

    if not isinstance(payload, dict):
        return []

    for key in ["releases", "data", "value", "results", "items"]:
        value = payload.get(key)
        if isinstance(value, list):
            return value

    if "ocid" in payload or "tender" in payload or "buyer" in payload:
        return [payload]

    return []


def tokenize(text):
    if not text:
        return []

    words = re.findall(r"[a-zA-Z0-9]+", text.lower())
    stopwords = {
        "the", "and", "for", "with", "from", "that", "this", "are", "was",
        "your", "you", "our", "have", "has", "will", "not", "all", "can",
        "services", "service", "company", "business", "profile", "south",
        "africa", "of", "to", "in", "on", "by", "at", "is", "as", "or",
        "an", "be", "we", "it", "their", "its", "pty", "ltd", "cc",
        "supplier", "summary", "report", "registration", "database",
        "government"
    }
    return [w for w in words if len(w) > 2 and w not in stopwords]


def extract_pdf_text(file_storage):
    pdf_bytes = file_storage.read()
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = []

    for page in reader.pages:
        text = page.extract_text() or ""
        pages.append(text)

    return "\n".join(pages)


def extract_profile_text():
    if "profile_pdf" in request.files:
        uploaded_file = request.files["profile_pdf"]
        if uploaded_file and uploaded_file.filename.lower().endswith(".pdf"):
            return extract_pdf_text(uploaded_file), "pdf"

    body = request.get_json(silent=True) or {}
    profile_text = body.get("profile_text", "")
    return profile_text, "text"


def get_request_value(name, default_value):
    if request.content_type and "multipart/form-data" in request.content_type:
        return request.form.get(name, default_value)

    body = request.get_json(silent=True) or {}
    return body.get(name, default_value)


def score_tender(profile_keywords, tender_text, category=""):
    tender_tokens = set(tokenize(tender_text))
    profile_set = set(profile_keywords)

    matched = sorted(profile_set.intersection(tender_tokens))
    base_score = (len(matched) / max(len(profile_set), 1)) * 100

    bonus = 0

    if category:
        category = category.lower()
        if category in ["works", "services"]:
            bonus += 10

    intent_keywords = ["installation", "maintenance", "repair", "construction"]
    intent_hits = [k for k in intent_keywords if k in tender_tokens]
    bonus += len(intent_hits) * 5

    final_score = round(min(base_score + bonus, 100), 1)

    if final_score >= 70:
        fit_band = "High fit"
    elif final_score >= 40:
        fit_band = "Medium fit"
    else:
        fit_band = "Low fit"

    return final_score, fit_band, matched


def estimate_tender_value(title, description, category):
    text = f"{title} {description}".lower()

    low = 50000
    high = 300000
    confidence = "Low"
    reason = "Generic service estimate based on tender wording."

    if "generator" in text:
        low = 800000
        high = 3000000
        confidence = "Medium"
        reason = "Generator installations typically fall within this range."

    elif any(k in text for k in ["construction", "building", "infrastructure"]):
        low = 500000
        high = 5000000
        confidence = "Medium"
        reason = "Construction and infrastructure tenders are usually medium to high value."

    elif any(k in text for k in ["maintenance", "repair", "servicing"]):
        low = 100000
        high = 1000000
        confidence = "Medium"
        reason = "Maintenance and repair contracts vary with scope and contract term."

    elif any(k in text for k in ["truck", "vehicle", "fire truck"]):
        low = 1000000
        high = 8000000
        confidence = "High"
        reason = "Specialized vehicles are typically high-value procurements."

    elif any(k in text for k in ["server", "hardware", "storage", "backup appliance"]):
        low = 200000
        high = 2000000
        confidence = "Medium"
        reason = "IT infrastructure procurement depends on scale and specification."

    elif category and category.lower() == "goods":
        low = 50000
        high = 1000000
        confidence = "Low"
        reason = "General goods procurement estimate."

    value_display = f"R{low:,.0f} - R{high:,.0f}"

    return {
        "value_display": value_display,
        "value_source": "estimated",
        "estimation_confidence": confidence,
        "estimation_reason": reason,
        "estimated_value_low": low,
        "estimated_value_high": high,
        "estimated_value_mid": round((low + high) / 2, 0)
    }


@app.post("/score")
def score():
    profile_text, profile_source = extract_profile_text()

    date_from = get_request_value("date_from", "2026-01-01")
    date_to = get_request_value("date_to", "2026-03-17")
    page_number = int(get_request_value("page_number", 1))
    page_size = int(get_request_value("page_size", 10))

    profile_keywords = tokenize(profile_text)[:25]

    url = "https://ocds-api.etenders.gov.za/api/OCDSReleases"
    params = {
        "PageNumber": page_number,
        "PageSize": page_size,
        "dateFrom": date_from,
        "dateTo": date_to
    }

    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        releases = extract_releases(data)

        tenders = []
        for item in releases:
            tender = item.get("tender", {}) if isinstance(item, dict) else {}
            buyer = item.get("buyer", {}) if isinstance(item, dict) else {}
            tender_period = tender.get("tenderPeriod", {}) if isinstance(tender, dict) else {}
            value = tender.get("value", {}) if isinstance(tender, dict) else {}

            description = tender.get("description", "") or ""
            title = tender.get("title", "") or ""
            buyer_name = buyer.get("name", "") or ""
            category = tender.get("mainProcurementCategory", "") or ""

            combined_text = f"{title} {description} {buyer_name} {category}"
            fit_score, fit_band, matched_keywords = score_tender(
                profile_keywords,
                combined_text,
                category
            )

            published_value = value.get("amount")
            published_currency = value.get("currency")
            estimation = estimate_tender_value(title, description, category)

            if published_value and published_value > 0:
                value_display = f"R{published_value:,.0f}"
                value_source = "published"
                estimation_confidence = "High"
                estimation_reason = "Published by tender source."
                estimated_value_low = published_value
                estimated_value_high = published_value
                estimated_value_mid = published_value
            else:
                value_display = estimation["value_display"]
                value_source = estimation["value_source"]
                estimation_confidence = estimation["estimation_confidence"]
                estimation_reason = estimation["estimation_reason"]
                estimated_value_low = estimation["estimated_value_low"]
                estimated_value_high = estimation["estimated_value_high"]
                estimated_value_mid = estimation["estimated_value_mid"]

            tenders.append({
                "ocid": item.get("ocid") if isinstance(item, dict) else None,
                "title": title,
                "buyer": buyer_name,
                "description": description,
                "status": tender.get("status"),
                "category": category,
                "close_date": tender_period.get("endDate"),
                "value_amount": published_value,
                "value_currency": published_currency,
                "value_display": value_display,
                "value_source": value_source,
                "estimation_confidence": estimation_confidence,
                "estimation_reason": estimation_reason,
                "estimated_value_low": estimated_value_low,
                "estimated_value_high": estimated_value_high,
                "estimated_value_mid": estimated_value_mid,
                "fit_score": fit_score,
                "fit_band": fit_band,
                "matched_keywords": matched_keywords
            })

        tenders = sorted(tenders, key=lambda x: x["fit_score"], reverse=True)

        return jsonify({
            "status": "ok",
            "profile_source": profile_source,
            "profile_text_preview": profile_text[:500],
            "profile_keywords": profile_keywords,
            "request_used": params,
            "summary": {
                "total_releases_found": len(releases),
                "returned_tenders": len(tenders),
                "high_fit": sum(1 for t in tenders if t["fit_band"] == "High fit"),
                "medium_fit": sum(1 for t in tenders if t["fit_band"] == "Medium fit"),
                "low_fit": sum(1 for t in tenders if t["fit_band"] == "Low fit")
            },
            "tenders": tenders
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e)
        }), 500
