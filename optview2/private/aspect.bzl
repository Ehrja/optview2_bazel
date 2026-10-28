"""`optview2_aspect`: re-compiles C/C++ sources of cc_* targets with clang's
`-fsave-optimization-record` and exposes the resulting `*.opt.yaml` files.

The aspect does not touch the regular compile actions. It replays the compile
command line of every source file (same toolchain, features, copts, defines and
include paths as the real `CppCompile` action) and adds

    -fsave-optimization-record -foptimization-record-file=<declared .opt.yaml>

so the remark files are declared outputs: they are sandbox-safe, cacheable
(incl. remote caches) and only produced when explicitly requested via the
`optview2_yaml` output group or the `optview2_report` rule.
"""

load("@bazel_skylib//rules:common_settings.bzl", "BuildSettingInfo")
load("@rules_cc//cc:action_names.bzl", "ACTION_NAMES")
load("@rules_cc//cc:find_cc_toolchain.bzl", "find_cc_toolchain", "use_cc_toolchain")
load("@rules_cc//cc/common:cc_common.bzl", "cc_common")
load("@rules_cc//cc/common:cc_info.bzl", "CcInfo")

OptView2Info = provider(
    doc = "Optimization records produced by optview2_aspect (transitive).",
    fields = {
        "sources": "depset[File]: sources + headers of analyzed targets (needed to render the HTML report).",
        "yamls": "depset[File]: *.opt.yaml optimization record files.",
    },
)

C_EXTENSIONS = ["c"]
CXX_EXTENSIONS = ["cc", "cpp", "cxx", "c++", "C", "cp", "CPP"]

SKIP_TAGS = ["no-optview2", "no_optview2", "nooptview2"]

_PROPAGATE_ATTRS = ["deps", "implementation_deps"]

def is_clang(compiler, tool_path):
    """Heuristic: does the resolved C++ toolchain use clang?

    Args:
      compiler: `cc_toolchain.compiler` (free-form string from the toolchain config).
      tool_path: path of the tool used for the C++ compile action.

    Returns:
      True if the toolchain looks like clang.
    """
    compiler = (compiler or "").lower()
    tool = (tool_path or "").lower().split("/")[-1]
    return "clang" in compiler or "clang" in tool

def _setting(ctx, name):
    return getattr(ctx.attr, name)[BuildSettingInfo].value

def _transitive_infos(ctx):
    infos = []
    for attr_name in _PROPAGATE_ATTRS:
        deps = getattr(ctx.rule.attr, attr_name, None)
        if type(deps) != type([]):
            continue
        for dep in deps:
            if OptView2Info in dep:
                infos.append(dep[OptView2Info])
    return infos

def _merge(direct_yamls, direct_sources, infos):
    return OptView2Info(
        yamls = depset(direct_yamls, transitive = [i.yamls for i in infos]),
        sources = depset(direct_sources, transitive = [i.sources for i in infos]),
    )

def _providers(info):
    return [
        info,
        OutputGroupInfo(
            optview2_yaml = info.yamls,
            optview2_sources = info.sources,
        ),
    ]

def _expand_copt(ctx, copt):
    # cc rules apply make-variable substitution on copts. Location expansion is not
    # supported here (rarely used in copts); such flags are passed verbatim.
    if "$(" in copt and "$(location" not in copt and "$(execpath" not in copt:
        return ctx.expand_make_variables("copts", copt, {})
    return copt

def _rule_flags(ctx, attr_name):
    return [_expand_copt(ctx, f) for f in getattr(ctx.rule.attr, attr_name, None) or []]

def _features(ctx):
    requested = list(ctx.features)
    unsupported = list(ctx.disabled_features)
    for f in getattr(ctx.rule.attr, "features", None) or []:
        if f.startswith("-"):
            unsupported.append(f[1:])
        else:
            requested.append(f)
    return requested, unsupported

def _use_pic(cc_toolchain, feature_configuration):
    # cc_library builds PIC objects whenever the toolchain needs them for shared
    # libraries (always true for typical Linux toolchains and with --force_pic).
    return cc_toolchain.needs_pic_for_dynamic_libraries(feature_configuration = feature_configuration)

def _compilation_context(ctx, target):
    # CcInfo only carries the *public* interface. The real compile action also sees
    # the headers/includes/defines of implementation_deps, so merge them back in.
    contexts = [target[CcInfo].compilation_context]
    for dep in getattr(ctx.rule.attr, "implementation_deps", None) or []:
        if CcInfo in dep:
            contexts.append(dep[CcInfo].compilation_context)
    if len(contexts) == 1:
        return contexts[0]
    return cc_common.merge_compilation_contexts(compilation_contexts = contexts)

def _sanitize(path):
    return path.replace("../", "external/").replace(":", "_")

