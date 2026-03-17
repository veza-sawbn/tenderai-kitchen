from flask import Flask, jsonify, request
import requests

app = Flask(__name__)


@app.get("/")
def home():
    return {"message": "TenderAI kitchen home v4"}


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


@app.post("/score")
def score():
    body = request.get_json(silent=True) or {}

    profile_text = body.get("profile_text", "")
    date_from = body.get("date_from", "2026-01-01")
    date_to = body.get("date_to", "2026-03-17")
    page_number = int(body.get("page_number", 1))
    page_size = int(body.get("page_size", 10))

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
        for item in releases[:10]:
            tender = item.get("tender", {}) if isinstance(item, dict) else {}
            buyer = item.get("buyer", {}) if isinstance(item, dict) else {}
            tender_period = tender.get("tenderPeriod", {}) if isinstance(tender, dict) else {}
            value = tender.get("value", {}) if isinstance(tender, dict) else {}

            tenders.append({
                "ocid": item.get("ocid") if isinstance(item, dict) else None,
                "title": tender.get("title"),
                "buyer": buyer.get("name"),
                "description": tender.get("description"),
                "status": tender.get("status"),
                "category": tender.get("mainProcurementCategory"),
                "close_date": tender_period.get("endDate"),
                "value_amount": value.get("amount"),
                "value_currency": value.get("currency")
            })

        return jsonify({
            "status": "ok",
            "profile_text": profile_text,
            "request_used": params,
            "summary": {
                "total_releases_found": len(releases),
                "returned_tenders": len(tenders)
            },
            "tenders": tenders
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e)
        }), 500
