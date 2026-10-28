#!/usr/bin/env python3
"""Generate an OptView2 HTML report plus a ranked "sweet spot" summary.

Pipeline (each step is a plain function):

    find_record_files -> load_records -> collect_findings -> summarize -> render_markdown
                                      \\-> render_html (OptView2's opt-viewer.py)

Compared to OptView2's own opt-viewer.py this
  * filters by translation unit and by source file (Bazel puts third-party code under
    external/ and generated code under bazel-out/),
  * keeps the loop-vectorizer Analysis remarks to explain *why* a loop was not vectorized,
  * ranks findings (see triage.py and docs/interpreting_results.md).

OptView2 must be importable (Bazel: @optview2 py_library; standalone: PYTHONPATH=<optview2>).
"""

import argparse
import collections
import contextlib
import dataclasses
import functools
import importlib.util
import io
import json
import logging
import multiprocessing
import os
import re
import sys

import optrecord
import yaml

import triage

DEFAULT_EXCLUDE_FILE_REGEX = r"^(external/|bazel-out/|/)"

_EMPTY_INDEX = """<html><head><meta charset="utf-8"/><title>OptView2 Index</title></head>
<body><h3>No missed-optimization remarks matched the filters.</h3></body></html>
"""


def _import_opt_viewer():
    """Import OptView2's opt-viewer.py, whose file name is not a valid module name."""
    path = os.path.join(os.path.dirname(optrecord.__file__), "opt-viewer.py")
    spec = importlib.util.spec_from_file_location("opt_viewer", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["opt_viewer"] = module  # lets multiprocessing pickle functions defined in it
    spec.loader.exec_module(module)
    return module


opt_viewer = _import_opt_viewer()


def _optview2_config_filters() -> tuple[str, str]:
    """(exclude_names, exclude_text) from OptView2's config.yaml - used as defaults."""
    with open(os.path.join(os.path.dirname(optrecord.__file__), "config.yaml"), encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return config.get("exclude_names", ""), config.get("exclude_text", "")


# --------------------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------------------
def _matches(pattern: str, text: str) -> bool:
    return bool(pattern) and re.search(pattern, text) is not None


def _included(pattern: str, text: str) -> bool:
    return not pattern or re.search(pattern, text) is not None


@dataclasses.dataclass(frozen=True)
class Filters:
    include_tu_regex: str = ""
    exclude_tu_regex: str = ""
    include_file_regex: str = ""
    exclude_file_regex: str = DEFAULT_EXCLUDE_FILE_REGEX
    exclude_names: str = ""
    exclude_text: str = ""

    def keep_tu(self, record_path: str) -> bool:
        return _included(self.include_tu_regex, record_path) and not _matches(self.exclude_tu_regex, record_path)

    def keep_file(self, file: str) -> bool:
        return _included(self.include_file_regex, file) and not _matches(self.exclude_file_regex, file)

    def keep(self, remark) -> bool:
        return (
            self.keep_file(remark.File)
            and not _matches(self.exclude_names, remark.Name)
            and not _matches(self.exclude_text, remark.message)
        )


# --------------------------------------------------------------------------------------
# Remark helpers (OptView2 stores arguments as tuples of (key, value) pairs)
# --------------------------------------------------------------------------------------
_SYMBOL_KEYS = ("Callee", "Caller", "DirectCallee")


def _args(remark) -> list[dict]:
    return [{k: dict(v) if k == "DebugLoc" else v for k, v in arg} for arg in remark.Args]


def _arg(remark, key: str) -> dict:
    """The first argument containing `key` (together with its DebugLoc), or {}."""
    return next((arg for arg in _args(remark) if key in arg), {})


def message(remark) -> str:
    """Plain-text remark message; locations in other files are spelled out."""
    parts = []
    for arg in _args(remark):
        loc = arg.pop("DebugLoc", {})
        ((key, value),) = arg.items()
        text = remark.demangle(value) if key in _SYMBOL_KEYS else str(value)
        if loc and key != "Caller":
            where = f"{loc['Line']}:{loc['Column']}"
            text += f" (line {where})" if loc["File"] == remark.File else f" ({loc['File']}:{where})"
        parts.append(text)
    return "".join(parts)


def _clobbered_by(remark) -> str:
    arg = _arg(remark, "ClobberedBy")
    if "DebugLoc" not in arg:
        return ""
    return f"{arg['ClobberedBy']} @ {arg['DebugLoc']['File']}:{arg['DebugLoc']['Line']}"


def _normalize(path: str) -> str:
    # clang spells headers found via `-iquote .` as "./pkg/x.h", sources as "pkg/x.h".
    return os.path.normpath(path) if "./" in path else path


def _normalize_loc(loc: tuple) -> tuple:
    return tuple((k, _normalize(v) if k == "File" else v) for k, v in loc)


def _normalize_paths(remark) -> None:
    remark.DebugLoc["File"] = _normalize(remark.DebugLoc["File"])
    remark.Args = tuple(tuple((k, _normalize_loc(v) if k == "DebugLoc" else v) for k, v in arg) for arg in remark.Args)


# --------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------
def find_record_files(paths: list[str], filters: Filters) -> list[str]:
    """*.opt.yaml files in `paths`. For files produced by optview2_aspect the path is
    `<bin>/<pkg>/_optview2/<target>/<source>.opt.yaml`, which the TU filters match."""
    files = []
    for path in paths:
        if os.path.isdir(path):
            for root, _, names in os.walk(path):
                files += [os.path.join(root, n) for n in names if ".opt.yaml" in n]
        else:
            files.append(path)
    return sorted({f for f in files if filters.keep_tu(f)})


def _is_note(remark) -> bool:
    """Analysis remarks kept as context: vectorizer reasons and stack frame sizes."""
    return isinstance(remark, optrecord.Analysis) and (remark.Pass == "loop-vectorize" or remark.Name == "StackSize")


def load_file(path: str, filters: Filters) -> tuple[list, list, int]:
    """Returns (missed remarks, note remarks, number of raw remarks)."""
    missed, notes, raw = [], [], 0
    with open(path, encoding="utf-8") as f:
        for remark in yaml.load_all(f, Loader=optrecord.Loader):
            raw += 1
            is_missed = isinstance(remark, optrecord.Missed)
            if not (is_missed or _is_note(remark)) or not hasattr(remark, "DebugLoc"):
                continue
            remark.canonicalize()
            _normalize_paths(remark)
            if is_missed and filters.keep(remark):
                missed.append(remark)
            elif not is_missed and filters.keep_file(remark.File):
                notes.append(remark)
    return missed, notes, raw


@dataclasses.dataclass
class Records:
    missed: dict  # remark.key -> optrecord.Remark, deduplicated across translation units
    notes: list  # optrecord.Analysis remarks, see _is_note
    files: int
    raw: int
    max_hotness: int


def load_records(files: list[str], filters: Filters, jobs: int) -> Records:
    load = functools.partial(load_file, filters=filters)
    if jobs > 1 and len(files) > 1:
        with multiprocessing.Pool(jobs) as pool:
            results = pool.map(load, files, chunksize=1)
    else:
        results = [load(f) for f in files]

    records = Records(missed={}, notes=[], files=len(files), raw=0, max_hotness=0)
    for missed, notes, raw in results:
        for remark in missed:
            records.missed.setdefault(remark.key, remark)
        records.notes += notes
        records.raw += raw
    records.max_hotness = max((r.Hotness for r in records.missed.values()), default=0)
    for remark in records.missed.values():
        remark.max_hotness = records.max_hotness  # used by OptView2's HTML
    return records


# --------------------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------------------
@dataclasses.dataclass
class Finding:
    """All missed-optimization remarks of one category on one source line."""

    file: str
    line: int
    category: str
    function: str
    message: str
    score: float
    names: list[str]
    reasons: list[str]  # why a loop was not vectorized
    clobbered_by: str  # store/call that forced a reload (gvn), "" if not applicable
    remarks: int = 1


def _vectorize_reasons(missed, notes) -> dict[tuple[str, int], list[str]]:
    """Map (file, loop line) to the loop vectorizer's Analysis remarks. A reason belongs to the
    closest 'loop not vectorized' remark above it in the same function."""
    loop_lines = collections.defaultdict(list)
    for remark in missed:
        if remark.Pass == "loop-vectorize":
            loop_lines[(remark.File, remark.Function)].append(remark.Line)

    reasons = collections.defaultdict(list)
    for note in notes:
        if note.Pass != "loop-vectorize" or note.Name in triage.IGNORED_VECTORIZE_REASONS:
            continue
        candidates = [line for line in loop_lines[(note.File, note.Function)] if line <= note.Line]
        if not candidates:
            continue
        explanation = triage.VECTORIZE_REASONS.get(note.Name, message(note))
        text = f"{note.Name}: {explanation} (line {note.Line}:{note.Column})"
        key = (note.File, max(candidates))
        if text not in reasons[key]:
            reasons[key].append(text)
    return reasons


def _score(remark, max_hotness: int) -> float:
    weight = triage.weight(remark.Pass, remark.Name, callee=_arg(remark, "Callee").get("Callee", ""))
    if not max_hotness:
        return weight
    return weight * max(remark.Hotness / max_hotness, 0.01)


def collect_findings(records: Records) -> list[Finding]:
    """Group remarks by (file, line, category), best score first."""
    reasons = _vectorize_reasons(records.missed.values(), records.notes)
    findings: dict[tuple, Finding] = {}
    for remark in records.missed.values():
        category = triage.classify(remark.Pass, remark.Name)
        score = _score(remark, records.max_hotness)
        finding = findings.get((remark.File, remark.Line, category.key))
        if finding:
            finding.remarks += 1
            if score > finding.score:  # show the message of the most important remark
                finding.score, finding.message = score, message(remark)
            finding.clobbered_by = finding.clobbered_by or _clobbered_by(remark)
            if remark.Name not in finding.names:
                finding.names.append(remark.Name)
            continue
        findings[(remark.File, remark.Line, category.key)] = Finding(
            file=remark.File,
            line=remark.Line,
            category=category.key,
            function=remark.demangled_func_name,
            message=message(remark),
            score=score,
            names=[remark.Name],
            reasons=reasons.get((remark.File, remark.Line), []) if category is triage.LOOP_NOT_VECTORIZED else [],
            clobbered_by=_clobbered_by(remark),
        )
    return sorted(findings.values(), key=lambda f: (-f.score, -f.remarks, f.file, f.line))


# --------------------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------------------
def _group(findings: list[Finding], key) -> list[dict]:
    groups = collections.defaultdict(list)
    for finding in findings:
        groups[key(finding)].append(finding)
    rows = [
        {
            "key": k,
            "locations": len(group),
            "functions": len({f.function for f in group}),
            "score": round(sum(f.score for f in group), 2),
            "categories": dict(collections.Counter(f.category for f in group).most_common()),
        }
        for k, group in groups.items()
    ]
    return sorted(rows, key=lambda r: (-r["score"], r["key"]))


def _stack_frames(notes: list) -> list[dict]:
    """Largest stack frame per function (from 'prologepilog StackSize' remarks)."""
    frames = {}
    for note in notes:
        if note.Name == "StackSize":
            size = int(_arg(note, "NumStackBytes").get("NumStackBytes", 0))
            if size > frames.get(note.Function, (0, ""))[0]:
                frames[note.Function] = (size, f"{note.File}:{note.Line}")
    rows = [{"function": optrecord.Remark.demangle(fn), "bytes": b, "location": loc} for fn, (b, loc) in frames.items()]
    return sorted(rows, key=lambda r: -r["bytes"])


def summarize(records: Records, findings: list[Finding], top: int, title: str) -> dict:
    return {
        "title": title,
        "stats": {
            "record_files": records.files,
            "raw_remarks": records.raw,
            "missed_remarks": len(records.missed),
            "locations": len(findings),
            "files": len({f.file for f in findings}),
            "functions": len({f.function for f in findings}),
            "has_hotness": bool(records.max_hotness),
        },
        "categories": _group(findings, lambda f: f.category),
        "files": _group(findings, lambda f: f.file)[:top],
        "functions": _group(findings, lambda f: f.function)[:top],
        "clobbers": _group([f for f in findings if f.clobbered_by], lambda f: f.clobbered_by)[:top],
        "stack_frames": _stack_frames(records.notes)[:top],
        "findings": [dataclasses.asdict(f) for f in findings],
    }


# --------------------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------------------
def _cell(value, width: int = 160) -> str:
    text = str(value).replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


def _section(title: str, intro: str, header: list[str], rows: list[list]) -> list[str]:
    if not rows:
        return []
    lines = [f"## {title}", ""] + ([intro, ""] if intro else [])
    lines += ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return lines + [""]


def _categories_cell(row: dict) -> str:
    return ", ".join(f"{k}: {v}" for k, v in row["categories"].items())


def render_markdown(summary: dict, top: int, html_link: str = "") -> str:
    stats = summary["stats"]
    lines = [
        f"# OptView2 summary: {summary['title']}",
        "",
        f"{stats['record_files']} optimization-record files, {stats['raw_remarks']} raw remarks, "
        f"**{stats['locations']} missed-optimization locations** in {stats['files']} files / "
        f"{stats['functions']} functions. Hotness (PGO): "
        f"{'yes' if stats['has_hotness'] else 'no - ranking uses static weights only'}.",
        "",
    ]
    if html_link:
        lines += [f"Full annotated report: `{html_link}`", ""]
    lines += [
        "Scores rank *candidates*; always confirm with a profiler/benchmark. "
        "See docs/interpreting_results.md of optview2_bazel.",
        "",
    ]

    categories = [triage.CATEGORIES[r["key"]] for r in summary["categories"]]
    lines += _section(
        "Categories",
        "",
        ["Category", "Weight", "Locations", "Score"],
        [
            [c.title, c.weight, r["locations"], r["score"]]
            for c, r in zip(categories, summary["categories"], strict=True)
        ],
    )
    lines += _section(
        "Top sweet spots",
        "",
        ["#", "Score", "Location", "Function", "Category", "Detail"],
        [
            [i, round(f["score"], 2), f"`{f['file']}:{f['line']}`", f["function"], f["category"], _detail(f)]
            for i, f in enumerate(summary["findings"][:top], 1)
        ],
    )
    lines += _section(
        "Files",
        "",
        ["File", "Locations", "Score", "Categories"],
        [[f"`{r['key']}`", r["locations"], r["score"], _categories_cell(r)] for r in summary["files"]],
    )
    lines += _section(
        "Functions",
        "",
        ["Function", "Locations", "Score", "Categories"],
        [[r["key"], r["locations"], r["score"], _categories_cell(r)] for r in summary["functions"]],
    )
    lines += _section(
        "Most frequent clobbers (root causes of redundant loads)",
        "Stores/calls that most often force the compiler to reload memory (gvn LoadClobbered). "
        "A single opaque call or `char*` store here can pessimize many functions.",
        ["Clobbered by", "Locations", "Functions"],
        [[f"`{r['key']}`", r["locations"], r["functions"]] for r in summary["clobbers"]],
    )
    lines += _section(
        "Largest stack frames",
        "",
        ["Function", "Bytes", "Location"],
        [[r["function"], r["bytes"], f"`{r['location']}`"] for r in summary["stack_frames"]],
    )

    lines += ["## What to do", ""]
    for category in categories:
        lines += [f"### {category.title}", "", category.explanation, ""]
        lines += [f"- {remedy}" for remedy in category.remedies] + [""]
    return "\n".join(lines)


def _detail(finding: dict) -> str:
    if not finding["reasons"]:
        return finding["message"]
    return finding["message"] + " — why: " + "; ".join(finding["reasons"])


# --------------------------------------------------------------------------------------
# HTML (OptView2)
# --------------------------------------------------------------------------------------
def render_html(records: Records, source_dir: str, output_dir: str, jobs: int) -> str:
    os.makedirs(output_dir, exist_ok=True)
    index = os.path.join(output_dir, "index.html")
    if not records.missed:
        with open(index, "w", encoding="utf-8") as f:
            f.write(_EMPTY_INDEX)
        return index

    file_remarks = collections.defaultdict(lambda: collections.defaultdict(list))
    for remark in records.missed.values():
        file_remarks[remark.File][remark.Line].append(remark)
    with contextlib.redirect_stdout(io.StringIO()):  # OptView2 prints progress counters
        opt_viewer.generate_report(
            all_remarks=records.missed,
            file_remarks={file: dict(lines) for file, lines in file_remarks.items()},
            source_dir=os.path.abspath(source_dir),
            output_dir=output_dir,
            should_display_hotness=bool(records.max_hotness),
            num_jobs=max(1, jobs),
        )
    return index


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    exclude_names, exclude_text = _optview2_config_filters()
    p = argparse.ArgumentParser(
        description=__doc__, fromfile_prefix_chars="@", formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("inputs", nargs="*", help="*.opt.yaml files or directories containing them")
    p.add_argument("--output-dir", "-o", default="", help="Write the OptView2 HTML report to this directory")
    p.add_argument("--summary-md", default="", help="Write the Markdown summary to this file")
    p.add_argument("--summary-json", default="", help="Write the JSON summary to this file")
    p.add_argument("--source-dir", "-s", default=".", help="Directory the compiler ran in (Bazel: execution root)")
    p.add_argument("--title", default="optimization remarks")
    p.add_argument("--include-tu-regex", default="", help="Only read record files whose path matches")
    p.add_argument(
        "--exclude-tu-regex",
        default="",
        help="Skip record files whose path matches, e.g. '/test/|_test\\.cpp' (header remarks caused by tests)",
    )
    p.add_argument("--include-file-regex", default="", help="Only keep remarks in source files matching")
    p.add_argument("--exclude-file-regex", default=DEFAULT_EXCLUDE_FILE_REGEX, help="Drop remarks in files matching")
    p.add_argument(
        "--exclude-names", default=exclude_names, help="Drop remark names matching (default: OptView2 config.yaml)"
    )
    p.add_argument(
        "--exclude-text", default=exclude_text, help="Drop remark messages matching (default: OptView2 config.yaml)"
    )
    p.add_argument("--jobs", "-j", type=int, default=1)
    p.add_argument("--top", type=int, default=25, help="Rows of the top-N tables")
    return p


def run(args: argparse.Namespace) -> dict:
    logging.getLogger().setLevel(logging.WARNING)  # OptView2 sets INFO on import
    filters = Filters(**{field.name: getattr(args, field.name) for field in dataclasses.fields(Filters)})
    records = load_records(find_record_files(args.inputs, filters), filters, args.jobs)
    findings = collect_findings(records)
    summary = summarize(records, findings, args.top, args.title)
    summary["filters"] = dataclasses.asdict(filters)

    index = render_html(records, args.source_dir, args.output_dir, args.jobs) if args.output_dir else ""
    if args.summary_json:
        with open(args.summary_json, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=1)
    if args.summary_md:
        link = os.path.relpath(index, os.path.dirname(os.path.abspath(args.summary_md))) if index else ""
        with open(args.summary_md, "w", encoding="utf-8") as f:
            f.write(render_markdown(summary, args.top, link))
    return summary


def main(argv: list[str] | None = None) -> int:
    run(build_parser().parse_args(argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
