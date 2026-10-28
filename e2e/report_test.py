"""Checks the optview2_report outputs produced with a real clang 19."""

import json
import os
import sys
import unittest

FILES = sys.argv[1:]
sys.argv = sys.argv[:1]


def _find(suffix, prefix="report."):
    hits = [f for f in FILES if os.path.basename(f).startswith(prefix) and f.endswith(suffix)]
    assert len(hits) == 1, (suffix, prefix, FILES)
    return hits[0]


class E2ETest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(_find(".summary.json"), encoding="utf-8") as f:
            cls.summary = json.load(f)

    def _findings(self, function_prefix, category=None):
        return [
            f
            for f in self.summary["findings"]
            if f["function"].startswith(function_prefix) and (category is None or f["category"] == category)
        ]

    def test_slow_variant_has_aliasing_reload(self):
        self.assertTrue(
            self._findings("e2e::scale_slow", "aliasing-loop-reload"), json.dumps(self.summary["findings"], indent=1)
        )

    def test_fast_variant_is_clean(self):
        self.assertEqual(self._findings("e2e::scale_fast", "aliasing-loop-reload"), [])
        self.assertEqual(self._findings("e2e::scale_fast", "loop-not-vectorized"), [])

    def test_fp_reduction_not_vectorized_with_reason(self):
        dot = self._findings("e2e::dot", "loop-not-vectorized")
        self.assertEqual(len(dot), 1, self.summary["findings"])
        self.assertTrue(any(r.startswith("CantReorderFPOps") for r in dot[0]["reasons"]))

    def test_cross_tu_calls_from_main(self):
        msgs = [f["message"] for f in self._findings("main", "inline-no-definition")]
        self.assertTrue(any("e2e::scale_slow will not be inlined" in m for m in msgs), msgs)

    def test_no_system_or_external_files(self):
        for f in self.summary["findings"]:
            self.assertTrue(f["file"].startswith("src/"), f["file"])

    def test_html_report(self):
        html_dir = _find("_html", prefix="report_html")
        with open(os.path.join(html_dir, "index.html"), encoding="utf-8") as f:
            self.assertIn("LoadWithLoopInvariantAddressInvalidated", f.read())
        self.assertTrue(os.path.exists(os.path.join(html_dir, "src_aliasing.cc.html")))

    def test_markdown(self):
        with open(_find(".summary.md"), encoding="utf-8") as f:
            md = f.read()
        self.assertIn("## Top sweet spots", md)
        self.assertIn("src/aliasing.cc", md)

    def test_include_regex_and_no_html(self):
        with open(_find(".summary.json", prefix="report_no_html."), encoding="utf-8") as f:
            summary = json.load(f)
        self.assertTrue(summary["findings"])
        self.assertTrue(all(f["file"].startswith("src/aliasing") for f in summary["findings"]))
        self.assertEqual(summary["stats"]["record_files"], 1)  # main.cc excluded by exclude_tu_regex
        self.assertFalse([f for f in summary["findings"] if f["function"] == "main"])
        self.assertFalse([f for f in FILES if os.path.basename(f) == "report_no_html_html"])


if __name__ == "__main__":
    unittest.main()
