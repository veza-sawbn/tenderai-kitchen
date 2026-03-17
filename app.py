from flask import Flask, jsonify
import requests

app = Flask(__name__)


@app.get("/")
def home():
    return {"message": "TenderAI kitchen home v3"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/score")
def score():
    url = "https://ocds-api.etenders.gov.za/api/OCDSReleases"
    params = {
        "PageNumber": 1,
        "PageSize": 5
    }

    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()

    return jsonify({
        "status": "ok",
        "message": "live tender fetch works",
        "api_type": str(type(data).__name__),
        "preview_keys": list(data.keys()) if isinstance(data, dict) else [],
        "preview": data
    })
