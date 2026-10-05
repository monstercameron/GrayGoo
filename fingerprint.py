"""Strong generation fingerprint (issues.md #4).

The old fingerprint (``workers.generation_fingerprint``:
``sbcl=<ver>|asd=<hash>|epoch=<id>``) cannot see source edits: most of
the tree can change without moving it. This module hashes everything
that defines a generation:

* source tree: every ``src/**/*.lisp`` file (sorted posix relpath +
  content sha256, folded into one digest),
* ``graygoo.asd``: full-file hash plus parsed ``:version`` and
  ``:depends-on``,
* dependency list: the ASDF ``:depends-on`` entries plus the SBCL and
  Python versions observed,
* capability manifest: the ``src/capability/*.lisp`` file set by
  default, optionally extended/replaced by a caller-supplied
  ``{capability-id: version}`` mapping,
* schema generation: caller-supplied state generation plus the
  ``src/state/*.lisp`` file-set hash,
* protocol version: :data:`PROTOCOL_VERSION` (worker stdio + evaluator
  protocol generation).

Stdlib plus ``hashlib`` only — no sibling imports, so lower layers
(``workers.py``, ``pool.py``) can adopt it without cycles.

Coordinator wiring (NOT applied here: ``workers.py`` is owned by the
sandbox lane). Replace ``workers.generation_fingerprint`` with::

    import fingerprint

    def generation_fingerprint(epoch_id=""):
        return fingerprint.compute_string(epoch_id=epoch_id)

That is the whole swap: same call signature, stronger digest.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys

__all__ = [
    "PROTOCOL_VERSION",
    "SCHEMA_GENERATION_DEFAULT",
    "compute_dict",
    "compute_string",
    "matches",
]

#: Worker stdio + evaluator protocol generation. Bump when the
#: pool.py stdio envelope or evaluator/protocol.py wire format changes.
PROTOCOL_VERSION = 1

#: Default schema generation when the caller supplies none (bootstrap).
SCHEMA_GENERATION_DEFAULT = 0

_ROOT = os.path.dirname(os.path.abspath(__file__))

_ASD_VERSION_RE = re.compile(r":version\s+\"([^\"]+)\"")
_ASD_DEPENDS_RE = re.compile(r":depends-on\s+\(([^)]*)\)")


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _lisp_files(root, subdir):
    """Sorted ``(posix-relpath, abspath)`` pairs for ``*.lisp`` files."""
    found = []
    top = os.path.join(root, subdir)
    for dirpath, _dirnames, filenames in os.walk(top):
        for name in filenames:
            if name.endswith(".lisp"):
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                found.append((rel, full))
    found.sort()
    return found


def _fold_files(pairs):
    """Fold ``(relpath, path)`` pairs into one sha256 hex digest."""
    digest = hashlib.sha256()
    for rel, path in pairs:
        try:
            content_hash = _sha256_file(path)
        except OSError:
            content_hash = "missing"
        digest.update(rel.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(content_hash.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _parse_asd(path):
    """Return ``(file_hash, version, depends)`` for an ``.asd`` file."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return "missing", "missing", []
    text = raw.decode("utf-8", errors="replace")
    version_match = _ASD_VERSION_RE.search(text)
    depends_match = _ASD_DEPENDS_RE.search(text)
    version = version_match.group(1) if version_match else "unknown"
    depends = []
    if depends_match:
        depends = sorted(
            token.strip("\"'")
            for token in depends_match.group(1).split()
            if token.strip("\"'")
        )
    return _sha256_bytes(raw), version, depends


