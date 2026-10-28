"""Component tests: tools/report.py CLI on real clang output (tests/testdata) and the examples/ sources."""

import json
import os
import tempfile
import unittest

import report

_PKG = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_PKG)  # runfiles root of the optview2_bazel repo (contains examples/)
_CLANG15 = os.path.join(_PKG, "testdata", "clang15")
_FILTERS = os.path.join(_PKG, "testdata", "filters.opt.yaml")


def _run(*argv):
    args = report.build_parser().parse_args(list(argv))
    return report.run(args)


class ExampleSummaryTest(unittest.TestCase):
    """Real clang output for examples/*.cc (see examples/README.md)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.summary = _run(
            "--summary-json", os.path.join(cls.tmp, "s.json"), "--summary-md", os.path.join(cls.tmp, "s.md"), _CLANG15
        )

    def test_stats(self):
        stats = self.summary["stats"]
        self.assertEqual(stats["record_files"], 3)
        self.assertGreater(stats["raw_remarks"], 100)
        self.assertGreater(stats["locations"], 5)
        self.assertFalse(stats["has_hotness"])

    def test_top_hotspots_are_loop_aliasing(self):
        top = self.summary["findings"][:3]
        self.assertEqual({h["category"] for h in top}, {"aliasing-loop-reload"})
        self.assertEqual(
            {(h["file"], h["line"]) for h in top},
            {("examples/kernels.cc", 7), ("examples/kernels.cc", 34), ("examples/kernels.cc", 35)},
        )

    def test_scale_down_by_value_is_clean(self):
        self.assertTrue(
            any(
                f["function"] == "examples::scale_down" and f["category"] == "aliasing-loop-reload"
                for f in self.summary["findings"]
            )
        )
        self.assertFalse(
            any(
                f["function"].startswith("examples::scale_down_by_value") and f["category"] == "aliasing-loop-reload"
                for f in self.summary["findings"]
            )
        )

    def test_vectorizer_reasons_attached(self):
        sum_loop = [
            h
            for h in self.summary["findings"]
            if h["category"] == "loop-not-vectorized" and h["function"].startswith("examples::sum")
        ]
        self.assertEqual(len(sum_loop), 1)
        self.assertTrue(any(r.startswith("CantReorderFPOps") for r in sum_loop[0]["reasons"]))

        gain_loop = [
            h
            for h in self.summary["findings"]
            if h["category"] == "loop-not-vectorized" and h["function"].startswith("examples::apply_gain_all")
        ]
        self.assertTrue(any(r.startswith("CantVectorizeLibcall") for r in gain_loop[0]["reasons"]))

    def test_clobbers_table(self):
        clobbers = {c["key"]: c for c in self.summary["clobbers"]}
        self.assertIn("store @ examples/kernels.cc:7", clobbers)
        self.assertIn("store @ examples/kernels.cc:34", clobbers)

    def test_cross_tu_call_detected(self):
        msgs = [f["message"] for f in self.summary["findings"] if f["category"] == "inline-no-definition"]
        self.assertTrue(any("apply_gain will not be inlined into" in m for m in msgs), msgs)

    def test_markdown_and_json_written(self):
        with open(os.path.join(self.tmp, "s.md"), encoding="utf-8") as f:
            md = f.read()
        self.assertIn("## Top sweet spots", md)
        self.assertIn("examples/kernels.cc:7", md)
        self.assertIn("### Loop-invariant load is reloaded every iteration (aliasing)", md)
        with open(os.path.join(self.tmp, "s.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["stats"], self.summary["stats"])

    def test_parallel_matches_serial(self):
        parallel = _run("-j", "3", _CLANG15)
        self.assertEqual(parallel["findings"], self.summary["findings"])


class TranslationUnitFilterTest(unittest.TestCase):
    def test_exclude_tu(self):
        summary = _run("--exclude-tu-regex", "main\\.opt\\.yaml$", _CLANG15)
        self.assertEqual(summary["stats"]["record_files"], 2)
        self.assertFalse([f for f in summary["findings"] if f["file"] == "examples/main.cc"])

    def test_include_tu(self):
        summary = _run("--include-tu-regex", "/main\\.opt\\.yaml$", _CLANG15)
        self.assertEqual(summary["stats"]["record_files"], 1)
        self.assertEqual({f["file"] for f in summary["findings"]}, {"examples/main.cc"})
        self.assertEqual(summary["filters"]["include_tu_regex"], "/main\\.opt\\.yaml$")


class HtmlTest(unittest.TestCase):
    def test_html_report(self):
        out = tempfile.mkdtemp()
        _run("--output-dir", out, "--source-dir", _ROOT, _CLANG15)
        index = os.path.join(out, "index.html")
        self.assertTrue(os.path.exists(index))
        with open(index, encoding="utf-8") as f:
            self.assertIn("LoadWithLoopInvariantAddressInvalidated", f.read())
        page = os.path.join(out, "examples_kernels.cc.html")
        with open(page, encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("Unable to locate file", content)
        self.assertIn("scale_down", content)
        self.assertTrue(os.path.exists(os.path.join(out, "assets", "style.css")))

    def test_empty_input_writes_placeholder(self):
        out = tempfile.mkdtemp()
        summary = _run("--output-dir", out, "--include-file-regex", "^does/not/exist", _CLANG15)
        self.assertEqual(summary["stats"]["locations"], 0)
        with open(os.path.join(out, "index.html"), encoding="utf-8") as f:
            self.assertIn("No missed-optimization remarks", f.read())


class FilterTest(unittest.TestCase):
    def _files(self, *argv):
        summary = _run(*argv, _FILTERS)
        return sorted({f["file"] for f in summary["findings"]}), summary

    def test_default_excludes_external_generated_and_system(self):
        files, summary = self._files()
        self.assertEqual(files, ["other/b.cc", "pkg/a.cc"])
        names = {n for f in summary["findings"] for n in f["names"]}
        # OptView2 config.yaml: SpillReloadCopies + std:: callees are filtered.
        self.assertNotIn("SpillReloadCopies", names)
        self.assertNotIn("NoDefinition", names)
        # Passed remarks never become findings.
        self.assertNotIn("Inlined", names)

    def test_without_optview2_config_filters(self):
        _, summary = self._files("--exclude-names=", "--exclude-text=")
        names = {n for f in summary["findings"] for n in f["names"]}
        self.assertIn("SpillReloadCopies", names)
        self.assertIn("NoDefinition", names)

    def test_include_regex(self):
        files, _ = self._files("--include-file-regex", "^pkg/")
        self.assertEqual(files, ["pkg/a.cc"])

    def test_custom_exclude_file_regex(self):
        files, _ = self._files("--exclude-file-regex", "^bazel-out/")
        self.assertIn("external/thirdparty/lib.h", files)
        self.assertIn("/usr/include/c++/13/bits/stl_vector.h", files)
        self.assertNotIn("bazel-out/k8-opt/bin/pkg/gen.pb.h", files)

    def test_exclude_names_replaces_optview2_default(self):
        _, summary = self._files("--exclude-names", "LoadWithLoopInvariantAddress|LoadClobbered")
        names = {n for f in summary["findings"] for n in f["names"]}
        self.assertEqual(names, {"SpillReloadCopies"})  # no longer dropped by the config.yaml default

    def test_dot_slash_paths_are_normalized_and_deduplicated(self):
        _, summary = self._files()
        licm = [f for f in summary["findings"] if f["file"] == "pkg/a.cc" and f["line"] == 7]
        self.assertEqual(len(licm), 1)
        self.assertFalse([f for f in summary["findings"] if f["file"].startswith("./")])
        gvn = [f for f in summary["findings"] if f["names"] == ["LoadClobbered"]]
        self.assertEqual(gvn[0]["file"], "pkg/a.cc")
        self.assertIn("store (line 8:3)", gvn[0]["message"])
        self.assertEqual(gvn[0]["clobbered_by"], "store @ pkg/a.cc:8")

    def test_html_links_use_normalized_paths(self):
        out = tempfile.mkdtemp()
        _run("--output-dir", out, _FILTERS)
        pages = sorted(p for p in os.listdir(out) if p.endswith(".html"))
        self.assertEqual(pages, ["index.html", "other_b.cc.html", "pkg_a.cc.html"])

    def test_collapsed_finding_shows_most_important_message(self):
        _, summary = self._files()
        b = [f for f in summary["findings"] if f["file"] == "other/b.cc"]
        self.assertEqual(len(b), 1)
        self.assertEqual(b[0]["remarks"], 2)
        self.assertIn("may invalidate its value", b[0]["message"])

    def test_hotness_ranking(self):
        _, summary = self._files()
        self.assertTrue(summary["stats"]["has_hotness"])
        self.assertEqual(summary["findings"][0]["file"], "pkg/a.cc")
        self.assertEqual(summary["findings"][0]["score"], 5.0)
        self.assertEqual(summary["findings"][1]["score"], 0.5)


if __name__ == "__main__":
    unittest.main()