def _compile_one(ctx, src, is_cxx, cc_toolchain, feature_configuration, compilation_context, user_flags, extra_inputs):
    action_name = ACTION_NAMES.cpp_compile if is_cxx else ACTION_NAMES.c_compile
    stem = "_optview2/%s/%s" % (ctx.label.name, _sanitize(src.short_path))
    yaml = ctx.actions.declare_file(stem + ".opt.yaml")
    obj = ctx.actions.declare_file(stem + ".o")

    system_includes = [compilation_context.system_includes]
    if hasattr(compilation_context, "external_includes"):
        system_includes.append(compilation_context.external_includes)
    defines = [compilation_context.defines]
    if hasattr(compilation_context, "local_defines"):
        defines.append(compilation_context.local_defines)

    variables = cc_common.create_compile_variables(
        feature_configuration = feature_configuration,
        cc_toolchain = cc_toolchain,
        user_compile_flags = user_flags,
        source_file = src.path,
        output_file = obj.path,
        include_directories = compilation_context.includes,
        quote_include_directories = compilation_context.quote_includes,
        system_include_directories = depset(transitive = system_includes),
        framework_include_directories = compilation_context.framework_includes,
        preprocessor_defines = depset(transitive = defines),
        use_pic = _use_pic(cc_toolchain, feature_configuration),
    )
    command_line = cc_common.get_memory_inefficient_command_line(
        feature_configuration = feature_configuration,
        action_name = action_name,
        variables = variables,
    )
    env = cc_common.get_environment_variables(
        feature_configuration = feature_configuration,
        action_name = action_name,
        variables = variables,
    )
    compiler = cc_common.get_tool_for_action(
        feature_configuration = feature_configuration,
        action_name = action_name,
    )

    args = ctx.actions.args()
    args.add_all(command_line)

    # Defensive: toolchain configs that do not model compiler_input/output_flags.
    if "-c" not in command_line:
        args.add_all(["-c", src.path])
    if "-o" not in command_line:
        args.add_all(["-o", obj.path])
    args.add("-fsave-optimization-record")
    args.add("-foptimization-record-file=" + yaml.path)
    args.add_all(_setting(ctx, "_extra_copts"))

    ctx.actions.run(
        executable = compiler,
        arguments = [args],
        env = env,
        inputs = depset(
            [src] + extra_inputs,
            transitive = [compilation_context.headers, cc_toolchain.all_files],
        ),
        outputs = [yaml, obj],
        mnemonic = "OptView2Compile",
        progress_message = "OptView2: recording optimization remarks for " + src.short_path,
        execution_requirements = {
            t: ""
            for t in getattr(ctx.rule.attr, "tags", [])
            if t.startswith("no-") or t.startswith("requires-")
        },
    )
    return yaml

def _optview2_aspect_impl(target, ctx):
    infos = _transitive_infos(ctx)

    tags = getattr(ctx.rule.attr, "tags", None) or []
    if CcInfo not in target or any([t in SKIP_TAGS for t in tags]):
        return _providers(_merge([], [], infos))

    if target.label.workspace_name and not _setting(ctx, "_include_external"):
        return _providers(_merge([], [], infos))

    srcs = [
        f
        for f in getattr(ctx.rule.files, "srcs", None) or []
        if f.extension in C_EXTENSIONS + CXX_EXTENSIONS
    ]
    compilation_context = _compilation_context(ctx, target)
    direct_sources = (
        list(getattr(ctx.rule.files, "srcs", None) or []) +
        list(getattr(ctx.rule.files, "hdrs", None) or []) +
        list(getattr(ctx.rule.files, "textual_hdrs", None) or [])
    )
    if not srcs:
        return _providers(_merge([], direct_sources, infos))

    cc_toolchain = find_cc_toolchain(ctx)
    requested, unsupported = _features(ctx)
    feature_configuration = cc_common.configure_features(
        ctx = ctx,
        cc_toolchain = cc_toolchain,
        requested_features = requested,
        unsupported_features = unsupported,
    )

    check = _setting(ctx, "_compiler_check")
    if check != "off":
        tool = cc_common.get_tool_for_action(
            feature_configuration = feature_configuration,
            action_name = ACTION_NAMES.cpp_compile,
        )
        if not is_clang(cc_toolchain.compiler, tool):
            if check == "skip":
                return _providers(_merge([], direct_sources, infos))
            fail(("optview2_aspect requires a clang toolchain (-fsave-optimization-record), " +
                  "but %s resolved compiler '%s' (%s). Build with your clang config " +
                  "(e.g. --config=clang) or set --@optview2_bazel//optview2:compiler_check=skip.") %
                 (target.label, cc_toolchain.compiler, tool))

    cpp = ctx.fragments.cpp
    common_flags = list(cpp.copts) + _rule_flags(ctx, "copts")
    cxx_flags = list(cpp.cxxopts) + _rule_flags(ctx, "cxxopts")
    c_flags = list(cpp.conlyopts) + _rule_flags(ctx, "conlyopts")
    extra_inputs = list(getattr(ctx.rule.files, "additional_compiler_inputs", None) or [])

    yamls = []
    for src in srcs:
        is_cxx = src.extension in CXX_EXTENSIONS
        user_flags = common_flags + (cxx_flags if is_cxx else c_flags)
        yamls.append(_compile_one(
            ctx,
            src,
            is_cxx,
            cc_toolchain,
            feature_configuration,
            compilation_context,
            user_flags,
            extra_inputs,
        ))

    return _providers(_merge(yamls, direct_sources, infos))

optview2_aspect = aspect(
    implementation = _optview2_aspect_impl,
    doc = "Records clang optimization remarks (*.opt.yaml) for cc_* targets. " +
          "Request the `optview2_yaml` output group or use `optview2_report`.",
    attr_aspects = _PROPAGATE_ATTRS,
    attrs = {
        "_compiler_check": attr.label(default = Label("//optview2:compiler_check")),
        "_extra_copts": attr.label(default = Label("//optview2:extra_copts")),
        "_include_external": attr.label(default = Label("//optview2:include_external")),
    },
    fragments = ["cpp"],
    provides = [OptView2Info],
    toolchains = use_cc_toolchain(),
)
