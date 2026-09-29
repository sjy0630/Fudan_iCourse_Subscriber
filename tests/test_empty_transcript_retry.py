"""Regression tests for lectures whose audio yielded no recognized speech."""

import os
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock

from src.data.database import Database


# The runner's OCR and ASR dependencies are large native packages. These
# stubs isolate its state transitions while using the real SQLite database.
ppt_module = types.ModuleType("src.pipeline.ppt_pipeline")
ppt_module.PPTPipeline = Mock
sys.modules["src.pipeline.ppt_pipeline"] = ppt_module
asr_module = types.ModuleType("src.ai.transcriber")
asr_module.IncompleteAudioError = type("IncompleteAudioError", (Exception,), {})
asr_module.NoAudioStreamError = type("NoAudioStreamError", (Exception,), {})
sys.modules["src.ai.transcriber"] = asr_module
bucketer_module = types.ModuleType("src.ai.bucketer")
bucketer_module.assemble = Mock()
sys.modules["src.ai.bucketer"] = bucketer_module

from src.pipeline.lecture_runner import LectureRunner  # noqa: E402


class EmptyTranscriptRetryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "lectures.db")
        self.db = Database(self.path)
        self.db.insert_lecture("670119", "37664", "模拟电子线路", "2026-09-28")

    def tearDown(self):
        self.db.conn.close()
        self.temp.cleanup()

    def test_empty_transcript_remains_eligible_for_retry(self):
        runner = LectureRunner.__new__(LectureRunner)
        runner._db = self.db
        runner._client = Mock()
        runner._reporter = Mock()
        runner._ppt = Mock()
        runner._ppt.submit.return_value.drain.return_value = None
        runner._schedule_next = Mock()
        runner._get_transcript = Mock(return_value=("", []))
        runner._release_audio = Mock()

        result = runner.run("37664", "模拟电子线路", {
            "sub_id": "670119", "sub_title": "9月28日", "date": "2026-09-28",
        })

        self.assertIsNone(result)
        row = self.db.get_lecture("670119")
        self.assertIsNone(row["processed_at"])
        self.assertEqual(row["error_stage"], "empty_transcript")
        self.assertEqual(row["error_count"], 1)
        self.assertEqual([r["sub_id"] for r in self.db.get_unprocessed_lectures("37664")], ["670119"])

    def test_old_processed_blank_transcript_is_requeued(self):
        self.db.update_transcript("670119", "")
        self.db.mark_processed("670119")
        self.db.conn.close()

        self.db = Database(self.path)

        row = self.db.get_lecture("670119")
        self.assertIsNone(row["processed_at"])
        self.assertEqual([r["sub_id"] for r in self.db.get_unprocessed_lectures("37664")], ["670119"])

    def test_three_empty_results_stop_automatic_retries(self):
        for _ in range(2):
            self.db.update_error("670119", "empty_transcript", "no recognized speech")
        self.assertNotIn("670119", self.db.get_ineligible_sub_ids("37664"))

        self.db.update_error("670119", "empty_transcript", "no recognized speech")
        self.assertIn("670119", self.db.get_ineligible_sub_ids("37664"))


if __name__ == "__main__":
    unittest.main()
