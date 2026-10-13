#!/usr/bin/env python3
"""Read-only topic/provenance statistics for ethics lecture 2026-09-09.

No iCourse/model/SMTP calls. No raw text, URLs, credentials or content hashes
are printed. Cross-lecture comparison reports metadata and aggregate counts.
"""

from collections import Counter, defaultdict
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import urlsplit

COURSE_ID = "38713"
DATE = "2026-09-09"
KEYWORDS = ("概率", "二项", "超几何", "生日", "泊松", "阿多诺", "马克思", "法治", "道德", "美学")


def emit(event, **data):
    print(json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)


def counts(text):
    text = text or ""
    return {word: text.count(word) for word in KEYWORDS if word in text}


def label(value):
    return re.sub(r"https?://\S+", "[URL]", str(value or ""))[:120]


def metadata(row):
    return {key: label(row[key]) for key in ("course_id", "course_title", "sub_id", "date")}


def path_only(url):
    return urlsplit(url or "").path


def grams(text):
    normalized = "".join(c for c in (text or "").casefold() if c.isalnum())
    return normalized, frozenset(normalized[i:i + 4] for i in range(len(normalized) - 3))


def text_statistics(field, text, sub_id):
    text = text or ""
    emit("field", sub_id=sub_id, field=field, chars=len(text), keywords=counts(text),
         paragraphs=len([p for p in re.split(r"\n\s*\n", text) if p.strip()]))
    # Fixed character ranges work when old ASR has no paragraphs/timestamps.
    for start in range(0, len(text), 1200):
        block = text[start:start + 1200]
        emit("text_block", sub_id=sub_id, field=field,
             char_start=start, char_end=start + len(block), keywords=counts(block))


def compare_pages(target_id, target_pages, all_pages, lectures):
    other_pages = [p for p in all_pages if p["sub_id"] != target_id]
    paths = defaultdict(list)
    for page in target_pages:
        path = path_only(page["pptimgurl"])
        if path:
            paths[path].append(page["page_num"])
    shared_paths = defaultdict(lambda: {"target_pages": set(), "other_pages": set(),
                                        "other_times": [], "keywords": Counter()})
    for page in other_pages:
        path = path_only(page["pptimgurl"])
        if path not in paths:
            continue
        group = shared_paths[page["sub_id"]]
        group["target_pages"].update(paths[path])
        group["other_pages"].add(page["page_num"])
        group["other_times"].append(page["created_sec"])
        group["keywords"].update(counts(page["text"]))
    for sub_id, group in sorted(shared_paths.items()):
        emit("same_image_path", target_sub_id=target_id, **metadata(lectures[sub_id]),
             target_page_count=len(group["target_pages"]), other_page_count=len(group["other_pages"]),
             target_pages=sorted(group["target_pages"]),
             other_time_range=[min(group["other_times"]), max(group["other_times"])],
             keywords=dict(group["keywords"]))

    # Shared material is evidence, not proof of a bug: repeat slides can match.
    prepared = []
    for page in other_pages:
        normalized, tokens = grams(page["text"])
        if len(normalized) >= 80 and len(tokens) >= 20:
            prepared.append((page, len(normalized), tokens))
    similar = defaultdict(lambda: {"target_pages": set(), "other_pages": set(),
                                   "max_containment": 0, "max_jaccard": 0,
                                   "keywords": Counter()})
    for target in target_pages:
        normalized, tokens = grams(target["text"])
        if len(normalized) < 80 or len(tokens) < 20:
            continue
        for page, text_length, other_tokens in prepared:
            if min(len(normalized), text_length) / max(len(normalized), text_length) < 0.35:
                continue
            shared = len(tokens & other_tokens)
            containment = shared / min(len(tokens), len(other_tokens))
            jaccard = shared / (len(tokens) + len(other_tokens) - shared)
            if containment < 0.8 or jaccard < 0.5:
                continue
            group = similar[page["sub_id"]]
            group["target_pages"].add(target["page_num"])
            if page["page_num"] not in group["other_pages"]:
                group["keywords"].update(counts(page["text"]))
            group["other_pages"].add(page["page_num"])
            group["max_containment"] = max(group["max_containment"], containment)
            group["max_jaccard"] = max(group["max_jaccard"], jaccard)
    ranked = sorted(similar.items(), key=lambda item: len(item[1]["target_pages"]), reverse=True)
    for sub_id, group in ranked[:20]:
        emit("similar_ppt_text", target_sub_id=target_id, **metadata(lectures[sub_id]),
             target_page_count=len(group["target_pages"]), other_page_count=len(group["other_pages"]),
             target_pages=sorted(group["target_pages"]), keywords=dict(group["keywords"]),
             max_containment=round(group["max_containment"], 4),
             max_jaccard=round(group["max_jaccard"], 4))
    emit("comparison_totals", target_sub_id=target_id,
         same_image_path_lectures=len(shared_paths), similar_text_lectures=len(similar),
         similar_text_lectures_reported=min(20, len(similar)))


def diagnose(db_path):
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        lectures = {row["sub_id"]: dict(row) for row in conn.execute(
            "SELECT l.*, c.title AS course_title FROM lectures l "
            "LEFT JOIN courses c ON c.course_id=l.course_id")}
        targets = [row for row in lectures.values() if row["course_id"] == COURSE_ID
                   and (row["date"] == DATE or (row["sub_title"] or "").startswith(DATE))]
        all_pages = [dict(row) for row in conn.execute(
            "SELECT p.* FROM ppt_pages p JOIN lectures l ON p.sub_id=l.sub_id")]
        emit("scope", course_id=COURSE_ID, date=DATE, target_rows=len(targets),
             compared_lecture_rows=len(lectures), compared_ppt_rows=len(all_pages),
             keywords=list(KEYWORDS), read_only=True)
        for row in targets:
            sub_id = row["sub_id"]
            emit("target", **metadata(row), sub_title=label(row["sub_title"]),
                 processed_at=label(row["processed_at"]), emailed_at=label(row["emailed_at"]),
                 error_stage=label(row["error_stage"]), error_count=row["error_count"],
                 summary_model=label(row["summary_model"]))
            for field in ("transcript", "summary"):
                text = row[field] or ""
                text_statistics(field, text, sub_id)
                if len(text.strip()) < 20:
                    continue
                digest = hashlib.sha256(text.encode()).digest()
                for other in lectures.values():
                    if other["sub_id"] == sub_id:
                        continue
                    if hashlib.sha256((other[field] or "").encode()).digest() == digest:
                        emit("identical_field", target_sub_id=sub_id, field=field,
                             **metadata(other), chars=len(text), keywords=counts(text))
            pages = sorted([p for p in all_pages if p["sub_id"] == sub_id],
                           key=lambda p: (p["created_sec"], p["page_num"]))
            emit("ppt_totals", sub_id=sub_id, pages=len(pages),
                 statuses=dict(Counter(p["ocr_status"] for p in pages)))
            for page in pages:
                emit("ppt_page", sub_id=sub_id, page_num=page["page_num"],
                     seconds=page["created_sec"], status=label(page["ocr_status"]),
                     chars=len(page["text"] or ""), keywords=counts(page["text"]))
            compare_pages(sub_id, pages, all_pages, lectures)
        return 0 if targets else 1


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("One database path is required")
        raise SystemExit(diagnose(sys.argv[1]))
    except Exception as exc:
        emit("failure", error_type=type(exc).__name__)
        raise SystemExit(1)
