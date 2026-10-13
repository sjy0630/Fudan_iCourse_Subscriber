"""Read-only backlog inventory; no media, model or email requests."""
import json
from pathlib import Path
import sqlite3
import sys
from diagnose_playback_metadata import login, quiet

COURSES = ("38713", "37664")

def emit(**data):
    print(json.dumps(data, ensure_ascii=False), flush=True)

def main(path):
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        stored = {r["sub_id"]: dict(r) for r in db.execute(
            "SELECT * FROM lectures WHERE course_id IN (?,?)", COURSES)}
    with quiet():
        client = login()
    for course_id in COURSES:
        with quiet():
            detail = client.get_course_detail(course_id)
        live = {str(r["sub_id"]): r for r in detail["lectures"]}
        ids = set(live) | {sid for sid, row in stored.items() if row["course_id"] == course_id}
        for sid in sorted(ids, key=lambda sid: (live.get(sid) or stored[sid]).get("date", "")):
            row, source = stored.get(sid, {}), live.get(sid, {})
            text, summary = row.get("transcript") or "", row.get("summary") or ""
            meaningful = ''.join(c for c in text if c.isalnum() and c not in '嗯啊呃哦唔')
            emit(course_id=course_id, sub_id=sid,
                 date=source.get("date") or row.get("date"),
                 in_database=bool(row), in_course_list=bool(source),
                 has_playback=source.get("has_playback"),
                 transcript_chars=len(text), meaningful_chars=len(meaningful),
                 unique_chars=len(set(meaningful)), summary_chars=len(summary),
                 processed_at=row.get("processed_at"), error_stage=row.get("error_stage"),
                 error_count=row.get("error_count"),
                 error_flags=[s for s in ('Empty transcript', 'No audio', '未到开放时间', 'No video', 'Incomplete', 'Timeout')
                              if s.lower() in (row.get('error_msg') or '').lower()])

if __name__ == '__main__':
    try:
        main(sys.argv[1])
    except Exception as exc:
        emit(error_type=type(exc).__name__)
        raise SystemExit(1)
