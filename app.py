from flask import Flask, request, jsonify

app = Flask(__name__)

@app.get("/health")
def health():
    return {"status": "ok"}
import requests

@app.post("/score")
def score():
    payload = request.get_json(silent=True) or {}

    profile_text = payload.get("profile_text", "")

    url = "https://ocds-api.etenders.gov.za/api/OCDSReleases"
    params = {
        "PageNumber": 1,
        "PageSize": 20
    }

    try:
        res = requests.get(url, params=params)
        data = res.json()
    except Exception as e:
        return {"error": str(e)}

    tenders = []

    if isinstance(data, dict):
        possible = data.get("releases") or data.get("data") or data.get("value") or []
    else:
        possible = []

    for item in possible[:10]:
        tenders.append({
            "title": item.get("tender", {}).get("title"),
            "buyer": item.get("buyer", {}).get("name"),
            "description": item.get("tender", {}).get("description")
        })

    return {
        "status": "ok",
        "profile_text": profile_text,
        "tenders": tenders
    }
