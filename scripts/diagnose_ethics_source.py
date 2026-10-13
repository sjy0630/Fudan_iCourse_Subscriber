"""Read-only source statistics for the ethics lecture on 2026-09-09.

Only request official transcript JSON and the PPT metadata list. Never request
media/images, call a model, write the database, or print source text/URLs/IDs.
"""

from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import urlsplit

from diagnose_playback_metadata import login, quiet, safe_key

COURSE_ID = "38713"
DATE = "2026-09-09"
KEYWORDS = ("概率", "二项", "超几何", "生日", "泊松", "阿多诺", "马克思", "法治", "道德", "美学")
SHANGHAI = timezone(timedelta(hours=8))


def emit(event, **data):
    print(json.dumps({"event": event, **data}, ensure_ascii=False,
                     allow_nan=False), flush=True)


def silent_call(function, *args, **kwargs):
    with quiet():
        try:
            return function(*args, **kwargs)
        finally:
            # Flush even on an exception before quiet() restores the descriptors.
            sys.stdout.flush()
            sys.stderr.flush()


def counts(text):
    return {word: text.count(word) for word in KEYWORDS if word in text}


def grams(text):
    normalized = "".join(c for c in (text or "").casefold() if c.isalnum())
    return normalized, set(normalized[i:i + 4] for i in range(len(normalized) - 3))


def compare_text(source, stored):
    source_norm, source_grams = grams(source)
    stored_norm, stored_grams = grams(stored)
    shared = len(source_grams & stored_grams)
    union = len(source_grams | stored_grams)
    return {
        "db_transcript_present": bool(stored_norm),
        "normalized_equal": source_norm == stored_norm if stored_norm else None,
        "shared_4grams": shared,
        "source_4gram_overlap": round(shared / len(source_grams), 4) if source_grams else None,
        "db_4gram_overlap": round(shared / len(stored_grams), 4) if stored_grams else None,
        "jaccard_4grams": round(shared / union, 4) if union else None,
    }


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def value_range(values):
    values = [value for value in values if value is not None]
    return [min(values), max(values)] if values else None


class TaskLabels:
    def __init__(self):
        self.values = {}

    def label(self, value):
        if value is None or value == "":
            return "missing"
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            return "non_scalar"
        key = str(value)
        if key not in self.values:
            self.values[key] = f"T{len(self.values) + 1}"
        return self.values[key]


def identity_statistics(item, sub_id, labels):
    checks, tasks = [], []

    def walk(value, path="", depth=0):
        if not isinstance(value, dict) or depth > 3:
            return
        for key, child in value.items():
            field = f"{path}.{safe_key(key)}" if path else safe_key(key)
            normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            if normalized in ("courseid", "subid"):
                expected = COURSE_ID if normalized == "courseid" else sub_id
                checks.append({"field": field, "matches_requested": str(child) == str(expected)})
            elif normalized == "taskid":
                tasks.append({"field": field, "task_label": labels.label(child)})
            elif isinstance(child, dict):
                walk(child, field, depth + 1)

    walk(item)
    return {"identity_checks": checks, "task_labels": tasks}


def transcript_statistics(payload, sub_id, stored, labels):
    code = payload.get("code")
    safe_code = int(code) if re.fullmatch(r"-?\d{1,6}", str(code)) else None
    items = payload.get("list")
    report = {"api_code": safe_code, "list_type": type(items).__name__,
              "list_length": len(items) if isinstance(items, list) else None,
              "results": []}
    for index, item in enumerate(items if isinstance(items, list) else []):
        result = {"result_index": index, "type": type(item).__name__}
        if not isinstance(item, dict):
            report["results"].append(result)
            continue
        result["keys"] = [safe_key(key) for key in item]
        result.update(identity_statistics(item, sub_id, labels))
        content = item.get("all_content")
        result["all_content_type"] = type(content).__name__
        if not isinstance(content, list):
            report["results"].append(result)
            continue
        segments = []
        buckets = defaultdict(lambda: {"segments": 0, "chars": 0, "keywords": Counter()})
        for segment in content:
            if not isinstance(segment, dict) or not isinstance(segment.get("Text"), str):
                continue
            text = segment["Text"]
            if not text.strip():
                continue
            start = number(segment.get("BeginSec"))
            end = number(segment.get("EndSec", segment.get("BeginSec")))
            segments.append((start, end, text))
            bucket = math.floor(start / 600) if start is not None else None
            buckets[bucket]["segments"] += 1
            buckets[bucket]["chars"] += len(text)
            buckets[bucket]["keywords"].update(counts(text))
        ordered = sorted(segments, key=lambda entry: entry[0] if entry[0] is not None else 0)
        joined = " ".join(entry[2] for entry in ordered)
        result.update(segments=len(segments), joined_chars=len(joined),
                      time_range_seconds=value_range([v for start, end, _ in segments for v in (start, end)]),
                      keywords=counts(joined), db_comparison=compare_text(joined, stored))
        result["ten_minute_buckets"] = [
            {"start_seconds": bucket * 600 if bucket is not None else None,
             "end_seconds": (bucket + 1) * 600 if bucket is not None else None,
             "segments": data["segments"], "chars": data["chars"], "keywords": dict(data["keywords"])}
            for bucket, data in sorted(buckets.items(), key=lambda entry: (entry[0] is None, entry[0] or 0))
        ]
        report["results"].append(result)
    return report


