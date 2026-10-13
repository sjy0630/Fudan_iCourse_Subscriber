"""Synthetic, offline checks for the read-only one-time diagnostic."""

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from src.data.schema import SCHEMA_SQL

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/diagnose_mixed_ethics_summary.py"
SPEC = importlib.util.spec_from_file_location("mixed_diagnostic", SCRIPT)
diagnostic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnostic)


class MixedDiagnosticTest(unittest.TestCase):
    def test_layers_matches_privacy_and_database_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic.db"
            transcript = "概率二项" + "文" * 1196 + "阿多诺马克思美学"
            slide = "PRIVATE_LESSON_BODY" + "".join(f"知识点{i:03d}概率二项" for i in range(30))
            with sqlite3.connect(path) as conn:
                conn.executescript(SCHEMA_SQL)
                conn.executemany("INSERT INTO courses VALUES (?,?,?)", [
                    ("38713", "思想道德与法治", "teacher A"),
                    ("40000", "对照课程", "teacher B"),
                ])
                conn.executemany(
                    "INSERT INTO lectures (sub_id,course_id,sub_title,date,transcript,summary,"
                    "processed_at,summary_model,error_msg) VALUES (?,?,?,?,?,?,?,?,?)", [
                        ("target", "38713", "2026-09-09第8-10节", "2026-09-09", transcript,
                         "法治道德", "2026-09-10T00:00:00", "test/model", "PRIVATE_ERROR_SECRET"),
                        ("other", "40000", "2026-09-08第1-2节", "2026-09-08", transcript,
                         "其他结果", "2026-09-10T00:00:00", "test/model", None),
                    ])
                conn.executemany(
                    "INSERT INTO ppt_pages (sub_id,page_num,created_sec,pptimgurl,text,ocr_status) "
                    "VALUES (?,?,?,?,?,?)", [
                        ("target", 1, 600, "https://one.invalid/PRIVATE_IMAGE_PATH?token=SECRET1", slide, "done"),
                        ("target", 2, 1200, "https://one.invalid/different", "阿多诺美学", "done"),
                        ("other", 8, 900, "https://two.invalid/PRIVATE_IMAGE_PATH?token=SECRET2", slide + "新文字", "done"),
                    ])
            before = hashlib.sha256(path.read_bytes()).digest()
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                self.assertEqual(diagnostic.diagnose(path), 0)
            self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), before)
            output = captured.getvalue()
            for private in ("PRIVATE_LESSON_BODY", "PRIVATE_ERROR_SECRET", "PRIVATE_IMAGE_PATH", "SECRET1", "https://"):
                self.assertNotIn(private, output)
            events = [json.loads(line) for line in output.splitlines()]
            blocks = [e for e in events if e["event"] == "text_block" and e["field"] == "transcript"]
            self.assertEqual(blocks[0]["keywords"], {"概率": 1, "二项": 1})
            self.assertEqual(blocks[1]["keywords"], {"阿多诺": 1, "马克思": 1, "美学": 1})
            identical = [e for e in events if e["event"] == "identical_field"]
            self.assertEqual([(e["sub_id"], e["field"]) for e in identical], [("other", "transcript")])
            path_matches = [e for e in events if e["event"] == "same_image_path"]
            self.assertEqual(path_matches[0]["target_pages"], [1])
            self.assertEqual(path_matches[0]["other_time_range"], [900, 900])
            overlap = [e for e in events if e["event"] == "similar_ppt_text"]
            self.assertEqual(overlap[0]["sub_id"], "other")
            self.assertGreater(overlap[0]["max_jaccard"], 0.8)
            pages = [e for e in events if e["event"] == "ppt_page"]
            self.assertEqual([(p["page_num"], p["seconds"]) for p in pages], [(1, 600), (2, 1200)])

    def test_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "missing.db"
            with self.assertRaises(sqlite3.OperationalError):
                diagnostic.diagnose(path)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
