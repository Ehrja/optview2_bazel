"""Unit tests for tools/report.py (one TestCase per pipeline step)."""

import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest

import optrecord
import yaml

import report
import triage


# --------------------------------------------------------------------------------------
# Helpers: build clang optimization-record YAML documents
# --------------------------------------------------------------------------------------
def _loc(file: str, line: int, column: int = 1) -> dict:
    return {"File": file, "Line": line, "Column": column}


def _arg_lines(arg: dict) -> list[str]:
    lines = []
    for i, (key, value) in enumerate(arg.items()):
        if key == "DebugLoc":
            value = f"{{ File: '{value['File']}', Line: {value['Line']}, Column: {value['Column']} }}"
        else:
            value = f"'{value}'"
        lines.append(("  - " if i == 0 else "    ") + f"{key}: {value}")
    return lines


def doc(
    kind="Missed",
    pass_name="gvn",
    name="LoadClobbered",
    file="pkg/a.cc",
    line=1,
    column=1,
    function="_Z1fv",
    hotness=0,
    args=({"String": "msg"},),
) -> str:
    """One remark document. `file=""` produces a remark without DebugLoc."""
    lines = [f"--- !{kind}", f"Pass: {pass_name}", f"Name: {name}"]
    if file:
        lines.append(f"DebugLoc: {{ File: '{file}', Line: {line}, Column: {column} }}")
    lines.append(f"Function: {function}")
    if hotness:
        lines.append(f"Hotness: {hotness}")
    lines.append("Args:")
    for arg in args:
        lines += _arg_lines(arg)
    return "\n".join(lines + ["..."]) + "\n"


def remarks(*docs: str) -> list:
    """Parse and canonicalize remarks like load_file does."""
    parsed = list(yaml.load_all("".join(docs), Loader=optrecord.Loader))
    for remark in parsed:
        remark.canonicalize()
    return parsed


def remark(**kwargs):
    return remarks(doc(**kwargs))[0]


