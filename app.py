from flask import Flask, jsonify

app = Flask(__name__)

@app.get("/")
def home():
    return {"message": "TenderAI kitchen home v2"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/score")
def score():
    return jsonify({
        "status": "ok",
        "message": "new version is live"
    })