def image_path(value):
    try:
        return urlsplit(value or "").path
    except (TypeError, ValueError):
        return ""


def created_date(value):
    milliseconds = number(value)
    if milliseconds is None or milliseconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(milliseconds / 1000, SHANGHAI).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def ppt_statistics(items, stored_pages, labels):
    stored_paths = defaultdict(list)
    for page in stored_pages:
        path = image_path(page["pptimgurl"])
        if path:
            stored_paths[path].append(page)
    groups = defaultdict(list)
    for item in items:
        groups[labels.label(item.get("taskid"))].append(item)
    result = []
    for task_label, group in groups.items():
        matched = {}
        matched_source_items = 0
        for item in group:
            pages = stored_paths.get(image_path(item.get("pptimgurl")), [])
            matched_source_items += bool(pages)
            for page in pages:
                matched[page["page_num"]] = page
        keywords, done_keywords = Counter(), Counter()
        for page in matched.values():
            keywords.update(counts(page["text"] or ""))
            if page["ocr_status"] == "done":
                done_keywords.update(counts(page["text"] or ""))
        dates = [created_date(item.get("created_ms")) for item in group]
        result.append({
            "task_label": task_label, "source_items": len(group),
            "created_sec_range": value_range([number(item.get("created_sec")) for item in group]),
            "created_date_range_shanghai": value_range(dates),
            "missing_or_invalid_created_dates": sum(date is None for date in dates),
            "matched_source_items": matched_source_items, "matched_db_pages": len(matched),
            "matched_db_status_counts": dict(Counter(page["ocr_status"] for page in matched.values())),
            "matched_db_keywords": dict(keywords), "matched_db_done_keywords": dict(done_keywords),
        })
    return {"source_items": len(items), "task_groups": result}


def main(db_path):
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        targets = [dict(row) for row in conn.execute(
            "SELECT sub_id, transcript FROM lectures WHERE course_id=? "
            "AND (date=? OR sub_title LIKE ?) ORDER BY sub_id",
            (COURSE_ID, DATE, DATE + "%"))]
        for target in targets:
            target["pages"] = [dict(row) for row in conn.execute(
                "SELECT page_num, pptimgurl, text, ocr_status FROM ppt_pages WHERE sub_id=?",
                (target["sub_id"],))]
    emit("scope", course_id=COURSE_ID, date=DATE, target_rows=len(targets),
         keywords=list(KEYWORDS), read_only=True)
    if not targets:
        return 1
    client = silent_call(login)
    emit("login", verified=True)
    labels = TaskLabels()
    errors = 0
    for index, target in enumerate(targets, start=1):
        target_label = f"lecture_{index}"
        sub_id = target["sub_id"]
        try:
            def fetch_transcript():
                response = client.vpn.get(
                    client.base_url + "/courseapi/v3/web-socket/search-trans-result",
                    params={"sub_id": sub_id, "format": "json"}, timeout=30)
                response.raise_for_status()
                return response.json()
            payload = silent_call(fetch_transcript)
            emit("official_transcript", target=target_label,
                 **transcript_statistics(payload, sub_id, target["transcript"], labels))
        except Exception as exc:
            errors += 1
            emit("source_error", target=target_label, endpoint="official_transcript", error_type=type(exc).__name__)
        try:
            items = silent_call(client.get_ppt_list, COURSE_ID, sub_id)
            emit("ppt_metadata", target=target_label,
                 **ppt_statistics(items, target["pages"], labels))
        except Exception as exc:
            errors += 1
            emit("source_error", target=target_label, endpoint="ppt_metadata", error_type=type(exc).__name__)
    emit("completed", source_errors=errors, media_requests=0, image_requests=0,
         model_requests=0, database_writes=0, emails=0)
    return 1 if errors else 0


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("One database path is required")
        raise SystemExit(main(sys.argv[1]))
    except Exception as exc:
        emit("failure", error_type=type(exc).__name__)
        raise SystemExit(1)
