"""Analysis tests for optview2_aspect."""

load("@bazel_skylib//lib:unittest.bzl", "analysistest", "asserts", "unittest")
load("//optview2:defs.bzl", "OptView2Info", "optview2_aspect")
load("//optview2/private:aspect.bzl", "is_clang")  # buildifier: disable=bzl-visibility

_COMPILER_CHECK = str(Label("//optview2:compiler_check"))
_EXTRA_COPTS = str(Label("//optview2:extra_copts"))

def _basenames(files):
    return sorted([f.basename for f in files])

def _compile_actions(env):
    target = analysistest.target_under_test(env)
    return [a for a in target.actions if a.mnemonic == "OptView2Compile"]

# --- cc_library with a C++ and a C source -----------------------------------------
def _library_test_impl(ctx):
    env = analysistest.begin(ctx)
    target = analysistest.target_under_test(env)
    info = target[OptView2Info]
    asserts.equals(env, ["a.cc.opt.yaml", "b.c.opt.yaml"], _basenames(info.yamls.to_list()))
    asserts.true(env, "lib.h" in _basenames(info.sources.to_list()))

    actions = _compile_actions(env)
    asserts.equals(env, 2, len(actions))
    for action in actions:
        argv = action.argv
        asserts.true(env, "-fsave-optimization-record" in argv, "missing -fsave-optimization-record")
        record = [a for a in argv if a.startswith("-foptimization-record-file=")]
        asserts.equals(env, 1, len(record))
        yaml_out = [f for f in action.outputs.to_list() if f.path.endswith(".opt.yaml")]
        asserts.equals(env, record[0].split("=", 1)[1], yaml_out[0].path)
        asserts.true(env, "-DOPTVIEW2_LOCAL=1" in argv, "local_defines not forwarded")
        asserts.true(env, "-DOPTVIEW2_DEFINE=1" in argv, "defines not forwarded")
        asserts.true(env, "-Wno-optview2-copt" in argv, "rule copts not forwarded")
        asserts.true(env, "--extra-optview2-flag" in argv, "extra_copts flag not forwarded")
        asserts.true(env, "-c" in argv and "-o" in argv)

    cxx = [a for a in actions if a.argv[a.argv.index("-c") + 1].endswith("a.cc")][0]
    c = [a for a in actions if a.argv[a.argv.index("-c") + 1].endswith("b.c")][0]
    asserts.true(env, "-only-cxx" in cxx.argv)
    asserts.false(env, "-only-cxx" in c.argv)
    asserts.true(env, "-only-c" in c.argv)

    groups = target[OutputGroupInfo]
    asserts.equals(env, 2, len(groups.optview2_yaml.to_list()))
    return analysistest.end(env)

library_test = analysistest.make(
    _library_test_impl,
    extra_target_under_test_aspects = [optview2_aspect],
    config_settings = {
        _COMPILER_CHECK: "off",
        _EXTRA_COPTS: ["--extra-optview2-flag"],
        "//command_line_option:cxxopt": ["-only-cxx"],
        "//command_line_option:conlyopt": ["-only-c"],
    },
)

# --- transitive collection through cc_binary -> cc_library ---------------------------
def _transitive_test_impl(ctx):
    env = analysistest.begin(ctx)
    info = analysistest.target_under_test(env)[OptView2Info]
    asserts.equals(env, ["a.cc.opt.yaml", "b.c.opt.yaml", "main.cc.opt.yaml"], _basenames(info.yamls.to_list()))
    return analysistest.end(env)

transitive_test = analysistest.make(
    _transitive_test_impl,
    extra_target_under_test_aspects = [optview2_aspect],
    config_settings = {_COMPILER_CHECK: "off"},
)

# --- implementation_deps are visible to the replayed compile --------------------------
def _implementation_deps_test_impl(ctx):
    env = analysistest.begin(ctx)
    actions = [a for a in _compile_actions(env) if a.argv[a.argv.index("-c") + 1].endswith("uses_impl.cc")]
    asserts.equals(env, 1, len(actions))
    action = actions[0]
    asserts.true(env, "impl.h" in _basenames(action.inputs.to_list()), "implementation_deps header missing")
    asserts.true(env, "-DOPTVIEW2_IMPL_DEFINE=1" in action.argv, "implementation_deps define missing")

    # implementation_deps are also analyzed (aspect propagation).
    info = analysistest.target_under_test(env)[OptView2Info]
    asserts.equals(env, ["impl.cc.opt.yaml", "uses_impl.cc.opt.yaml"], _basenames(info.yamls.to_list()))
    return analysistest.end(env)

implementation_deps_test = analysistest.make(
    _implementation_deps_test_impl,
    extra_target_under_test_aspects = [optview2_aspect],
    config_settings = {_COMPILER_CHECK: "off"},
)

# --- header-only and opted-out targets ------------------------------------------------
def _no_yaml_test_impl(ctx):
    env = analysistest.begin(ctx)
    asserts.equals(env, 0, len(_compile_actions(env)))
    info = analysistest.target_under_test(env)[OptView2Info]
    asserts.equals(env, [], info.yamls.to_list())
    return analysistest.end(env)

no_yaml_test = analysistest.make(
    _no_yaml_test_impl,
    extra_target_under_test_aspects = [optview2_aspect],
    config_settings = {_COMPILER_CHECK: "off"},
)

# --- compiler_check=skip on a non-clang toolchain produces nothing --------------------
def _skip_test_impl(ctx):
    env = analysistest.begin(ctx)
    actions = _compile_actions(env)
    if ctx.attr.host_is_clang:
        asserts.equals(env, 1, len(actions))
    else:
        asserts.equals(env, 0, len(actions))
    return analysistest.end(env)

skip_test = analysistest.make(
    _skip_test_impl,
    extra_target_under_test_aspects = [optview2_aspect],
    config_settings = {_COMPILER_CHECK: "skip"},
    attrs = {"host_is_clang": attr.bool()},
)

# --- pure function unit test ------------------------------------------------------------
def _is_clang_test_impl(ctx):
    env = unittest.begin(ctx)
    asserts.true(env, is_clang("clang", "/usr/bin/cc"))
    asserts.true(env, is_clang("compiler", "external/llvm/bin/clang++"))
    asserts.true(env, is_clang("", "bin/clang-19"))
    asserts.false(env, is_clang("gcc", "/usr/bin/gcc"))
    asserts.false(env, is_clang("compiler", "/usr/bin/g++"))
    asserts.false(env, is_clang(None, None))
    return unittest.end(env)

is_clang_test = unittest.make(_is_clang_test_impl)
