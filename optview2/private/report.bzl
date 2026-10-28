"""`optview2_report`: hermetic, cacheable OptView2 report for a set of cc targets."""

load(":aspect.bzl", "OptView2Info", "optview2_aspect")

def _opt_transition_impl(settings, attr):
    if not attr.compilation_mode:
        return {"//command_line_option:compilation_mode": settings["//command_line_option:compilation_mode"]}
    return {"//command_line_option:compilation_mode": attr.compilation_mode}

_opt_transition = transition(
    implementation = _opt_transition_impl,
    inputs = ["//command_line_option:compilation_mode"],
    outputs = ["//command_line_option:compilation_mode"],
)

def _optview2_report_impl(ctx):
    infos = [t[OptView2Info] for t in ctx.attr.targets if OptView2Info in t]
    yamls = depset(transitive = [i.yamls for i in infos])
    sources = depset(transitive = [i.sources for i in infos])

    summary_md = ctx.actions.declare_file(ctx.label.name + ".summary.md")
    summary_json = ctx.actions.declare_file(ctx.label.name + ".summary.json")
    html = [ctx.actions.declare_directory(ctx.label.name + "_html")] if ctx.attr.html else []

    args = ctx.actions.args()
    args.add("--title=" + (ctx.attr.title or "//%s:%s" % (ctx.label.package, ctx.label.name)))
    args.add("--summary-md", summary_md)
    args.add("--summary-json", summary_json)
    args.add_all(html, before_each = "--output-dir", expand_directories = False)
    args.add("--jobs", str(ctx.attr.jobs))
    args.add("--top", str(ctx.attr.top_n))
    args.add("--include-tu-regex=" + ctx.attr.include_tu_regex)
    args.add("--exclude-tu-regex=" + ctx.attr.exclude_tu_regex)
    args.add("--include-file-regex=" + ctx.attr.include_file_regex)
    args.add("--exclude-file-regex=" + ctx.attr.exclude_file_regex)

    # Empty means "OptView2 config.yaml defaults" for these two.
    if ctx.attr.exclude_names:
        args.add("--exclude-names=" + ctx.attr.exclude_names)
    if ctx.attr.exclude_text:
        args.add("--exclude-text=" + ctx.attr.exclude_text)

    yaml_args = ctx.actions.args()
    yaml_args.use_param_file("@%s", use_always = True)
    yaml_args.set_param_file_format("multiline")
    yaml_args.add_all(yamls)

    ctx.actions.run(
        executable = ctx.executable._report_tool,
        arguments = [args, yaml_args],
        inputs = depset(transitive = [yamls, sources]),
        outputs = [summary_md, summary_json] + html,
        mnemonic = "OptView2Report",
        progress_message = "OptView2: generating report %{label}",
    )

    return [
        DefaultInfo(files = depset([summary_md, summary_json] + html)),
        OutputGroupInfo(
            optview2_yaml = yamls,
            summary = depset([summary_md, summary_json]),
            html = depset(html),
        ),
    ]

optview2_report = rule(
    implementation = _optview2_report_impl,
    doc = """Builds an OptView2 HTML report plus a triaged summary (Markdown + JSON) for
the given cc targets and all their (main-repository) cc dependencies.

Outputs:
  <name>.summary.md    human-readable triage of missed optimizations ("sweet spots")
  <name>.summary.json  machine-readable version of the same data
  <name>_html/         OptView2 HTML report (index.html), if html = True
""",
    attrs = {
        "compilation_mode": attr.string(
            default = "opt",
            values = ["opt", "dbg", "fastbuild", ""],
            doc = "Compilation mode the targets are analyzed in. Remarks are only meaningful " +
                  "for optimized builds. Empty string keeps the current configuration.",
        ),
        "exclude_file_regex": attr.string(
            default = "^(external/|bazel-out/|/)",
            doc = "Drop remarks whose source file matches this regex (third-party, generated " +
                  "and system headers by default).",
        ),
        "exclude_names": attr.string(
            doc = "Drop remarks whose name matches. Empty: OptView2's config.yaml default " +
                  "(drops e.g. spill and NeverInline noise).",
        ),
        "exclude_text": attr.string(
            doc = "Drop remarks whose message matches. Empty: OptView2's config.yaml default " +
                  "(drops e.g. std:: inlining noise).",
        ),
        "exclude_tu_regex": attr.string(
            doc = "Ignore translation units whose record path matches (e.g. '_test\\.cpp' so that " +
                  "header remarks caused by test code are not reported).",
        ),
        "html": attr.bool(default = True, doc = "Generate the OptView2 HTML report."),
        "include_tu_regex": attr.string(
            doc = "Only use translation units whose record path matches (e.g. '/common/geometry/').",
        ),
        "include_file_regex": attr.string(
            doc = "Keep only remarks whose source file matches this regex (e.g. '^common/').",
        ),
        "jobs": attr.int(default = 1, doc = "Parallel YAML parsing / rendering processes."),
        "targets": attr.label_list(
            aspects = [optview2_aspect],
            cfg = _opt_transition,
            mandatory = True,
            doc = "cc targets to analyze (cc deps are analyzed transitively).",
        ),
        "title": attr.string(doc = "Report title. Defaults to the target label."),
        "top_n": attr.int(default = 25, doc = "Number of entries in the top-N tables of the summary."),
        "_report_tool": attr.label(
            default = Label("//tools:report"),
            executable = True,
            cfg = "exec",
        ),
    },
)
