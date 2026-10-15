"""Withdraw one confirmed mismatched transcript; preserve all other records.

The caller must back up the encrypted database before invoking this script.
No network, media, models or email are used here.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

TARGET = "682860"
ORIGINAL_TIME = "2026-10-14T16:03:53.122314"
MESSAGE = "Official transcript belongs to another course; waiting for actual recording."


def requeue(path):
    uri = Path(path).resolve().as_uri() + "?mode=rw"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        with db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM lectures WHERE sub_id=?", (TARGET,)).fetchone()
            if not (row and row["course_id"] == "38713"
                    and row["processed_at"] == ORIGINAL_TIME
                    and len(row["transcript"] or "") == 32306
                    and len(row["summary"] or "") == 4224
                    and not row["error_count"]):
                raise ValueError("Target no longer matches the diagnosed record; refusing to change it")
            db.execute(
                "UPDATE lectures SET transcript=NULL, summary=NULL, summary_model=NULL, "
                "processed_at=NULL, error_stage='no_video', error_msg=? WHERE sub_id=?",
                (MESSAGE, TARGET))
    print(json.dumps({"event": "requeued", "sub_id": TARGET,
                      "original_preserved_in_encrypted_backup": True, "emails_sent": 0}))


if __name__ == "__main__":
    try:
        requeue(sys.argv[1])
    except Exception as exc:
        print(json.dumps({"event": "failure", "error_type": type(exc).__name__}))
        raise SystemExit(1)
