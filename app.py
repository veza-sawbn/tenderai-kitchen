from flask import Flask, jsonify, request
import requests
import re

app = Flask(__name__)


@app.get("/")
def home():
    return {"message": "TenderAI kitchen home v5"}


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
        "an", "be", "we", "it", "their", "its"
    }
    return [w for w in words if len(w) > 2 and w not in stopwords]


def score_tender(profile_keywords, tender_text):
    tender_tokens = set(tokenize(tender_text))
    profile_set = set(profile_keywords)

    matched = sorted(profile_set.intersection(tender_tokens))
    score = round((len(matched) / max(len(profile_set), 1)) * 100, 1)

    if score >= 60:
        fit_band = "High fit"
    elif score >= 30:
        fit_band = "Medium fit"
    else:
        fit_band = "Low fit"

    return score, fit_band, matched


@app.post("/score")
def score():
    body = request.get_json(silent=True) or {}

    profile_text = body.get("profile_text", "")
    date_from = body.get("date_from", "2026-01-01")
    date_to = body.get("date_to", "2026-03-17")
    page_number = int(body.get("page_number", 1))
    page_size = int(body.get("page_size", 10))

    profile_keywords = tokenize(profile_text)[:20]

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

            description = tender.get("description", "")
            title = tender.get("title", "")
            buyer_name = buyer.get("name", "")
            category = tender.get("mainProcurementCategory", "")

            combined_text = f"{title} {description} {buyer_name} {category}"
            fit_score, fit_band, matched_keywords = score_tender(profile_keywords, combined_text)

            tenders.append({
                "ocid": item.get("ocid") if isinstance(item, dict) else None,
                "title": title,
                "buyer": buyer_name,
                "description": description,
                "status": tender.get("status"),
                "category": category,
                "close_date": tender_period.get("endDate"),
                "value_amount": value.get("amount"),
                "value_currency": value.get("currency"),
                "fit_score": fit_score,
                "fit_band": fit_band,
                "matched_keywords": matched_keywords
            })

        tenders = sorted(tenders, key=lambda x: x["fit_score"], reverse=True)

        return jsonify({
            "status": "ok",
            "profile_text": profile_text,
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
