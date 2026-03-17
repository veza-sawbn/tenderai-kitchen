from flask import Flask, request, jsonify

app = Flask(__name__)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/score")
def score():
    payload = request.get_json(silent=True) or {}

    profile_text = payload.get("profile_text", "")
    date_from = payload.get("date_from", "")
    date_to = payload.get("date_to", "")
    pages = payload.get("pages", 1)
    page_size = payload.get("page_size", 50)

    words = [w.strip().lower() for w in profile_text.split() if len(w.strip()) > 3]
    keywords = words[:10]

    return jsonify({
        "status": "ok",
        "profile_summary": {
            "keywords": keywords
        },
        "request_received": {
            "date_from": date_from,
            "date_to": date_to,
            "pages": pages,
            "page_size": page_size
        },
        "summary": {
            "total_tenders": 0
        },
        "tenders": []
    })
