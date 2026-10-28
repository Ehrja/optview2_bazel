"""Module extension that provides the `@optview2` repository.

By default a pinned upstream commit of https://github.com/OfekShilon/optview2 is
downloaded. The root module can redirect it to a local checkout or a different
archive:

```starlark
optview2 = use_extension("@optview2_bazel//optview2:extensions.bzl", "optview2")
optview2.local(path = "/path/to/optview2")
# or
optview2.archive(urls = [...], sha256 = "...", strip_prefix = "...")
```
"""

load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive")

_DEFAULT_COMMIT = "aec2d71f984e61884088834e3d2742c31a25bc12"
_DEFAULT_SHA256 = "fc1955f5e4535f8faadd931c4691e8ff46afa9568820d1828005cfb09876a496"
_BUILD_FILE = Label("//third_party:optview2.BUILD")

def _local_optview2_impl(rctx):
    src = rctx.path(rctx.attr.path)
    if not src.exists:
        fail("optview2.local(path = '%s') does not exist" % rctx.attr.path)
    for child in src.readdir():
        if child.basename in (".git", "venv", "__pycache__", "BUILD", "BUILD.bazel"):
            continue
        rctx.symlink(child, child.basename)
    rctx.symlink(rctx.attr.build_file, "BUILD.bazel")
    rctx.file("REPO.bazel", "")

_local_optview2 = repository_rule(
    implementation = _local_optview2_impl,
    attrs = {
        "build_file": attr.label(allow_single_file = True),
        "path": attr.string(mandatory = True),
    },
    local = True,
)

_local_tag = tag_class(attrs = {
    "path": attr.string(mandatory = True, doc = "Absolute path to an optview2 checkout."),
})

_archive_tag = tag_class(attrs = {
    "sha256": attr.string(),
    "strip_prefix": attr.string(),
    "urls": attr.string_list(mandatory = True),
})

def _optview2_ext_impl(mctx):
    local = None
    archive = None

    # `mctx.modules` is in BFS order starting at the root module, so the module
    # closest to the root that configures the source wins.
    for mod in mctx.modules:
        if mod.tags.local:
            local = mod.tags.local[0]
            break
        if mod.tags.archive:
            archive = mod.tags.archive[0]
            break

    if local:
        _local_optview2(name = "optview2", path = local.path, build_file = _BUILD_FILE)
    elif archive:
        http_archive(
            name = "optview2",
            urls = archive.urls,
            sha256 = archive.sha256,
            strip_prefix = archive.strip_prefix,
            build_file = _BUILD_FILE,
        )
    else:
        http_archive(
            name = "optview2",
            urls = ["https://github.com/OfekShilon/optview2/archive/%s.tar.gz" % _DEFAULT_COMMIT],
            sha256 = _DEFAULT_SHA256,
            strip_prefix = "optview2-%s" % _DEFAULT_COMMIT,
            build_file = _BUILD_FILE,
        )

optview2 = module_extension(
    implementation = _optview2_ext_impl,
    tag_classes = {
        "archive": _archive_tag,
        "local": _local_tag,
    },
)
