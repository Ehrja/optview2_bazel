#!/usr/bin/env python3
"""`bazel run @optview2_bazel//:optview2 -- [report options] -- <bazel build args>`

Builds the given targets with the optview2 aspect (in your workspace, with your
flags - e.g. `--config=clang -c opt`), collects the produced *.opt.yaml files
from the Build Event Protocol and renders the OptView2 HTML report plus the
triaged summary into --output (default: <workspace>/optview2_report).

Everything before the first `--` is a report option (see report.py --help plus
the options below); everything after it is passed to `bazel build`.
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
import urllib.parse
import urllib.request

import report


def split_argv(argv: list[str]) -> tuple[list[str], list[str]]:
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1 :]
    return argv, []


def target_patterns(bazel_args: list[str]) -> list[str]:
    """Target patterns among bazel arguments (skips flags and flag values like `-c opt`)."""
    return [a for a in bazel_args if not a.startswith("-") and ("//" in a or a.startswith(":"))]


def _set_ids(file_sets: list[dict]) -> list[str]:
    return [file_set["id"] for file_set in file_sets]


def _read_bep(path: str, output_group: str) -> tuple[dict[str, dict], list[str]]:
    """(named file sets by id, ids of the file sets that make up `output_group`)."""
    named_sets, roots = {}, []
    with open(path, encoding="utf-8") as f:
        events = [json.loads(line) for line in f if line.strip()]
    for event in events:
        event_id = event.get("id", {})
        if "namedSet" in event_id:
            named_sets[event_id["namedSet"]["id"]] = event.get("namedSetOfFiles", {})
        for group in event.get("completed", {}).get("outputGroup", []):
            if group.get("name") == output_group:
                roots += _set_ids(group.get("fileSets", []))
    return named_sets, roots


def parse_bep(path: str, output_group: str = "optview2_yaml") -> list[str]:
    """Local paths of all files in `output_group` from a Build Event Protocol JSON file."""
    named_sets, pending = _read_bep(path, output_group)
    files, visited = set(), set()
    while pending:
        set_id = pending.pop()
        if set_id in visited:
            continue
        visited.add(set_id)
        named_set = named_sets.get(set_id, {})
        pending += _set_ids(named_set.get("fileSets", []))
        uris = [file.get("uri", "") for file in named_set.get("files", [])]
        files |= {urllib.request.url2pathname(urllib.parse.urlparse(u).path) for u in uris if u.startswith("file://")}
    return sorted(files)


def _parser() -> argparse.ArgumentParser:
    parser = report.build_parser()
    parser.description = __doc__
    parser.fromfile_prefix_chars = None  # values like `@repo//label` are not params files here
    parser.set_defaults(title="")
    parser.add_argument("--aspect", required=True, help="Aspect label (set by the BUILD target)")
    parser.add_argument("--bazel", default=os.environ.get("BAZEL", "bazel"), help="Bazel binary")
    parser.add_argument("--output", default="optview2_report", help="Output directory, relative to the workspace")
    parser.add_argument("--no-html", action="store_true", help="Only write the summary")
    parser.add_argument("--bep", default="", help="Do not build; read *.opt.yaml locations from this BEP JSON file")
    parser.add_argument("--no-keep-going", action="store_true", help="Do not pass --keep_going to bazel build")
    return parser


def build(args: argparse.Namespace, bazel_args: list[str], workspace: str, bep: str) -> None:
    """`bazel build` the targets with the aspect, writing the Build Event Protocol to `bep`."""
    cmd = [
        args.bazel,
        "build",
        f"--aspects={args.aspect}",
        "--output_groups=optview2_yaml",
        f"--build_event_json_file={bep}",
        # Make sure the yaml files end up on disk with remote caching/execution.
        "--remote_download_regex=.*\\.opt\\.yaml$",
    ]
    cmd += [] if args.no_keep_going else ["--keep_going"]
    cmd += bazel_args
    if not any(a == "-c" or a.startswith("--compilation_mode") for a in bazel_args):
        print("optview2: note: no -c/--compilation_mode given; remarks are only meaningful for -c opt", file=sys.stderr)
    print("optview2: " + shlex.join(cmd), file=sys.stderr)
    if subprocess.call(cmd, cwd=workspace) != 0:
        print("optview2: warning: bazel build failed; reporting partial results", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    own, bazel_args = split_argv(sys.argv[1:] if argv is None else argv)
    parser = _parser()
    args = parser.parse_args(own)
    if not args.bep and not bazel_args:
        parser.error("missing `-- <bazel flags and targets>`")

    workspace = os.environ.get("BUILD_WORKSPACE_DIRECTORY", os.getcwd())
    out = os.path.join(workspace, args.output)
    os.makedirs(out, exist_ok=True)

    bep = os.path.join(workspace, args.bep) if args.bep else os.path.join(out, "build_events.json")
    if not args.bep:
        build(args, bazel_args, workspace, bep)
    records = parse_bep(bep)
    if not records:
        print("optview2: no *.opt.yaml files were produced (no cc sources in scope?)", file=sys.stderr)
        return 1

    args.inputs += records
    args.output_dir = args.output_dir or ("" if args.no_html else os.path.join(out, "html"))
    args.summary_md = args.summary_md or os.path.join(out, "summary.md")
    args.summary_json = args.summary_json or os.path.join(out, "summary.json")
    args.title = args.title or " ".join(target_patterns(bazel_args)) or args.bep
    if args.source_dir == ".":
        args.source_dir = subprocess.check_output([args.bazel, "info", "execution_root"], cwd=workspace, text=True)
        args.source_dir = args.source_dir.strip()

    # Relative DebugLoc paths are relative to the execution root.
    os.chdir(args.source_dir)
    stats = report.run(args)["stats"]
    print(f"optview2: {stats['record_files']} record files, {stats['locations']} missed-optimization locations")
    print(f"optview2: summary : {args.summary_md}")
    if args.output_dir:
        print(f"optview2: html    : {os.path.join(args.output_dir, 'index.html')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
