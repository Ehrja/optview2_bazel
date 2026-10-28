# BUILD file overlay for https://github.com/OfekShilon/optview2
#
# OptView2 is a set of plain python scripts. `opt-viewer.py` is not importable by
# name (dash), so @optview2_bazel//tools:report.py loads it via importlib from the
# directory of `optrecord.py`.

load("@rules_python//python:py_library.bzl", "py_library")

package(default_visibility = ["//visibility:public"])

exports_files(glob(["*.py"]) + ["config.yaml"])

py_library(
    name = "optview2",
    srcs = [
        "config_parser.py",
        "opt-viewer.py",
        "optpmap.py",
        "optrecord.py",
    ],
    data = ["config.yaml"] + glob(["assets/*"]),
    imports = ["."],
    deps = [
        "@optview2_bazel//tools:pygments",
        "@optview2_bazel//tools:pyyaml",
    ],
)
