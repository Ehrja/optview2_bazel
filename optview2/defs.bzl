"""Public API of optview2_bazel.

    load("@optview2_bazel//optview2:defs.bzl", "optview2_aspect", "optview2_report", "OptView2Info")

Command line usage of the aspect (raw *.opt.yaml files only):

    bazel build //my/pkg/... -c opt --config=<clang> \
        --aspects=@optview2_bazel//optview2:defs.bzl%optview2_aspect \
        --output_groups=optview2_yaml
"""

load("//optview2/private:aspect.bzl", _OptView2Info = "OptView2Info", _optview2_aspect = "optview2_aspect")
load("//optview2/private:report.bzl", _optview2_report = "optview2_report")

OptView2Info = _OptView2Info
optview2_aspect = _optview2_aspect
optview2_report = _optview2_report
