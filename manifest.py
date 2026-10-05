"""Reproducibility manifest (issues.md #69). Stdlib only, offline.

``graygoo.asd`` presents a dependency-free Lisp system, but a GrayGoo
generation is really defined by Lisp + Python + SBCL + OS sandbox +
model/provider configuration together. This module collects all of
that into one JSON-serializable dict so any run, experiment, or bug
report can pin exactly what it ran on.

Security: the manifest NEVER contains secrets. It does not read
``.env`` and does not touch ``CEREBRAS_API_KEY``; it records only the
provider name, base URL, model id, and list prices. The test suite
asserts the live key (when present in the environment) is not a
substring of the rendered manifest.

Usage:
    python manifest.py                # print manifest JSON to stdout
    python manifest.py --write out.json
    python manifest.py --check        # exit 0 iff required keys present
"""

import hashlib
import json
import os
import platform
import subprocess
import sys

MANIFEST_VERSION = 1

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

REQUIRED_KEYS = (
    "manifest_version",
    "python",
    "platform",
    "git",
    "sbcl",
    "asdf",
    "source_tree",
    "deps",
    "model",
    "sandbox",
)


def _run(argv, timeout=15):
    """Run argv capturing text output; return stdout or "" on any failure."""
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            cwd=REPO_ROOT,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    try:
        return proc.stdout.decode("utf-8", "replace").strip()
    except Exception:
        return ""


def _sha256_file(path):
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def _git_info():
    sha = _run(["git", "rev-parse", "HEAD"]) or None
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"]) or None
    status = _run(["git", "status", "--porcelain=v1"])
    return {
        "sha": sha,
        "branch": branch,
        # None when git is missing (not a checkout); else True/False.
        "dirty": (len(status) > 0) if (sha or branch or status) else None,
    }


def _sbcl_info():
    exe = os.environ.get("GRAYGOO_SBCL")
    if not exe:
        for candidate in ("sbcl", "sbcl.exe"):
            found = _run(["where" if os.name == "nt" else "which",
                          candidate])
            if found:
                exe = found.splitlines()[0].strip()
                break
    version = _run([exe, "--version"]) if exe else ""
    first_line = version.splitlines()[0] if version else ""
    return {"exe": exe, "version": first_line or "unknown"}


def _asdf_info():
    path = os.path.join(REPO_ROOT, "graygoo.asd")
    return {"file": "graygoo.asd",
            "sha256": _sha256_file(path),
            "external_dependencies": []}


def _source_tree():
    try:
        sys.path.insert(0, REPO_ROOT)
        import fingerprint
        return {"fingerprint": fingerprint.compute_string()}
    except Exception as exc:
        return {"fingerprint": None, "error": "%s: %s" % (
            type(exc).__name__, exc)}
    finally:
        try:
            sys.path.remove(REPO_ROOT)
        except ValueError:
            pass


def _dep_version(dist_name):
    try:
        from importlib import metadata
        return metadata.version(dist_name)
    except Exception:
        return None


def _model_info():
    # Import locally: constants only, never credentials.
    sys.path.insert(0, REPO_ROOT)
    try:
        import cerebras_client
        return {
            "provider": "cerebras",
            "base_url": cerebras_client.BASE_URL,
            "model": cerebras_client.DEFAULT_MODEL,
            "price_input_usd_per_mtok":
                cerebras_client.COST_INPUT_USD_PER_MTOK,
            "price_output_usd_per_mtok":
                cerebras_client.COST_OUTPUT_USD_PER_MTOK,
        }
    except Exception as exc:
        return {"provider": "cerebras", "error": "%s: %s" % (
            type(exc).__name__, exc)}
    finally:
        try:
            sys.path.remove(REPO_ROOT)
        except ValueError:
            pass


def _sandbox_info():
    # Declared posture, kept in sync with sandbox.py / worker.lisp /
    # documents/adversarial-report.md. OS enforcement is NOT present;
    # this section exists so the manifest cannot silently claim more.
    return {
        "os_enforcement": False,
        "same_user": True,
        "job_objects": False,
        "containers": False,
        "layers": ["risk.py static gates", "sandbox.py prelude deny",
                   "worker.lisp in-image sandbox", "effects.py harness",
                   "sanitized worker env"],
    }


def collect_manifest():
    """Collect the full reproducibility manifest dict."""
    import datetime
    return {
        "manifest_version": MANIFEST_VERSION,
        "collected_at": datetime.datetime.now(
            datetime.timezone.utc).isoformat(timespec="seconds"),
        "python": {"version": platform.python_version(),
                   "implementation": platform.python_implementation()},
        "platform": {"system": platform.system(),
                     "release": platform.release(),
                     "machine": platform.machine()},
        "git": _git_info(),
        "sbcl": _sbcl_info(),
        "asdf": _asdf_info(),
        "source_tree": _source_tree(),
        "deps": {"openai": _dep_version("openai"),
                 "python-dotenv": _dep_version("python-dotenv")},
        "model": _model_info(),
        "sandbox": _sandbox_info(),
    }


def check_manifest(manifest):
    """Return a list of problems; empty means the manifest is valid."""
    problems = []
    if not isinstance(manifest, dict):
        return ["manifest is not a dict"]
    for key in REQUIRED_KEYS:
        if key not in manifest:
            problems.append("missing key: %s" % key)
    try:
        json.dumps(manifest)
    except (TypeError, ValueError) as exc:
        problems.append("not JSON-serializable: %s" % exc)
    return problems


def main(argv):
    if "--check" in argv:
        problems = check_manifest(collect_manifest())
        for problem in problems:
            print("manifest problem: %s" % problem)
        return 1 if problems else 0
    manifest = collect_manifest()
    text = json.dumps(manifest, indent=2, sort_keys=True)
    if "--write" in argv:
        index = argv.index("--write")
        if index + 1 >= len(argv):
            print("missing path after --write")
            return 2
        with open(argv[index + 1], "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
        return 0
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
