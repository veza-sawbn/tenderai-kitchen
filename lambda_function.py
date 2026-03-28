import json
import os
import urllib.request
import urllib.parse
import urllib.error


APP_BASE_URL = os.environ["APP_BASE_URL"].rstrip("/")
ADMIN_TOKEN = os.environ["ADMIN_TOKEN"]

RUN_INGEST = os.environ.get("RUN_INGEST", "true").lower() == "true"
RUN_FETCH_DOCUMENTS = os.environ.get("RUN_FETCH_DOCUMENTS", "true").lower() == "true"

FETCH_LIMIT = int(os.environ.get("FETCH_LIMIT", "25"))
TIMEOUT_SECONDS = int(os.environ.get("TIMEOUT_SECONDS", "60"))


def http_get_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "TenderAI-Automation/1.0",
            "Accept": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return {
            "status_code": resp.status,
            "body_text": body,
        }


def call_endpoint(path: str, params: dict | None = None) -> dict:
    params = params or {}
    params["token"] = ADMIN_TOKEN
    url = f"{APP_BASE_URL}{path}?{urllib.parse.urlencode(params)}"
    result = http_get_json(url)

    parsed_json = None
    try:
        parsed_json = json.loads(result["body_text"])
    except Exception:
        parsed_json = {
            "raw_body": result["body_text"][:4000]
        }

    return {
        "url": url,
        "status_code": result["status_code"],
        "json": parsed_json,
    }


def lambda_handler(event, context):
    outputs = {
        "ok": True,
        "steps": [],
    }

    try:
        if RUN_INGEST:
            ingest_result = call_endpoint("/api/admin/run-ingest")
            outputs["steps"].append({
                "step": "run_ingest",
                **ingest_result,
            })

        if RUN_FETCH_DOCUMENTS:
            fetch_result = call_endpoint(
                "/api/admin/fetch-documents",
                {"limit": FETCH_LIMIT},
            )
            outputs["steps"].append({
                "step": "fetch_documents",
                **fetch_result,
            })

        return outputs

    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {
            "ok": False,
            "error": f"HTTPError {exc.code}",
            "body": body[:4000],
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
        }