def _sbcl_version(sbcl_exe=None):
    """Best-effort SBCL version line; ``"unknown"`` on any failure."""
    exe = (
        sbcl_exe
        or os.environ.get("GRAYGOO_SBCL")
        or (
            os.path.join(
                os.environ.get("LOCALAPPDATA", ""),
                "sbcl-local",
                "sbcl-2.6.9",
                "PFiles",
                "Steel Bank Common Lisp",
                "sbcl.exe",
            )
        )
    )
    try:
        proc = subprocess.run(
            [exe, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
        first = proc.stdout.decode("utf-8", errors="replace").splitlines()
        return first[0].strip() if first else "unknown"
    except Exception:
        return "unknown"


def _manifest_digest(manifest):
    """Digest a caller-supplied ``{capability-id: version}`` mapping."""
    digest = hashlib.sha256()
    for key in sorted(manifest, key=repr):
        digest.update(repr(key).encode("utf-8"))
        digest.update(b"\x00")
        digest.update(repr(manifest[key]).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def compute_dict(root=None, *, capability_manifest=None,
                 schema_generation=None, protocol_version=None,
                 sbcl_exe=None, epoch_id=""):
    """Compute the full generation fingerprint as a dict.

    :param root: repo root (defaults to this file's directory).
    :param capability_manifest: optional ``{id: version}`` mapping
        folded into the capability digest alongside the
        ``src/capability/`` file set.
    :param schema_generation: state generation number (defaults to
        :data:`SCHEMA_GENERATION_DEFAULT`).
    :param protocol_version: override for :data:`PROTOCOL_VERSION`.
    :param sbcl_exe: SBCL path for the version probe (default:
        ``GRAYGOO_SBCL`` or the repo-standard path).
    :param epoch_id: caller-supplied epoch tag (``"-"`` when empty),
        matching the ``workers.generation_fingerprint`` convention.
    :returns: dict with ``source_tree``, ``asd`` (hash/version/
        depends), ``dependencies``, ``capability_manifest``,
        ``schema`` (generation/tree), ``protocol_version``,
        ``epoch``, ``digest`` (the folded full digest), and
        ``files`` (count of hashed Lisp files).
    """
    root = os.path.abspath(root or _ROOT)
    if not os.path.isdir(root):
        raise ValueError("fingerprint root is not a directory: %r" % (root,))
    if capability_manifest is not None and not isinstance(
        capability_manifest, dict
    ):
        raise TypeError("capability_manifest must be a dict or None")
    if schema_generation is None:
        schema_generation = SCHEMA_GENERATION_DEFAULT
    if protocol_version is None:
        protocol_version = PROTOCOL_VERSION

    src_pairs = _lisp_files(root, "src")
    cap_pairs = _lisp_files(root, os.path.join("src", "capability"))
    state_pairs = _lisp_files(root, os.path.join("src", "state"))
    source_tree = _fold_files(src_pairs)
    capability_tree = _fold_files(cap_pairs)
    schema_tree = _fold_files(state_pairs)

    asd_hash, asd_version, asd_depends = _parse_asd(
        os.path.join(root, "graygoo.asd")
    )
    sbcl_version = _sbcl_version(sbcl_exe)
    dependencies = {
        "asd_depends_on": list(asd_depends),
        "sbcl": sbcl_version,
        "python": sys.version.split()[0],
    }
    deps_digest = _sha256_bytes(
        ("\x00".join(
            ["sbcl=%s" % sbcl_version,
             "python=%s" % dependencies["python"]]
            + ["dep=%s" % dep for dep in asd_depends]
        )).encode("utf-8")
    )
    manifest_extra = (
        _manifest_digest(capability_manifest)
        if capability_manifest
        else None
    )
    capability_digest = _sha256_bytes(
        (capability_tree + "\x00" + (manifest_extra or "-")).encode("utf-8")
    )
    epoch = epoch_id if epoch_id else "-"
    folded = _sha256_bytes(
        "\x00".join(
            [
                source_tree,
                asd_hash,
                deps_digest,
                capability_digest,
                "%r:%s" % (schema_generation, schema_tree),
                "proto=%r" % (protocol_version,),
            ]
        ).encode("utf-8")
    )
    return {
        "source_tree": source_tree,
        "source_files": [rel for rel, _ in src_pairs],
        "asd": {
            "hash": asd_hash,
            "version": asd_version,
            "depends_on": list(asd_depends),
        },
        "dependencies": dependencies,
        "capability_manifest": {
            "tree": capability_tree,
            "extra": manifest_extra,
            "digest": capability_digest,
        },
        "schema": {
            "generation": schema_generation,
            "tree": schema_tree,
        },
        "protocol_version": protocol_version,
        "epoch": epoch,
        "digest": folded,
        "files": len(src_pairs),
    }


def compute_string(root=None, **kwargs):
    """Render the canonical one-line fingerprint string.

    Same arguments as :func:`compute_dict`. Format::

        ggfp1:src=<12>|asd=<12>|deps=<12>|caps=<12>|schema=<gen>:<12>|proto=<v>|epoch=<id>

    (12-char digest prefixes; full hex lives in :func:`compute_dict`.)
    """
    info = compute_dict(root, **kwargs)
    short = lambda value: value[:12] if isinstance(value, str) else "missing"
    deps_digest = _sha256_bytes(
        "\x00".join(
            ["sbcl=%s" % info["dependencies"]["sbcl"],
             "python=%s" % info["dependencies"]["python"]]
            + ["dep=%s" % dep
               for dep in info["dependencies"]["asd_depends_on"]]
        ).encode("utf-8")
    )
    return (
        "ggfp1:src=%s|asd=%s|deps=%s|caps=%s|schema=%r:%s|proto=%r|epoch=%s"
        % (
            short(info["source_tree"]),
            short(info["asd"]["hash"]),
            short(deps_digest),
            short(info["capability_manifest"]["digest"]),
            info["schema"]["generation"],
            short(info["schema"]["tree"]),
            info["protocol_version"],
            info["epoch"],
        )
    )


def matches(rendered, root=None, **kwargs):
    """True when RENDERED equals a fresh fingerprint for these inputs.

    Compares against :func:`compute_string` with the same keyword
    arguments (minus ``epoch_id`` drift: pass the same epoch through).
    """
    if not isinstance(rendered, str):
        return False
    try:
        return rendered == compute_string(root, **kwargs)
    except (OSError, ValueError):
        return False
