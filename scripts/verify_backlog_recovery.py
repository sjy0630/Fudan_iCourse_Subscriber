"""Read-only before/after comparison. Never print lecture bodies or URLs."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

TARGETS = {"660912": "38713", "671336": "38713", "664293": "37664", "680154": "37664"}
KEYS = {"lectures": ("sub_id",), "courses": ("course_id",),
        "ppt_pages": ("sub_id", "page_num"), "all_courses": ("course_id", "term"), "meta": ("key",)}
TOPICS = {"38713": ("道德", "法律", "人生", "理想", "马克思", "概率", "阿多诺"),
          "37664": ("电路", "电压", "电流", "二极管", "晶体管", "放大")}
ALLOWED = {"transcript", "summary", "summary_model", "processed_at", "error_msg", "error_count", "error_stage"}

def emit(**values):
    print(json.dumps(values, ensure_ascii=False), flush=True)

def connect(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db

def verify(before_path, after_path):
    valid = True
    changed_targets = []
    with closing(connect(before_path)) as before, closing(connect(after_path)) as after:
        for table, keys in KEYS.items():
            rows = [{tuple(row[k] for k in keys): dict(row) for row in db.execute(f"SELECT * FROM {table}")}
                    for db in (before, after)]
            old, new = rows
            changed = {k for k in old.keys() | new.keys() if old.get(k) != new.get(k)}
            emit(table=table, changed_rows=len(changed))
            if table != "lectures":
                valid = valid and not changed
                continue
            valid = valid and old.keys() == new.keys() and changed <= {(sid,) for sid in TARGETS}
            for sid, course_id in TARGETS.items():
                previous, current = old.get((sid,)), new.get((sid,))
                if not previous or not current:
                    valid = False
                    emit(sub_id=sid, missing=True)
                    continue
                fields = {k for k in previous if previous[k] != current[k]}
                if fields:
                    changed_targets.append(sid)
                    valid = valid and fields <= ALLOWED and current['course_id'] == course_id
                    valid = valid and bool(current['transcript']) and bool(current['summary']) and bool(current['processed_at'])
                    valid = valid and not current['error_stage'] and not current['error_msg'] and not current['error_count']
                emit(sub_id=sid, changed_fields=sorted(fields),
                     transcript_chars=len(current['transcript'] or ''),
                     summary_chars=len(current['summary'] or ''),
                     emailed_at_preserved=previous['emailed_at'] == current['emailed_at'],
                     summary_topics={word: (current['summary'] or '').count(word) for word in TOPICS[course_id]})
    emit(verified=bool(valid and changed_targets), changed_targets=changed_targets)
    return 0 if valid and changed_targets else 1

if __name__ == '__main__':
    try:
        raise SystemExit(verify(sys.argv[1], sys.argv[2]))
    except Exception as exc:
        emit(error_type=type(exc).__name__)
        raise SystemExit(1)