def write(directory: str, relpath: str, content: str) -> str:
    path = os.path.join(directory, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def records(missed=(), notes=(), max_hotness=0) -> report.Records:
    return report.Records(
        missed={r.key: r for r in missed},
        notes=list(notes),
        files=1,
        raw=len(missed) + len(notes),
        max_hotness=max_hotness,
    )


LICM = {"pass_name": "licm", "name": "LoadWithLoopInvariantAddressInvalidated"}
LOOP = {"pass_name": "loop-vectorize", "name": "MissedDetails", "args": ({"String": "loop not vectorized"},)}


def vectorize_note(name, line, function="_Z1fv", file="pkg/a.cc", text="why"):
    return remark(kind="Analysis", pass_name="loop-vectorize", name=name, line=line, file=file, function=function,
                  args=({"String": text},))  # fmt: skip


def stack_note(function, size, line=1):
    return remark(kind="Analysis", pass_name="prologepilog", name="StackSize", function=function, line=line,
                  args=({"NumStackBytes": str(size)}, {"String": " stack bytes"}))  # fmt: skip


def stack_doc():
    return doc(kind="Analysis", pass_name="prologepilog", name="StackSize",
               args=({"NumStackBytes": "16"}, {"String": " stack bytes"}))  # fmt: skip


def finding(**kwargs) -> report.Finding:
    values = {
        "file": "pkg/a.cc",
        "line": 1,
        "category": "aliasing-loop-reload",
        "function": "f",
        "message": "msg",
        "score": 5.0,
        "names": ["X"],
        "reasons": [],
        "clobbered_by": "",
    }
    return report.Finding(**(values | kwargs))


# --------------------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------------------
class MatchHelpersTest(unittest.TestCase):
    def test_matches(self):
        self.assertFalse(report._matches("", "anything"))
        self.assertTrue(report._matches("b", "abc"))
        self.assertFalse(report._matches("^b", "abc"))

    def test_included(self):
        self.assertTrue(report._included("", "anything"))
        self.assertTrue(report._included("^a", "abc"))
        self.assertFalse(report._included("^b", "abc"))


class FiltersTest(unittest.TestCase):
    def test_default_file_filter(self):
        filters = report.Filters()
        self.assertTrue(filters.keep_file("pkg/a.cc"))
        for path in ("external/lib/a.h", "bazel-out/k8-opt/bin/x.pb.h", "/usr/include/vector"):
            with self.subTest(path=path):
                self.assertFalse(filters.keep_file(path))

    def test_include_and_exclude_file(self):
        filters = report.Filters(include_file_regex="^pkg/", exclude_file_regex="_test")
        self.assertTrue(filters.keep_file("pkg/a.cc"))
        self.assertFalse(filters.keep_file("other/a.cc"))
        self.assertFalse(filters.keep_file("pkg/a_test.cc"))

    def test_keep_tu(self):
        filters = report.Filters(include_tu_regex="/pkg/", exclude_tu_regex="_test\\.cc")
        self.assertTrue(filters.keep_tu("bin/pkg/_optview2/lib/pkg/a.cc.opt.yaml"))
        self.assertFalse(filters.keep_tu("bin/pkg/_optview2/lib/pkg/a_test.cc.opt.yaml"))
        self.assertFalse(filters.keep_tu("bin/other/_optview2/lib/other/a.cc.opt.yaml"))
        self.assertTrue(report.Filters().keep_tu("anything"))

    def test_keep_by_name_and_text(self):
        r = remark(pass_name="inline", name="NoDefinition", args=({"Callee": "_ZNSt6vectorIiSaIiEE5clearEv"},))
        self.assertTrue(report.Filters().keep(r))
        self.assertFalse(report.Filters(exclude_names="^NoDef").keep(r))
        # The text filter sees demangled names.
        self.assertFalse(report.Filters(exclude_text="^std::vector").keep(r))

    def test_keep_applies_file_filter(self):
        self.assertFalse(report.Filters().keep(remark(file="external/x.h")))


# --------------------------------------------------------------------------------------
# Remark helpers
# --------------------------------------------------------------------------------------
class RemarkHelpersTest(unittest.TestCase):
    def setUp(self):
        self.remark = remark(
            args=(
                {"String": "load not eliminated because it is clobbered by "},
                {"ClobberedBy": "store", "DebugLoc": _loc("pkg/a.cc", 8, 3)},
            )
        )

    def test_args_converts_debugloc_to_dict(self):
        args = report._args(self.remark)
        self.assertEqual(args[1], {"ClobberedBy": "store", "DebugLoc": _loc("pkg/a.cc", 8, 3)})

    def test_arg(self):
        self.assertEqual(report._arg(self.remark, "ClobberedBy")["DebugLoc"]["Line"], 8)
        self.assertEqual(report._arg(self.remark, "Callee"), {})

    def test_message_same_file_location(self):
        self.assertEqual(report.message(self.remark), "load not eliminated because it is clobbered by store (line 8:3)")

    def test_message_other_file_location(self):
        r = remark(args=({"ClobberedBy": "call", "DebugLoc": _loc("lib/math.h", 51, 40)},))
        self.assertEqual(report.message(r), "call (lib/math.h:51:40)")

    def test_message_demangles_and_omits_caller_location(self):
        r = remark(
            pass_name="inline",
            name="NoDefinition",
            args=(
                {"Callee": "_Z3foov"},
                {"String": " will not be inlined into "},
                {"Caller": "_Z3barv", "DebugLoc": _loc("pkg/a.cc", 4, 0)},
            ),
        )
        self.assertEqual(report.message(r), "foo will not be inlined into bar")

    def test_clobbered_by(self):
        self.assertEqual(report._clobbered_by(self.remark), "store @ pkg/a.cc:8")
        self.assertEqual(report._clobbered_by(remark()), "")
        self.assertEqual(report._clobbered_by(remark(args=({"ClobberedBy": "call"},))), "")


class NormalizeTest(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(report._normalize("./pkg/a.h"), "pkg/a.h")
        self.assertEqual(report._normalize("pkg/../other/a.h"), "other/a.h")
        self.assertEqual(report._normalize("external/x/./y.h"), "external/x/y.h")
        self.assertEqual(report._normalize("pkg/a.h"), "pkg/a.h")

    def test_normalize_loc(self):
        self.assertEqual(
            report._normalize_loc((("File", "./a.h"), ("Line", 1))),
            (("File", "a.h"), ("Line", 1)),
        )

    def test_normalize_paths_updates_remark_and_arguments(self):
        r = remark(file="./pkg/a.cc", args=({"ClobberedBy": "store", "DebugLoc": _loc("./pkg/../pkg/a.cc", 2)},))
        report._normalize_paths(r)
        self.assertEqual(r.File, "pkg/a.cc")
        self.assertEqual(report._arg(r, "ClobberedBy")["DebugLoc"]["File"], "pkg/a.cc")


# --------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------
class FindRecordFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.a = write(self.tmp, "x/_optview2/lib/x/a.cc.opt.yaml", "")
        self.b = write(self.tmp, "x/_optview2/lib/x/sub/b_test.cc.opt.yaml", "")
        write(self.tmp, "x/_optview2/lib/x/a.cc.o", "")

    def test_walks_directories_and_ignores_other_files(self):
        self.assertEqual(report.find_record_files([self.tmp], report.Filters()), [self.a, self.b])

    def test_explicit_files_are_deduplicated_and_sorted(self):
        self.assertEqual(report.find_record_files([self.b, self.a, self.b], report.Filters()), [self.a, self.b])

    def test_tu_filters(self):
        self.assertEqual(report.find_record_files([self.tmp], report.Filters(exclude_tu_regex="_test")), [self.a])
        self.assertEqual(report.find_record_files([self.tmp], report.Filters(include_tu_regex="/sub/")), [self.b])


class IsNoteTest(unittest.TestCase):
    def test_is_note(self):
        self.assertTrue(report._is_note(vectorize_note("CantReorderFPOps", 1)))
        self.assertTrue(report._is_note(stack_note("_Z1fv", 8)))
        self.assertFalse(report._is_note(remark(kind="Analysis", pass_name="asm-printer", name="InstructionCount")))
        self.assertFalse(report._is_note(remark(**LOOP)))


class LoadFileTest(unittest.TestCase):
    def setUp(self):
        content = "".join(
            [
                doc(**LICM, line=1),
                doc(kind="Failure", pass_name="inline", name="TooCostly", line=2),
                doc(kind="Passed", pass_name="inline", name="Inlined", line=3),
                doc(kind="Analysis", pass_name="loop-vectorize", name="CantReorderFPOps", line=4),
                doc(kind="Analysis", pass_name="asm-printer", name="InstructionCount", line=5),
                doc(file=""),  # no DebugLoc
                doc(**LICM, file="external/lib.h"),
                doc(kind="Analysis", pass_name="loop-vectorize", name="CantReorderFPOps", file="external/lib.h"),
                doc(**LICM, file="./pkg/b.cc"),
            ]
        )
        self.path = write(tempfile.mkdtemp(), "a.opt.yaml", content)

    def test_load_file(self):
        missed, notes, raw = report.load_file(self.path, report.Filters())
        self.assertEqual(raw, 9)
        self.assertEqual([(r.File, r.Name) for r in missed], [
            ("pkg/a.cc", "LoadWithLoopInvariantAddressInvalidated"),
            ("pkg/a.cc", "TooCostly"),
            ("pkg/b.cc", "LoadWithLoopInvariantAddressInvalidated"),
        ])  # fmt: skip
        self.assertEqual([(r.File, r.Name) for r in notes], [("pkg/a.cc", "CantReorderFPOps")])

    def test_name_filter_does_not_apply_to_notes(self):
        missed, notes, _ = report.load_file(self.path, report.Filters(exclude_names="."))
        self.assertEqual(missed, [])
        self.assertEqual(len(notes), 1)


class LoadRecordsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        header_remark = doc(**LICM, file="pkg/common.h", line=7, hotness=10)
        self.files = [
            write(tmp, "a.opt.yaml", header_remark + doc(line=1, hotness=40)),
            write(tmp, "b.opt.yaml", header_remark + stack_doc()),
        ]

    def test_merges_and_deduplicates(self):
        rec = report.load_records(self.files, report.Filters(), jobs=1)
        self.assertEqual(rec.files, 2)
        self.assertEqual(rec.raw, 4)
        self.assertEqual(len(rec.missed), 2)  # the header remark is reported once
        self.assertEqual(len(rec.notes), 1)
        self.assertEqual(rec.max_hotness, 40)
        self.assertTrue(all(r.max_hotness == 40 for r in rec.missed.values()))

    def test_parallel_equals_serial(self):
        serial = report.load_records(self.files, report.Filters(), jobs=1)
        parallel = report.load_records(self.files, report.Filters(), jobs=2)
        self.assertEqual(list(serial.missed), list(parallel.missed))
        self.assertEqual(serial.raw, parallel.raw)

    def test_no_files(self):
        rec = report.load_records([], report.Filters(), jobs=4)
        self.assertEqual((rec.missed, rec.notes, rec.files, rec.raw, rec.max_hotness), ({}, [], 0, 0, 0))


# --------------------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------------------
class VectorizeReasonsTest(unittest.TestCase):
    def setUp(self):
        self.loops = [remark(**LOOP, line=10), remark(**LOOP, line=20)]

    def test_reason_attaches_to_closest_preceding_loop(self):
        reasons = report._vectorize_reasons(self.loops, [vectorize_note("CantReorderFPOps", 22)])
        self.assertEqual(list(reasons), [("pkg/a.cc", 20)])
        self.assertTrue(reasons[("pkg/a.cc", 20)][0].startswith("CantReorderFPOps: FP reduction"))
        self.assertTrue(reasons[("pkg/a.cc", 20)][0].endswith("(line 22:1)"))

    def test_reason_in_loop_header_line(self):
        reasons = report._vectorize_reasons(self.loops, [vectorize_note("CantComputeNumberOfIterations", 10)])
        self.assertEqual(list(reasons), [("pkg/a.cc", 10)])

    def test_unmatched_and_ignored_notes(self):
        notes = [
            vectorize_note("CantReorderFPOps", 5),  # before every loop
            vectorize_note("CantReorderFPOps", 12, function="_Z1gv"),  # other function
            vectorize_note("InterleavingNotBeneficial", 12),  # not a reason
        ]
        self.assertEqual(report._vectorize_reasons(self.loops, notes), {})

    def test_unknown_reason_uses_message_and_duplicates_are_dropped(self):
        notes = [vectorize_note("BrandNewReason", 11, text="new text")] * 2
        self.assertEqual(
            report._vectorize_reasons(self.loops, notes), {("pkg/a.cc", 10): ["BrandNewReason: new text (line 11:1)"]}
        )


class ScoreTest(unittest.TestCase):
    def test_without_hotness_score_is_weight(self):
        self.assertEqual(report._score(remark(**LICM), max_hotness=0), 5.0)

    def test_hotness_scales_score(self):
        self.assertEqual(report._score(remark(**LICM, hotness=50), max_hotness=100), 2.5)
        self.assertEqual(report._score(remark(**LICM), max_hotness=100), 0.05)  # clamped to 1 %

    def test_uses_callee(self):
        r = remark(pass_name="inline", name="NoDefinition", args=({"Callee": "printf"},))
        self.assertEqual(report._score(r, max_hotness=0), 0.5)


class CollectFindingsTest(unittest.TestCase):
    def test_groups_by_file_line_and_category(self):
        rec = records(
            [
                remark(
                    pass_name="licm",
                    name="LoadWithLoopInvariantAddressCondExecuted",
                    line=3,
                    column=1,
                    args=({"String": "conditional"},),
                ),  # fmt: skip
                remark(**LICM, line=3, column=9, args=({"String": "invalidated"},)),
                remark(line=3, args=({"ClobberedBy": "store", "DebugLoc": _loc("pkg/a.cc", 2)},)),
                remark(**LICM, line=4),
            ]
        )
        findings = report.collect_findings(rec)
        self.assertEqual([(f.line, f.category, f.remarks) for f in findings], [
            (3, "aliasing-loop-reload", 2),
            (4, "aliasing-loop-reload", 1),
            (3, "aliasing-redundant-load", 1),
        ])  # fmt: skip
        first = findings[0]
        self.assertEqual(
            first.names, ["LoadWithLoopInvariantAddressCondExecuted", "LoadWithLoopInvariantAddressInvalidated"]
        )
        self.assertEqual((first.score, first.message), (5.0, "invalidated"))  # the most important remark wins
        self.assertEqual(first.function, "f")  # OptView2 demangles without parameters (c++filt -p)
        self.assertEqual(findings[2].clobbered_by, "store @ pkg/a.cc:2")

    def test_reasons_only_for_loop_findings(self):
        rec = records([remark(**LOOP, line=10), remark(**LICM, line=10)], [vectorize_note("CantReorderFPOps", 11)])
        by_category = {f.category: f for f in report.collect_findings(rec)}
        self.assertEqual(len(by_category["loop-not-vectorized"].reasons), 1)
        self.assertEqual(by_category["aliasing-loop-reload"].reasons, [])

    def test_clobber_taken_from_later_remark(self):
        rec = records([remark(line=1), remark(line=1, args=({"ClobberedBy": "call", "DebugLoc": _loc("x.h", 5)},))])
        self.assertEqual(report.collect_findings(rec)[0].clobbered_by, "call @ x.h:5")

    def test_sorted_by_score_then_location(self):
        rec = records([remark(line=9), remark(**LICM, file="pkg/b.cc"), remark(**LICM, file="pkg/a.cc", line=2)])
        self.assertEqual(
            [(f.file, f.line) for f in report.collect_findings(rec)],
            [("pkg/a.cc", 2), ("pkg/b.cc", 1), ("pkg/a.cc", 9)],
        )


# --------------------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------------------
class GroupTest(unittest.TestCase):
    def test_group(self):
        findings = [
            finding(function="f", score=1.004),
            finding(function="g", score=2.0, category="loop-not-vectorized"),
            finding(file="pkg/b.cc", score=0.5),
        ]
        rows = report._group(findings, lambda f: f.file)
        self.assertEqual(rows[0], {
            "key": "pkg/a.cc",
            "locations": 2,
            "functions": 2,
            "score": 3.0,
            "categories": {"aliasing-loop-reload": 1, "loop-not-vectorized": 1},
        })  # fmt: skip
        self.assertEqual(rows[1]["key"], "pkg/b.cc")

    def test_ties_sorted_by_key(self):
        rows = report._group([finding(file="b"), finding(file="a")], lambda f: f.file)
        self.assertEqual([r["key"] for r in rows], ["a", "b"])


class StackFramesTest(unittest.TestCase):
    def test_largest_frame_per_function(self):
        notes = [
            stack_note("_Z1fv", 16),
            stack_note("_Z1fv", 64, line=3),
            stack_note("_Z1gv", 32),
            stack_note("_Z1hv", 0),
        ]
        notes.append(vectorize_note("CantReorderFPOps", 1))
        self.assertEqual(report._stack_frames(notes), [
            {"function": "f", "bytes": 64, "location": "pkg/a.cc:3"},
            {"function": "g", "bytes": 32, "location": "pkg/a.cc:1"},
        ])  # fmt: skip


class SummarizeTest(unittest.TestCase):
    def test_summarize(self):
        rec = records([remark(**LICM, file=f"pkg/{i}.cc") for i in range(3)], [stack_note("_Z1fv", 8)])
        findings = report.collect_findings(rec)
        summary = report.summarize(rec, findings, top=2, title="t")
        self.assertEqual(summary["title"], "t")
        self.assertEqual(summary["stats"], {
            "record_files": 1, "raw_remarks": 4, "missed_remarks": 3, "locations": 3,
            "files": 3, "functions": 1, "has_hotness": False,
        })  # fmt: skip
        self.assertEqual(len(summary["findings"]), 3)  # never truncated
        self.assertEqual(len(summary["files"]), 2)
        self.assertEqual(len(summary["categories"]), 1)
        self.assertEqual(summary["clobbers"], [])
        self.assertEqual(summary["stack_frames"][0]["bytes"], 8)
        json.dumps(summary)  # serializable


# --------------------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------------------
class MarkdownHelpersTest(unittest.TestCase):
    def test_cell(self):
        self.assertEqual(report._cell("a|b\nc"), "a\\|b c")
        self.assertEqual(report._cell(1.5), "1.5")
        self.assertEqual(report._cell("x" * 10, width=5), "xxxx…")

    def test_section(self):
        self.assertEqual(report._section("T", "", ["A"], []), [])
        self.assertEqual(report._section("T", "intro", ["A", "B"], [[1, "x|y"]]), [
            "## T", "", "intro", "", "| A | B |", "|---|---|", "| 1 | x\\|y |", "",
        ])  # fmt: skip

    def test_categories_cell(self):
        self.assertEqual(report._categories_cell({"categories": {"a": 2, "b": 1}}), "a: 2, b: 1")

    def test_detail(self):
        self.assertEqual(report._detail({"message": "m", "reasons": []}), "m")
        self.assertEqual(report._detail({"message": "m", "reasons": ["r1", "r2"]}), "m — why: r1; r2")


class RenderMarkdownTest(unittest.TestCase):
    def setUp(self):
        rec = records(
            [remark(**LICM, line=i) for i in range(1, 4)]
            + [remark(line=9, args=({"ClobberedBy": "call", "DebugLoc": _loc("x.h", 5)},))],
            [stack_note("_Z1fv", 128)],
        )
        self.summary = report.summarize(rec, report.collect_findings(rec), top=10, title="demo")

    def test_sections(self):
        md = report.render_markdown(self.summary, top=2)
        self.assertTrue(md.startswith("# OptView2 summary: demo\n"))
        for heading in ("## Categories", "## Top sweet spots", "## Files", "## Functions",
                        "## Most frequent clobbers", "## Largest stack frames", "## What to do"):  # fmt: skip
            self.assertIn(heading, md)
        self.assertIn("### " + triage.ALIASING_LOOP.title, md)
        self.assertIn("`call @ x.h:5`", md)
        self.assertNotIn("Full annotated report", md)

    def test_top_limits_sweet_spots(self):
        md = report.render_markdown(self.summary, top=2)
        spots = md.split("## Top sweet spots")[1].split("## Files")[0]
        self.assertIn("| 2 |", spots)
        self.assertNotIn("| 3 |", spots)

    def test_html_link(self):
        self.assertIn(
            "Full annotated report: `html/index.html`", report.render_markdown(self.summary, 2, "html/index.html")
        )

    def test_empty_sections_are_omitted(self):
        rec = records([remark(**LICM)])
        md = report.render_markdown(report.summarize(rec, report.collect_findings(rec), 5, "x"), top=5)
        self.assertNotIn("## Most frequent clobbers", md)
        self.assertNotIn("## Largest stack frames", md)


# --------------------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------------------
class RenderHtmlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.out = os.path.join(self.tmp, "html")

    def test_empty_records_write_placeholder(self):
        index = report.render_html(records(), self.tmp, self.out, jobs=1)
        self.assertEqual(index, os.path.join(self.out, "index.html"))
        with open(index, encoding="utf-8") as f:
            self.assertIn("No missed-optimization remarks", f.read())

    def test_renders_index_and_annotated_source(self):
        write(self.tmp, "pkg/a.cc", "int f();\nint g();\n")
        index = report.render_html(records([remark(**LICM, line=2)]), self.tmp, self.out, jobs=1)
        with open(index, encoding="utf-8") as f:
            self.assertIn("LoadWithLoopInvariantAddressInvalidated", f.read())
        with open(os.path.join(self.out, "pkg_a.cc.html"), encoding="utf-8") as f:
            page = f.read()
        self.assertNotIn("Unable to locate file", page)
        self.assertIn('name=\\"L2\\"', page)  # line anchors of the rendered source
        self.assertIn("msg", page)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
class ParserTest(unittest.TestCase):
    def test_defaults(self):
        args = report.build_parser().parse_args([])
        self.assertEqual((args.exclude_names, args.exclude_text), report._optview2_config_filters())
        self.assertTrue(args.exclude_names)  # OptView2 ships non-empty defaults
        self.assertEqual(args.exclude_file_regex, report.DEFAULT_EXCLUDE_FILE_REGEX)
        self.assertEqual((args.output_dir, args.summary_md, args.jobs, args.top), ("", "", 1, 25))

    def test_params_file(self):
        params = write(tempfile.mkdtemp(), "args.params", "a.opt.yaml\nb.opt.yaml\n")
        self.assertEqual(report.build_parser().parse_args([f"@{params}"]).inputs, ["a.opt.yaml", "b.opt.yaml"])


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        write(self.tmp, "pkg/a.cc", "int f();\n")
        self.yaml = write(self.tmp, "rec/a.opt.yaml", doc(**LICM))

    def _args(self, *argv) -> argparse.Namespace:
        return report.build_parser().parse_args(["--source-dir", self.tmp, self.yaml, *argv])

    def test_writes_outputs_with_relative_link(self):
        md = os.path.join(self.tmp, "out", "summary.md")
        js = os.path.join(self.tmp, "out", "summary.json")
        summary = report.run(
            self._args("-o", os.path.join(self.tmp, "out", "html"), "--summary-md", md, "--summary-json", js)
        )
        with open(md, encoding="utf-8") as f:
            self.assertIn("Full annotated report: `html/index.html`", f.read())
        with open(js, encoding="utf-8") as f:
            self.assertEqual(json.load(f), summary)
        self.assertEqual(summary["filters"]["exclude_file_regex"], report.DEFAULT_EXCLUDE_FILE_REGEX)

    def test_without_output_dir_no_html(self):
        md = os.path.join(self.tmp, "summary.md")
        report.run(self._args("--summary-md", md))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "index.html")))
        with open(md, encoding="utf-8") as f:
            self.assertNotIn("Full annotated report", f.read())

    def test_main(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(report.main(["--source-dir", self.tmp, self.yaml]), 0)


if __name__ == "__main__":
    unittest.main()
