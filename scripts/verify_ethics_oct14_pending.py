#!/usr/bin/env python3
"""Compare a recovery backup and latest database without modifying either.

Usage: python verify_ethics_oct14.py BACKUP_DB LATEST_DB
Only table counts and the authorized lecture's metadata/topic counts are
printed. Exit 0 requires exactly the intended record restored to pending.
"""

from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import sys

TARGET = "682860"
KEYS = {"lectures": ("sub_id",), "courses": ("course_id",),
        "ppt_pages": ("sub_id", "page_num"),
        "all_courses": ("course_id", "term"), "meta": ("key",)}
ALLOWED = {"transcript", "summary", "summary_model", "processed_at",
           "error_msg", "error_count", "error_stage"}
TOPICS = ("巴厘岛", "听写", "游泳池", "标准间", "烹饪", "课文", "语法", "道德", "法治", "法律", "理想", "人生", "信念", "价值")


def emit(event, **data):
    print(json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)


def open_readonly(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def rows(conn, table):
    return {tuple(row[key] for key in KEYS[table]): dict(row)
            for row in conn.execute(f"SELECT * FROM {table}")}


def safe_label(value):
    return re.sub(r"https?://\S+", "[URL]", str(value or ""))[:120]


def verify(backup_path, latest_path):
    valid = True
    with closing(open_readonly(backup_path)) as backup, closing(open_readonly(latest_path)) as latest:
        for table in KEYS:
            old = rows(backup, table)
            new = rows(latest, table)
            inserted = new.keys() - old.keys()
            deleted = old.keys() - new.keys()
            modified = {key for key in old.keys() & new.keys() if old[key] != new[key]}
            changes = inserted | deleted | modified
            emit("table_changes", table=table, changed_rows=len(changes),
                 inserted_rows=len(inserted), deleted_rows=len(deleted),
                 modified_rows=len(modified))
            if table != "lectures":
                valid = valid and not changes
                continue

            target_key = (TARGET,)
            valid = valid and modified == {target_key} and not inserted and not deleted
            old_target = old.get(target_key)
            target = new.get(target_key)
            if not old_target or not target:
                emit("target", sub_id=TARGET, present_in_both=False)
                valid = False
                continue
            changed_fields = {name for name in old_target if old_target[name] != target[name]}
            email_unchanged = old_target["emailed_at"] == target["emailed_at"]
            valid = (valid and changed_fields <= ALLOWED and email_unchanged
                     and target["course_id"] == "38713"
                     and target["transcript"] is None and target["summary"] is None
                     and target["summary_model"] is None and target["processed_at"] is None
                     and target["error_stage"] == "no_video"
                     and target["error_msg"] == "Official transcript belongs to another course; waiting for actual recording."
                     and target["error_count"] == old_target["error_count"])
            emit("target", sub_id=TARGET, present_in_both=True,
                 changed_fields=sorted(changed_fields), emailed_at_equal=email_unchanged,
                 processed_at=safe_label(target["processed_at"]),
                 summary_model=safe_label(target["summary_model"]))
            for field in ("transcript", "summary"):
                text = target[field] or ""
                old_text = old_target[field] or ""
                emit("target_text_stats", sub_id=TARGET, field=field,
                     before_chars=len(old_text), after_chars=len(text),
                     before_topics={word: old_text.count(word) for word in TOPICS},
                     after_topics={word: text.count(word) for word in TOPICS})
    emit("verification", only_authorized_record_requeued=bool(valid), read_only=True)
    return 0 if valid else 1


if __name__ == "__main__":
    try:
        if len(sys.argv) != 3:
            raise ValueError("Expected backup and latest database paths")
        raise SystemExit(verify(sys.argv[1], sys.argv[2]))
    except Exception as exc:
        emit("failure", error_type=type(exc).__name__)
        raise SystemExit(1)
