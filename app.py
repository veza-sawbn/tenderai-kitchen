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

    try:
        response = requests.get(url, params=params, timeout=30)

        return jsonify({
            "status": "ok",
            "message": "live tender fetch works",
            "status_code": response.status_code,
            "content_type": response.headers.get("Content-Type"),
            "text_preview": response.text[:1000]
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e)
        }), 500
