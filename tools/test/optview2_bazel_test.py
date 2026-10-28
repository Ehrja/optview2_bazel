"""Unit tests for tools/optview2_bazel.py (the `bazel run @optview2_bazel//:optview2` driver)."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import optview2_bazel

_ASPECT = "@@optview2_bazel+//optview2:defs.bzl%optview2_aspect"

_REMARK = """--- !Missed
Pass: licm
Name: LoadWithLoopInvariantAddressInvalidated
DebugLoc: { File: 'pkg/a.cc', Line: 1, Column: 1 }
Function: _Z1fv
Args:
  - String: 'failed to move load'
...
"""


def named_set(set_id, uris=(), children=()):
    return {
        "id": {"namedSet": {"id": set_id}},
        "namedSetOfFiles": {
            "files": [{"name": os.path.basename(u), "uri": u} for u in uris],
            "fileSets": [{"id": c} for c in children],
        },
    }


def completed(group, *set_ids, aspect=_ASPECT):
    return {
        "id": {"targetCompleted": {"label": "//pkg:lib", "aspect": aspect}},
        "completed": {"success": True, "outputGroup": [{"name": group, "fileSets": [{"id": i} for i in set_ids]}]},
    }


def write_bep(directory, events) -> str:
    path = os.path.join(directory, "bep.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(e) for e in events) + "\n\n")
    return path


class SplitArgvTest(unittest.TestCase):
    def test_split(self):
        self.assertEqual(
            optview2_bazel.split_argv(["-j", "4", "--", "-c", "opt", "//a"]), (["-j", "4"], ["-c", "opt", "//a"])
        )

    def test_without_separator(self):
        self.assertEqual(optview2_bazel.split_argv(["-j", "4"]), (["-j", "4"], []))

    def test_only_first_separator_splits(self):
        self.assertEqual(optview2_bazel.split_argv(["--", "//a", "--", "x"]), ([], ["//a", "--", "x"]))


class TargetPatternsTest(unittest.TestCase):
    def test_target_patterns(self):
        argv = ["-c", "opt", "--config", "clang", "//pkg/...", "@repo//x:y", ":local", "--@flag//a:b=1", "-//pkg/skip"]
        self.assertEqual(optview2_bazel.target_patterns(argv), ["//pkg/...", "@repo//x:y", ":local"])


class BepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_set_ids(self):
        self.assertEqual(optview2_bazel._set_ids([{"id": "1"}, {"id": "7"}]), ["1", "7"])
        self.assertEqual(optview2_bazel._set_ids([]), [])

    def test_read_bep(self):
        path = write_bep(
            self.tmp, [named_set("1", ["file:///a"]), completed("optview2_yaml", "1"), completed("default", "2")]
        )
        named_sets, roots = optview2_bazel._read_bep(path, "optview2_yaml")
        self.assertEqual(sorted(named_sets), ["1"])
        self.assertEqual(roots, ["1"])

    def test_parse_bep_follows_nested_sets(self):
        path = write_bep(
            self.tmp,
            [
                named_set("1", ["file:///x/a.opt.yaml"], children=["2", "missing"]),
                named_set("2", ["file:///x/b%20c.opt.yaml"], children=["1"]),  # cycle
                named_set("3", ["file:///x/lib.a"]),
                completed("optview2_yaml", "1"),
                completed("default", "3"),
            ],
        )
        self.assertEqual(optview2_bazel.parse_bep(path), ["/x/a.opt.yaml", "/x/b c.opt.yaml"])

    def test_parse_bep_ignores_remote_uris(self):
        path = write_bep(
            self.tmp,
            [
                named_set("1", ["bytestream://cache/blobs/abc/1", "file:///x/a.opt.yaml"]),
                completed("optview2_yaml", "1"),
            ],
        )
        self.assertEqual(optview2_bazel.parse_bep(path), ["/x/a.opt.yaml"])

    def test_parse_bep_other_output_group(self):
        path = write_bep(self.tmp, [named_set("1", ["file:///x/lib.a"]), completed("default", "1")])
        self.assertEqual(optview2_bazel.parse_bep(path), [])
        self.assertEqual(optview2_bazel.parse_bep(path, output_group="default"), ["/x/lib.a"])


class ParserTest(unittest.TestCase):
    def test_requires_aspect(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            optview2_bazel._parser().parse_args([])

    def test_values_starting_with_at_are_not_params_files(self):
        args = optview2_bazel._parser().parse_args(["--aspect", _ASPECT, "--title", "@repo//pkg"])
        self.assertEqual((args.aspect, args.title), (_ASPECT, "@repo//pkg"))

    def test_defaults(self):
        args = optview2_bazel._parser().parse_args(["--aspect", _ASPECT])
        self.assertEqual((args.title, args.output, args.bep, args.no_html), ("", "optview2_report", "", False))


class BuildTest(unittest.TestCase):
    def _build(self, bazel_args, *own, returncode=0):
        args = optview2_bazel._parser().parse_args(["--aspect", _ASPECT, "--bazel", "mybazel", *own])
        stderr = io.StringIO()
        with mock.patch("subprocess.call", return_value=returncode) as call, contextlib.redirect_stderr(stderr):
            optview2_bazel.build(args, bazel_args, "/ws", "/out/bep.json")
        call.assert_called_once()
        self.assertEqual(call.call_args.kwargs, {"cwd": "/ws"})
        return call.call_args.args[0], stderr.getvalue()

    def test_command(self):
        cmd, stderr = self._build(["-c", "opt", "//pkg/..."])
        self.assertEqual(cmd[:2], ["mybazel", "build"])
        self.assertIn(f"--aspects={_ASPECT}", cmd)
        self.assertIn("--output_groups=optview2_yaml", cmd)
        self.assertIn("--build_event_json_file=/out/bep.json", cmd)
        self.assertIn("--remote_download_regex=.*\\.opt\\.yaml$", cmd)
        self.assertIn("--keep_going", cmd)
        self.assertEqual(cmd[-3:], ["-c", "opt", "//pkg/..."])  # user flags win
        self.assertNotIn("note:", stderr)

    def test_no_keep_going(self):
        cmd, _ = self._build(["-c", "opt", "//a"], "--no-keep-going")
        self.assertNotIn("--keep_going", cmd)

    def test_notes_missing_compilation_mode(self):
        _, stderr = self._build(["//a"])
        self.assertIn("no -c/--compilation_mode", stderr)
        _, stderr = self._build(["--compilation_mode=opt", "//a"])
        self.assertNotIn("no -c/--compilation_mode", stderr)

    def test_failed_build_is_reported_not_raised(self):
        _, stderr = self._build(["-c", "opt", "//a"], returncode=1)
        self.assertIn("bazel build failed; reporting partial results", stderr)


class MainTest(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.execroot = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.execroot, "pkg"))
        with open(os.path.join(self.execroot, "pkg", "a.cc"), "w", encoding="utf-8") as f:
            f.write("int f();\n")
        self.record = os.path.join(self.execroot, "a.opt.yaml")
        with open(self.record, "w", encoding="utf-8") as f:
            f.write(_REMARK)
        self.addCleanup(os.chdir, os.getcwd())  # main() changes into the execution root
        patcher = mock.patch.dict(os.environ, {"BUILD_WORKSPACE_DIRECTORY": self.workspace})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _main(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = optview2_bazel.main(["--aspect", _ASPECT, *argv])
        return rc, stdout.getvalue(), stderr.getvalue()

    def _bep(self, uris):
        return write_bep(self.workspace, [named_set("1", uris), completed("optview2_yaml", "1")])

    def test_missing_bazel_args(self):
        with self.assertRaises(SystemExit):
            self._main()

    def test_empty_bep(self):
        self._bep([])
        rc, _, stderr = self._main("--bep", "bep.json")
        self.assertEqual(rc, 1)
        self.assertIn("no *.opt.yaml files were produced", stderr)

    def test_report_from_bep(self):
        self._bep(["file://" + self.record])
        rc, stdout, _ = self._main("--bep", "bep.json", "--source-dir", self.execroot, "--output", "rep")
        self.assertEqual(rc, 0)
        self.assertIn("1 record files, 1 missed-optimization locations", stdout)
        out = os.path.join(self.workspace, "rep")
        self.assertTrue(os.path.exists(os.path.join(out, "html", "index.html")))
        with open(os.path.join(out, "summary.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["title"], "bep.json")
        self.assertTrue(os.path.exists(os.path.join(out, "summary.md")))

    def test_no_html(self):
        self._bep(["file://" + self.record])
        rc, stdout, _ = self._main("--bep", "bep.json", "--source-dir", self.execroot, "--no-html")
        self.assertEqual(rc, 0)
        self.assertNotIn("html", stdout)
        self.assertFalse(os.path.exists(os.path.join(self.workspace, "optview2_report", "html")))

    def test_builds_and_uses_execution_root(self):
        def fake_build(args, bazel_args, workspace, bep):
            self.assertEqual((bazel_args, workspace), (["-c", "opt", "//pkg:lib"], self.workspace))
            write_bep(
                os.path.dirname(bep), [named_set("1", ["file://" + self.record]), completed("optview2_yaml", "1")]
            )
            os.rename(os.path.join(os.path.dirname(bep), "bep.json"), bep)

        with (
            mock.patch.object(optview2_bazel, "build", side_effect=fake_build),
            mock.patch("subprocess.check_output", return_value=self.execroot + "\n") as info,
        ):
            rc, _, _ = self._main("--", "-c", "opt", "//pkg:lib")
        self.assertEqual(rc, 0)
        self.assertEqual(info.call_args.args[0], ["bazel", "info", "execution_root"])
        with open(os.path.join(self.workspace, "optview2_report", "summary.json"), encoding="utf-8") as f:
            summary = json.load(f)
        self.assertEqual(summary["title"], "//pkg:lib")
        self.assertEqual(summary["findings"][0]["file"], "pkg/a.cc")


if __name__ == "__main__":
    unittest.main()
