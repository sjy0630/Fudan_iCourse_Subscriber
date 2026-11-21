import tempfile
import unittest
from pathlib import Path

from scripts.merge_db import merge
from src.data.database import Database


class EmptyTranscriptRetryTests(unittest.TestCase):
    def test_legacy_empty_transcript_is_recovered_and_retry_count_survives_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            local_path = str(Path(directory) / "local.db")
            remote_path = str(Path(directory) / "remote.db")
            remote = Database(remote_path)
            for sub_id in ("empty", "video_only", "summarized"):
                remote.insert_lecture(sub_id, "course", sub_id, "")
                remote.mark_processed(sub_id)
            remote.update_transcript("empty", "")
            remote.update_error("video_only", "transcribe", "no audio stream")
            remote.update_transcript("summarized", "")
            remote.update_summary("summarized", "Existing summary", "provider")
            local_db = Database(local_path)
            remote.conn.backup(local_db.conn)
            local_db.conn.close()
            remote.conn.close()

            local_db = Database(local_path)
            self.assertEqual(local_db.get_processed_sub_ids("course"), {"video_only", "summarized"})
            for _ in range(3):
                local_db.update_error("empty", "empty_transcript", "No text")
            self.assertIn("empty", local_db.get_ineligible_sub_ids("course"))
            local_db.conn.close()

            merge(local_path, remote_path)
            remote = Database(remote_path)
            try:
                lecture = remote.get_lecture("empty")
                self.assertIsNone(lecture["processed_at"])
                self.assertEqual(lecture["error_count"], 3)
                self.assertEqual(lecture["error_stage"], "empty_transcript")
                self.assertIn("empty", remote.get_ineligible_sub_ids("course"))
            finally:
                remote.conn.close()
