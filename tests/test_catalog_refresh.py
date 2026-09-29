"""Regression test for courses added after a semester was first catalogued."""

import ast
import pathlib
import time
import types
import unittest


SOURCE = pathlib.Path(__file__).resolve().parents[1] / "main.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
CRAWL = next(
    node for node in TREE.body
    if isinstance(node, ast.FunctionDef)
    and node.name == "_crawl_semester_catalog"
)
MODULE = ast.Module(
    body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), CRAWL],
    type_ignores=[],
)
ast.fix_missing_locations(MODULE)
namespace = {"time": time, "_check_session": lambda client: None}
exec(compile(MODULE, str(SOURCE), "exec"), namespace)
crawl = namespace["_crawl_semester_catalog"]


class CatalogRefreshTest(unittest.TestCase):
    def test_new_course_appears_in_already_known_current_semester(self):
        class Client:
            fetched = []

            def discover_terms(self):
                return [
                    {"code": "27", "name": "2026-20271", "count": 2},
                    {"code": "26", "name": "2025-2026暑期", "count": 1},
                ]

            def list_semester_courses(self, code):
                self.fetched.append(code)
                return [
                    {"course_id": "1", "title": "已有课程"},
                    {"course_id": "2", "title": "新增课程"},
                ]

        class DB:
            rows = {"1": "已有课程"}

            def list_catalog_terms(self):
                return {"2026-20271", "2025-2026暑期"}

            def upsert_all_courses_for_term(self, term, rows):
                self.rows = {r["course_id"]: r["title"] for r in rows}
                return 0, len(rows)

        reporter = types.SimpleNamespace(
            info=lambda *_: None,
            crawl_courses_start=lambda *_: None,
            crawl_courses_done=lambda *_: None,
            crawl_courses_failed=lambda *_: None,
        )
        client, db = Client(), DB()

        crawl(client, db, reporter)

        self.assertEqual(client.fetched, ["27"])
        self.assertEqual(db.rows["2"], "新增课程")


if __name__ == "__main__":
    unittest.main()
