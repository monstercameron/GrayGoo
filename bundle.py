"""One-command demo bundle export (issues.md #102). Stdlib only, offline.

``python bundle.py --out artifacts/graygoo-demo.zip`` packs the
reviewable core of the project — thesis + gate docs, the frozen
transfer set with its stubs and hash pins, and a fresh
reproducibility manifest — into a single zip, with an inner
``bundle-manifest.json`` recording git HEAD plus sha256 of every
member so a reviewer can verify integrity without the repo.

Security: like ``manifest.py``, the bundle never contains secrets.
``.env`` is not a member, and the test asserts the live key (when
present) is not a substring of any member.
"""

import hashlib
import json
import os
import subprocess
import sys
import zipfile

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_ROOT)
import manifest  # noqa: E402

BUNDLE_VERSION = 1

# Fixed reviewable core: docs a reviewer needs + frozen eval inputs.
MEMBERS = (
    "README.md",
    "AGENTS.md",
    "todos.md",
    "issues.md",
    "documents/thesis.md",
    "documents/benchmark-plan.md",
    "documents/core-experiment-gate.md",
    "documents/transfer2.md",
    "documents/learning-efficiency.md",
    "benchmarks/family-a/transfer.json",
    "benchmarks/family-a/README.md",
    "benchmarks/family-a/recorded/stub_all_pass.json",
    "benchmarks/family-a/recorded/stub_all_fail.json",
)


def git_head():
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    head = (out.stdout or "").strip()
    return head if head else "unknown"


def build_bundle(out_path):
    files = {}
    with zipfile.ZipFile(out_path, "w",
                         zipfile.ZIP_DEFLATED) as zf:
        for rel in MEMBERS:
            full = os.path.join(REPO_ROOT, rel)
            with open(full, "rb") as fh:
                blob = fh.read()
            zf.writestr(rel, blob)
            files[rel] = hashlib.sha256(blob).hexdigest()
        inner = {
            "bundle_version": BUNDLE_VERSION,
            "git_head": git_head(),
            "files": files,
            "manifest": manifest.collect_manifest(),
        }
        inner_text = json.dumps(inner, indent=2,
                                sort_keys=True) + "\n"
        zf.writestr("bundle-manifest.json",
                    inner_text.encode("utf-8"))
    return out_path


def main(argv):
    out = None
    args = list(argv)
    while args:
        arg = args.pop(0)
        if arg == "--out" and args:
            out = args.pop(0)
        elif arg in ("-h", "--help"):
            print(__doc__.strip())
            return 0
    if not out:
        print("usage: python bundle.py --out <file.zip>")
        return 2
    for rel in MEMBERS:
        full = os.path.join(REPO_ROOT, rel)
        if not os.path.isfile(full):
            print("missing member: %s" % rel)
            return 1
    build_bundle(out)
    print("wrote %s (%d members)" % (out, len(MEMBERS)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
