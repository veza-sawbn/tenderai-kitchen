from flask import Flask, request, jsonify
import requests

app = Flask(__name__)


@app.get("/")
def home():
    return {"message": "TenderAI kitchen home"}


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


def safe_get(dct, *keys):
    current = dct
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


@app.post("/score")
def score():
    payload = request.get_json(silent=True) or {}

    profile_text = payload.get("profile_text", "")
    date_from = payload.get("date_from", "")
    date_to = payload.get("date_to", "")
    pages = int(payload.get("pages", 1))
    page_size = int(payload.get("page_size", 20))

    tenders = []
    errors = []

    for page_number in range(1, pages + 1):
        url = "https://ocds-api.etenders.gov.za/api/OCDSReleases"
        params = {
            "PageNumber": page_number,
            "PageSize": page_size
        }

        if date_from:
            params["dateFrom"] = date_from
        if date_to:
            params["dateTo"] = date_to

        try:
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            errors.append(f"Page {page_number}: {str(e)}")
            continue

        releases = extract_releases(data)

        for item in releases:
            tender = safe_get(item, "tender") or {}
            buyer = safe_get(item, "buyer") or {}
            tender_period = tender.get("tenderPeriod") or {}
            value = tender.get("value") or {}

            tender_record = {
                "ocid": item.get("ocid"),
                "title": tender.get("title"),
                "buyer": buyer.get("name"),
                "description": tender.get("description"),
                "status": tender.get("status"),
                "category": tender.get("mainProcurementCategory"),
                "procurement_method": tender.get("procurementMethod"),
                "close_date": tender_period.get("endDate"),
                "value_amount": value.get("amount"),
                "value_currency": value.get("currency")
            }

            tenders.append(tender_record)

    return jsonify({
        "status": "ok",
        "profile_text": profile_text,
        "request_received": {
            "date_from": date_from,
            "date_to": date_to,
            "pages": pages,
            "page_size": page_size
        },
        "summary": {
            "total_tenders": len(tenders),
            "errors": errors
        },
        "tenders": tenders
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
