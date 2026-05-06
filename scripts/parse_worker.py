"""
TenderAI parse worker.

Use this from Lambda/EventBridge, a container task, or a manual one-off run.
It connects to the same DATABASE_URL and processes queued parse jobs without
going through the browser request path.
"""

import json
import os

from database import get_db_session, init_db
from services.tender_document_parser import process_parse_worker

init_db()


def handler(event=None, context=None):
    limit = int(os.getenv("TENDER_PARSE_WORKER_LIMIT", "1"))
    with get_db_session() as session:
        result = process_parse_worker(session, limit=limit)
    return {
        "statusCode": 200,
        "body": json.dumps(result, default=str),
    }


if __name__ == "__main__":
    print(json.dumps(handler(), indent=2))
