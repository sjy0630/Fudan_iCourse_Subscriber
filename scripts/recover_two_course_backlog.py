#!/usr/bin/env python3
"""One-time recovery, run from the repository containing the audio-channel fix.

  python /path/recover_two_course_backlog.py process INPUT_DB CANDIDATE_DB
  python /path/recover_two_course_backlog.py apply CANDIDATE_DB LATEST_REMOTE_DB

process uses the existing ASR/PPT/summary pipeline for the four inventoried incomplete lectures of ethics and analog electronics,
with official transcripts disabled. It never sends email. Existing PPT cache
is reused; new OCR can inform the replacement summary but is NOT copied by
apply. apply changes only successful lecture content/error fields, preserving
the latest email markers, PPT rows, metadata and all unrelated lectures.

The calling workflow must back up the encrypted data branch, fetch its latest
version before apply, encrypt the result, publish with force-with-lease, and
remove all plaintext databases. No workflow or remote publication is included.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time


TARGETS = (("38713", "660912"), ("38713", "671336"), ("37664", "664293"), ("37664", "680154"))
FIELDS = ("transcript", "summary", "summary_model", "processed_at",
          "error_msg", "error_count", "error_stage")
SUCCESS_TABLE = "channel_recovery_successes"
TOPIC_WORDS = ("概率", "生日", "二项", "阿多诺", "美学", "道德", "法治", "法律", "理想", "人生")


def emit(*, output=None, **stats):
    print(json.dumps(stats, ensure_ascii=True), file=output, flush=True)


@contextmanager
def quiet_pipeline():
    # Existing exception messages can include server responses/URLs. Discard
    # underlying logs rather than attempting to redact unknown formats.
    with open(os.devnull, "w") as sink:
        with redirect_stdout(sink), redirect_stderr(sink):
            yield


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def prepare_candidate(source_path, candidate_path):
    if Path(candidate_path).exists():
        raise FileExistsError("Candidate output must be a new file")
    with readonly(source_path) as source, sqlite3.connect(candidate_path) as dest:
        source.backup(dest)
        dest.execute(f"CREATE TABLE {SUCCESS_TABLE} (sub_id TEXT PRIMARY KEY)")
        dest.executemany(
            "UPDATE lectures SET transcript=NULL, summary=NULL, "
            "summary_model=NULL, processed_at=NULL, error_msg=NULL, "
            "error_stage=NULL, error_count=0 WHERE course_id=? AND sub_id=?",
            TARGETS,
        )


def completed(row, course_id):
    return bool(row and row["course_id"] == course_id
                and (row["transcript"] or "").strip()
                and (row["summary"] or "").strip()
                and row["processed_at"] and not row["error_stage"]
                and not row["error_msg"] and not row["error_count"])


def process_targets(db, runner, ensure_session=lambda: None,
                    cleanup=lambda sub_id: None, output=None, audio_stats=None):
    successes = 0
    for course_id, sub_id in TARGETS:
        lecture = db.get_lecture(sub_id)
        if not lecture or lecture["course_id"] != course_id:
            emit(output=output, sub_id=sub_id, success=False, error_type="MissingLecture")
            continue
        title_row = db.conn.execute(
            "SELECT title FROM courses WHERE course_id=?", (course_id,)
        ).fetchone()
        title = title_row[0] if title_row else course_id
        start = time.monotonic()
        emit(output=output, sub_id=sub_id, event="processing_started")
        error_type = None
        succeeded = False
        try:
            with quiet_pipeline():
                ensure_session()
                runner.run(course_id, title, lecture)
            row = db.get_lecture(sub_id)
            succeeded = completed(row, course_id)
            if succeeded:
                with db.conn:
                    db.conn.execute(f"INSERT INTO {SUCCESS_TABLE} VALUES (?)", (sub_id,))
                successes += 1
        except Exception as exc:
            error_type = type(exc).__name__
        finally:
            with quiet_pipeline():
                cleanup(sub_id)
        row = db.get_lecture(sub_id)
        emit(output=output, sub_id=sub_id, success=succeeded, error_type=error_type,
             error_stage=(row or {}).get("error_stage"),
             error_flags=[word for word in ("未到开放时间", "No video", "Empty transcript", "No audio", "Timeout")
                          if word.lower() in ((row or {}).get("error_msg") or "").lower()],
             transcript_chars=len((row or {}).get("transcript") or ""),
             summary_chars=len((row or {}).get("summary") or ""),
             topic_counts={field: {word: (row.get(field) or "").count(word)
                                   for word in TOPIC_WORDS}
                           for field in ("transcript", "summary")} if row else {},
             elapsed_seconds=round(time.monotonic() - start),
             **(audio_stats() if succeeded and audio_stats else {}))
    return successes


def process(source_path, candidate_path):
    # Imports happen after configuration so no cached official transcript or
    # persistent audio directory can bypass the intended recovery path.
    with tempfile.TemporaryDirectory(prefix="icourse-two-course-backlog-") as scratch:
        os.environ["USE_OFFICIAL_TRANSCRIPT"] = "0"
        os.environ["DATA_DIR"] = scratch
        sys.path.insert(0, str(Path.cwd()))
        with quiet_pipeline():
            from main import login_with_retry, _check_session
            from src.ai.summarizer import Summarizer
            from src.ai.transcriber import Transcriber
            from src.api.icourse import ICourseClient
            from src.data.database import Database
            from src.pipeline.lecture_runner import LectureRunner
            from src.runtime.reporter import Reporter
            from src.runtime.scheduler import Scheduler

        prepare_candidate(source_path, candidate_path)
        db = None
        scheduler = None
        stats_output = sys.stdout
        with quiet_pipeline():
            try:
                db = Database(str(candidate_path))
                reporter = Reporter()
                client = ICourseClient(login_with_retry())
                scheduler = Scheduler(reporter)
                ensure_session = lambda: _check_session(client)
                transcriber = Transcriber()
                runner = LectureRunner(client, db, scheduler, transcriber,
                                       Summarizer(), reporter,
                                       ensure_session=ensure_session)

                def cleanup(sub_id):
                    scheduler.image_cache.discard(sub_id)
                    scheduler.audio_downloader.release(sub_id)

                successes = process_targets(db, runner, ensure_session, cleanup,
                    output=stats_output, audio_stats=lambda: {
                        "audio_seconds": round(transcriber._last_duration, 1),
                        "media_seconds": transcriber._media_duration,
                        "segments": len(transcriber._last_segments),
                    })
            finally:
                try:
                    if scheduler:
                        scheduler.shutdown()
                finally:
                    if db:
                        db.conn.close()
        emit(successful_lectures=successes, requested_lectures=len(TARGETS),
             emails_sent=0, new_ppt_cache_will_be_applied=False)
        # Publish successful lectures only; failed targets retain their old data.
        return 0


def apply(candidate_path, remote_path):
    if Path(candidate_path).resolve() == Path(remote_path).resolve():
        raise ValueError("Candidate and latest remote must be different files")
    with readonly(candidate_path) as candidate:
        candidate.row_factory = sqlite3.Row
        successful = {row[0] for row in candidate.execute(
            f"SELECT sub_id FROM {SUCCESS_TABLE}")}
        replacements = []
        for course_id, sub_id in TARGETS:
            if sub_id not in successful:
                continue
            row = candidate.execute("SELECT * FROM lectures WHERE sub_id=?", (sub_id,)).fetchone()
            if not completed(row, course_id):
                raise ValueError("Incomplete candidate marked successful")
            replacements.append((course_id, sub_id, row))

    if not replacements:
        raise ValueError("No successfully recovered lectures to apply")

    # No Database() here: its startup migration changes unrelated rows.
    remote = sqlite3.connect(Path(remote_path).resolve().as_uri() + "?mode=rw", uri=True)
    try:
        with remote:
            for course_id, sub_id, row in replacements:
                assignments = ", ".join(f"{field}=?" for field in FIELDS)
                cursor = remote.execute(
                    f"UPDATE lectures SET {assignments} WHERE course_id=? AND sub_id=?",
                    [*(row[field] for field in FIELDS), course_id, sub_id],
                )
                if cursor.rowcount != 1:
                    raise ValueError("Target missing from latest remote database")
    finally:
        remote.close()
    emit(applied_lectures=len(replacements), emailed_at_preserved=True,
         ppt_cache_rows_applied=0)
    return len(replacements)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("process", "apply"))
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        if args.mode == "process":
            return process(args.source, args.destination)
        apply(args.source, args.destination)
        return 0
    except Exception as exc:
        emit(success=False, error_type=type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
