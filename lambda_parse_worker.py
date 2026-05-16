"""
TenderAI Lambda Parse Worker

Do not parse tender documents through App Runner/browser.
This Lambda processes queued rows in tender_document_parsed_cache using process_parse_worker().
"""

import json
import os
import traceback

from database import get_db_session, init_db
from services.tender_document_parser import process_parse_worker


def handler(event=None, context=None):
    try:
        init_db()
        limit = int(os.getenv("TENDER_PARSE_WORKER_LIMIT", "1"))

        with get_db_session() as session:
            result = process_parse_worker(session, limit=limit)

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(result, default=str),
        }

    except Exception as exc:
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "ok": False,
                "status": "failed",
                "error": str(exc),
                "traceback": traceback.format_exc()[-4000:],
            }, default=str),
        }


if __name__ == "__main__":
    print(json.dumps(json.loads(handler()["body"]), indent=2))
