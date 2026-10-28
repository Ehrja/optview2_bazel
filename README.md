# optview2_bazel

Bazel integration for [OptView2](https://github.com/OfekShilon/optview2): find the places in your
C/C++ code where **clang failed to optimize** (aliasing reloads, non-vectorized loops, calls that
could not be inlined, ...) — for whole Bazel packages, hermetically, cacheable, with a ranked
"sweet spot" summary on top of OptView2's annotated HTML report.

> **OptView2, not LLVM's opt-viewer.** This repository integrates Ofek Shilon's
> [OptView2](https://github.com/OfekShilon/optview2), a user-oriented fork of LLVM's
> `llvm/tools/opt-viewer` (only failed optimizations, system headers ignored, filterable, sortable
> index). It does not use the upstream LLVM `opt-viewer.py`; hence all names carry the `2`.

```
bazel run @optview2_bazel//:optview2 -- -j 16 -- --config=clang -c opt //my/pkg/...
# -> optview2_report/summary.md     ranked findings + remedies
#    optview2_report/summary.json   same, machine readable
#    optview2_report/html/index.html OptView2 annotated sources
```

## How it works

```
 cc_library / cc_binary / cc_test           (your targets, unchanged)
        │  optview2_aspect (propagates along deps / implementation_deps)
        ▼
 OptView2Compile action per source file
   = exact replay of the CppCompile command line (same toolchain, features,
     copts, defines, include paths, -O level)
   + -fsave-optimization-record -foptimization-record-file=<declared>.opt.yaml
        │  output group: optview2_yaml          provider: OptView2Info
        ▼
 tools/report.py  (OptView2's optrecord.py + opt-viewer.py, plus triage)
   ├─ filters: external/, bazel-out/, system headers, OptView2 config.yaml
   ├─ HTML: OptView2 index + annotated sources
   └─ summary.md / summary.json: categories, ranked sweet spots, per file/function,
      loop-vectorizer "why" reasons, largest stack frames
```

* The regular build is **not** modified: remark files come from separate actions that only run when
  requested (`--output_groups=optview2_yaml`, the `optview2_report` rule or the `optview2` driver).
* Everything is a declared output: works with sandboxing, disk/remote cache and remote execution.
* Requires a **clang** C++ toolchain (GCC has no compatible remark format). The aspect fails with a
  clear message otherwise (`--@optview2_bazel//optview2:compiler_check=skip|off` to change).

## Setup

`MODULE.bazel` of your project:

```starlark
bazel_dep(name = "optview2_bazel", dev_dependency = True)
local_path_override(  # or git_override / archive_override
    module_name = "optview2_bazel",
    path = "/path/to/optview2_bazel",
)

# Optional, see "Choosing the OptView2 source" below.
optview2 = use_extension("@optview2_bazel//optview2:extensions.bzl", "optview2", dev_dependency = True)
optview2.local(path = "/path/to/optview2")
```

### Choosing the OptView2 source

`@optview2` is a variation point provided by the `optview2` module extension. The module closest
to the root that sets a tag decides; without any tag the pinned default is used:

| Tag | Use case |
|---|---|
| *(none)* | Pinned upstream commit of github.com/OfekShilon/optview2 (sha256-verified download) |
| `optview2.local(path = …)` | Your own checkout, e.g. while developing OptView2 itself |
| `optview2.archive(urls, sha256, strip_prefix)` | A fork or internal mirror with patches |

All variants get the same BUILD overlay (`third_party/optview2.BUILD`); only `optrecord.py`,
`opt-viewer.py`, `optpmap.py`, `config_parser.py`, `config.yaml` and `assets/` are used.

OptView2 needs PyYAML + Pygments. optview2_bazel brings its own pip hub (`@optview2_bazel_pypi`,
downloaded through the Bazel downloader, so `--downloader_config` mirrors apply). If your project
already has both packages in a pip hub, reuse it (recommended in corporate setups):

```
# .bazelrc / user.bazelrc
common:optview2 --@optview2_bazel//tools:pyyaml=@my_pypi//pyyaml
common:optview2 --@optview2_bazel//tools:pygments=@my_pypi//pygments
```

Python ≥ 3.12 is required by OptView2 (rules_python toolchain of the root module).

## Usage

### 1. Driver: whole packages in one go (recommended for exploration)

```
bazel run @optview2_bazel//:optview2 -- [report options] -- <bazel build flags and targets>

bazel run --config=optview2 @optview2_bazel//:optview2 -- -j 16 --output optview2_report/common \
    -- --config=clang19 --config=optview2 -c opt //common/...
```

The driver runs `bazel build --aspects=…%optview2_aspect --output_groups=optview2_yaml --keep_going
<your args>`, collects the YAML files from the Build Event Protocol, and renders the report with
the execution root as source directory. Everything before the second `--` is a report option:

| Option | Default | Meaning |
|---|---|---|
| `--output DIR` | `<workspace>/optview2_report` | Output directory |
| `-j N` | 1 | Parallel YAML parsing / HTML rendering |
| `--top N` | 25 | Rows in the top-N tables |
| `--include-file-regex RE` | – | Keep only remarks in matching files, e.g. `^common/` |
| `--exclude-file-regex RE` | `^(external/\|bazel-out/\|/)` | Drop third-party, generated and system files |
| `--include-tu-regex RE` / `--exclude-tu-regex RE` | – | Select translation units by record path, e.g. `--exclude-tu-regex '/test/\|_test\.cpp'` — header remarks are attributed to the TU that inlined the header, so file filters alone do not remove test-induced findings |
| `--exclude-names RE` / `--exclude-text RE` | OptView2 `config.yaml` | Drop remarks by name / message; a given value *replaces* the default, `''` disables it |
| `--no-html` | off | Summary only (fast) |
| `--bep FILE` | – | Do not build; re-render from an existing BEP JSON file |

Always pass `-c opt` (or your release config): remarks of unoptimized builds are meaningless.

**What-if experiments**: compare reports with an extra flag, e.g. how much a strict FP model costs:
`-- --config=clang -c opt '--@optview2_bazel//optview2:extra_copts=-Wno-overriding-option,-fno-rounding-math,-ffp-exception-behavior=ignore' //pkg/...`
(note: a later `-ffp-model=precise` does *not* undo `-ffp-model=strict` in clang 19). The YAML
outputs of different flag sets share paths in `bazel-bin`, so render one experiment before
building the next.

### 2. Rule: hermetic, cacheable report (CI, regression tracking)

```starlark
load("@optview2_bazel//optview2:defs.bzl", "optview2_report")

optview2_report(
    name = "geometry_optview2",
    targets = ["//common/geometry:all_libs"],  # cc deps are analyzed transitively
    include_file_regex = "^common/geometry/",
    jobs = 4,
)
```

`bazel build --config=clang //pkg:geometry_optview2` produces `geometry_optview2.summary.md`,
`.summary.json` and `geometry_optview2_html/`. The `targets` are transitioned to
`compilation_mode = "opt"` by default (`compilation_mode = ""` keeps the current one). See
`optview2/private/report.bzl` for all attributes.

### 3. Raw aspect (bring your own tooling)

```
bazel build -c opt --config=clang //my/pkg/... \
    --aspects=@optview2_bazel//optview2:defs.bzl%optview2_aspect --output_groups=optview2_yaml
```

`*.opt.yaml` files land in `bazel-bin/<pkg>/_optview2/<target>/<src>.opt.yaml`. Upstream
`opt-viewer.py --source-dir $(bazel info execution_root) …` works on them, or use
`bazel run @optview2_bazel//tools:report -- --help`. Custom rules can consume the `OptView2Info`
provider.

### Flags

| Flag | Default | |
|---|---|---|
| `--@optview2_bazel//optview2:extra_copts=…` | `[]` | Extra compiler flags for OptView2 compiles, e.g. `-foptimization-record-passes=licm\|gvn\|loop-vectorize\|inline` to shrink YAML, or PGO: `-fprofile-instr-use=/abs/x.profdata,-fdiagnostics-show-hotness` |
| `--@optview2_bazel//optview2:compiler_check` | `error` | `skip` silently ignores non-clang targets, `off` disables the check |
| `--@optview2_bazel//optview2:include_external` | `False` | Also analyze cc targets of external repositories |
| `--@optview2_bazel//tools:pyyaml` / `:pygments` | own pip hub | Python deps for OptView2 |

Targets tagged `no-optview2` are skipped. The `optview2_report` rule additionally offers
`exclude_tu_regex`, `include_file_regex`, `exclude_file_regex`, `exclude_names`, `exclude_text`.

## Interpreting the results

See **[docs/interpreting_results.md](docs/interpreting_results.md)** — what each remark category
means, how the ranking works, typical false positives, and the fix patterns.

## Tests

```
bazel test //...            # unit + component + aspect analysis tests
cd e2e && bazel test //...  # end-to-end with hermetic LLVM 19 (toolchains_llvm): compile, report, assert
```

| Level | Target | Covers |
|---|---|---|
| Unit | `//tools/test:report_test` | every function of `report.py` on small synthetic remark YAML (filters, remark helpers, path normalization, loading/dedup, vectorizer reasons, scoring, grouping, summary, Markdown, HTML, CLI) |
| Unit | `//tools/test:triage_test` | classification, weights, knowledge-base consistency |
| Unit | `//tools/test:optview2_bazel_test` | driver: argv split, target patterns, BEP parsing (nested/cyclic sets, remote URIs), `bazel build` command line (mocked), `main` incl. `--bep` and build paths |
| Component | `//tests:report_component_test` | `report.py` CLI on real clang 15 output of `examples/` (rankings, reasons, clobbers, TU/file filters, hotness, HTML) |
| Analysis | `//tests/analysis:*` | aspect actions & flags (copts, cxxopts/conlyopts, defines, local_defines, `implementation_deps`, `extra_copts`), transitive collection, header-only/opt-out, compiler check |
| End-to-end | `e2e //:report_test` | real clang 19: slow vs. fixed variant, FP-reduction reason, cross-TU inlining, include/TU regex, no-HTML mode |

The unit tests reach 99 % branch coverage of `tools/` (only the `__main__` guards are not run).

`examples/` contains small C++ kernels with typical missed optimizations (aliasing via `const&`,
FP reduction, cross-TU call in a loop, `char*` stores clobbering members); the clang 15 records of
these files are the unit-test fixtures in `tests/testdata/clang15/`.

## Development

```
pip install pre-commit && pre-commit install   # buildifier, buildifier-lint, ruff (lint + format)
pre-commit run --all-files
```

Python style is configured in `ruff.toml` (incl. a McCabe complexity limit of 10 to keep the
report pipeline flat). `tools/report.py` is a straight pipeline of plain functions:
`find_record_files → load_records → collect_findings → summarize → render_markdown`
(+ `render_html` via OptView2).

## License

[MIT](LICENSE). OptView2 itself is fetched at build time and not part of this repository.

## Limitations

* `$(location …)` in `copts` and `--per_file_copt` are not replayed.
* Header-only code is only analyzed through the `.cc` files that instantiate it (remarks are
  reported at the header location, deduplicated across translation units).
* The OptView2 compile does not reuse the object file of the real build, i.e. analyzed sources are
  compiled a second time (once, then cached).
* LTO-time remarks (`-flto` link step) are not collected.
