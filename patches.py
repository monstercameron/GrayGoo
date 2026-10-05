"""Executable patch memory (plan.md sections 10, 29-31).

A patch is a validated-but-task-specific candidate that persists
temporarily and may later be promoted to a skill after transfer
evidence. Patches have short TTLs (default 7 days, plan.md section 31:
patch 1-7 days); expiry is enforced on READ (``get_patch`` /
``find_for_task`` return ``None``/exclude expired patches even before
:meth:`PatchStore.sweep` deletes them).

Storage: plain JSON files under ``directory`` (stdlib only).
Reuse outcomes are appended to a JSONL log.

Patch record shape::

    {
        "patch_id": str,
        "task_id": str,
        "candidate": <jsonable candidate>,
        "created_at": float (epoch seconds),
        "ttl_days": float (default 7),
        "status": str (default "patch"),
        "task_family": str,
        "tags": [str],
    }

Reuse outcome shape::

    {
        "patch_id": str,
        "task_id": str,
        "helped": bool,
        "tokens_saved": float,
        "latency_saved_ms": float,
        "recorded_at": float,
        ...extra caller fields (success, severe, held_out, ...)
    }
"""

import hashlib
import json
import os
import time

DEFAULT_TTL_DAYS = 7
SECONDS_PER_DAY = 86400.0


def _now():
    return time.time()


def _is_expired(patch, now=None):
    """True when ``patch`` is past its TTL."""
    if patch is None:
        return True
    if now is None:
        now = _now()
    try:
        created = float(patch.get("created_at", 0.0))
    except (TypeError, ValueError):
        return True
    try:
        ttl = float(patch.get("ttl_days", DEFAULT_TTL_DAYS))
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL_DAYS
    return (created + ttl * SECONDS_PER_DAY) < now


def _patch_filename(patch_id):
    # Keep filenames safe: hash the id, keep a short readable prefix.
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_"
                   for c in str(patch_id))
    digest = hashlib.sha256(str(patch_id).encode("utf-8")).hexdigest()[:12]
    return "%s-%s.json" % (safe[:48] or "patch", digest)


def _generate_patch_id(task_id, candidate, created_at):
    material = json.dumps({"task_id": task_id, "candidate": candidate,
                           "created_at": created_at}, sort_keys=True,
                          default=str)
    return "patch-" + hashlib.sha256(
        material.encode("utf-8")).hexdigest()[:16]


class PatchStore:
    """JSON-file backed store of executable patches + reuse outcomes."""

    def __init__(self, directory):
        self.directory = os.path.abspath(directory)
        os.makedirs(self.directory, exist_ok=True)
        self._patches_dir = os.path.join(self.directory, "patches")
        os.makedirs(self._patches_dir, exist_ok=True)
        self._reuse_path = os.path.join(self.directory, "reuse.jsonl")

    # -- internal helpers ------------------------------------------
    def _patch_path(self, patch_id):
        return os.path.join(self._patches_dir, _patch_filename(patch_id))

    def _write_patch(self, patch):
        path = self._patch_path(patch["patch_id"])
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(patch, fh, sort_keys=True, default=str)
        os.replace(tmp, path)
        return patch

    def _read_patch_file(self, path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _iter_patch_files(self):
        try:
            names = sorted(os.listdir(self._patches_dir))
        except OSError:
            return
        for name in names:
            if name.endswith(".json"):
                yield os.path.join(self._patches_dir, name)

    # -- patch lifecycle -------------------------------------------
    def save_patch(self, task_id, candidate, task_family=None, tags=None,
                   ttl_days=DEFAULT_TTL_DAYS, status="patch", patch_id=None,
                   created_at=None, **extra):
        """Persist a successful candidate as a patch; returns the record."""
        if created_at is None:
            created_at = _now()
        if patch_id is None:
            patch_id = _generate_patch_id(task_id, candidate, created_at)
        patch = {
            "patch_id": patch_id,
            "task_id": task_id,
            "candidate": candidate,
            "created_at": float(created_at),
            "ttl_days": float(ttl_days),
            "status": status,
            "task_family": task_family or extra.get("family", "") or "",
            "tags": list(tags or extra.get("task_tags") or []),
        }
        for key, value in extra.items():
            if key in ("family", "task_tags"):
                continue
            patch[key] = value
        return self._write_patch(patch)

    # Aliases so callers using sibling naming still work.
    store_patch = save_patch
    add_patch = save_patch
    persist_patch = save_patch
    create_patch = save_patch
    save = save_patch
    store = save_patch

    def get_patch(self, patch_id, now=None):
        """Return the patch dict, or None when missing/expired.

        Expiry is enforced on read: an expired patch yields None even
        before :meth:`sweep` deletes it from disk.
        """
        path = self._patch_path(patch_id)
        if not os.path.exists(path):
            # Fall back to scanning (covers patches written with an
            # older filename scheme, if any).
            for candidate_path in self._iter_patch_files():
                data = self._read_patch_file(candidate_path)
                if data and data.get("patch_id") == patch_id:
                    path = candidate_path
                    break
            else:
                return None
            patch = self._read_patch_file(path)
        else:
            patch = self._read_patch_file(path)
        if patch is None:
            return None
        if _is_expired(patch, now):
            return None
        return patch

    get = get_patch
    load_patch = get_patch

    def list_patches(self, include_expired=False, now=None):
        """Return all stored patches (expired excluded by default)."""
        out = []
        for path in self._iter_patch_files():
            patch = self._read_patch_file(path)
            if patch is None:
                continue
            if not include_expired and _is_expired(patch, now):
                continue
            out.append(patch)
        out.sort(key=lambda p: p.get("created_at", 0.0))
        return out

    list_all = list_patches

    def find_for_task(self, task_family=None, tags=None, family=None,
                      task_tags=None, limit=None, include_expired=False,
                      now=None, **kwargs):
        """Retrieve live patches relevant to a later related task.

        Accepts ``find_for_task("parsing")``, ``find_for_task(tags=[...])``,
        ``find_for_task({"family": ..., "tags": ...})``, or keyword args.
        Family match is exact; tag match scores by overlap. Expired
        patches are excluded unless ``include_expired`` is true.
        """
        # Flexible first-arg handling.
        if isinstance(task_family, dict):
            blob = task_family
            task_family = blob.get("task_family", blob.get("family"))
            tags = tags if tags is not None else blob.get(
                "tags", blob.get("task_tags"))
        elif isinstance(task_family, (list, tuple)):
            tags = list(task_family) if tags is None else tags
            task_family = None
        if family is not None and task_family is None:
            task_family = family
        if task_tags is not None and tags is None:
            tags = task_tags
        if "task" in kwargs and task_family is None and tags is None:
            # find_for_task(task={...}) convenience.
            task = kwargs["task"]
            if isinstance(task, dict):
                task_family = task.get("task_family", task.get("family"))
                tags = task.get("tags", task.get("task_tags"))
            elif isinstance(task, str):
                task_family = task
        want_tags = set(tags or [])
        scored = []
        for patch in self.list_patches(include_expired=include_expired,
                                       now=now):
            if task_family and patch.get("task_family") != task_family:
                continue
            patch_tags = set(patch.get("tags") or [])
            if want_tags and not (want_tags & patch_tags):
                # When both a family and tags are given, require the tag
                # overlap too; when only tags are given, same rule.
                continue
            overlap = len(want_tags & patch_tags) if want_tags else 0
            scored.append((overlap, patch.get("created_at", 0.0), patch))
        # Most overlapping tags first, then newest.
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        patches = [p for _, _, p in scored]
        if limit is not None and limit >= 0:
            patches = patches[:limit]
        return patches

    # Alias used by sibling retrieval naming.
    find_patches_for_task = find_for_task

    # -- reuse outcomes --------------------------------------------
    def record_reuse(self, patch_id, task_id, helped=False, tokens_saved=0,
                     latency_saved_ms=0.0, **extra):
        """Append one reuse outcome; returns the stored record."""
        if isinstance(patch_id, dict):
            # record_reuse({...outcome...}) convenience.
            blob = dict(patch_id)
            patch_id = blob.pop("patch_id", None)
            task_id = blob.pop("task_id", task_id)
            helped = blob.pop("helped", blob.pop("success", helped))
            tokens_saved = blob.pop("tokens_saved", tokens_saved)
            latency_saved_ms = blob.pop("latency_saved_ms",
                                        latency_saved_ms)
            extra = dict(blob, **extra)
        outcome = {
            "patch_id": patch_id,
            "task_id": task_id,
            "helped": bool(extra.pop("success", helped))
            if "success" in extra else bool(helped),
            "tokens_saved": tokens_saved,
            "latency_saved_ms": latency_saved_ms,
            "recorded_at": _now(),
        }
        outcome.update(extra)
        # Normalize common success aliases.
        if "succeeded" in outcome and "helped" not in extra:
            outcome["helped"] = bool(outcome.pop("succeeded"))
        with open(self._reuse_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(outcome, sort_keys=True, default=str) + "\n")
        return outcome

    log_reuse = record_reuse
    record_outcome = record_reuse
    log_outcome = record_reuse

    def get_reuses(self, patch_id=None):
        """Return recorded reuse outcomes (all, or filtered by patch)."""
        outcomes = []
        if not os.path.exists(self._reuse_path):
            return outcomes
        with open(self._reuse_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if patch_id is not None and row.get("patch_id") != patch_id:
                    continue
                outcomes.append(row)
        return outcomes

    get_outcomes = get_reuses
    list_reuses = get_reuses
    list_outcomes = get_reuses

    # -- expiration --------------------------------------------------
    def sweep(self, now=None):
        """Delete expired patches; report counts.

        Returns ``{"swept": n, "remaining": m}`` (with ``deleted`` /
        ``expired`` / ``kept`` aliases for the same counts).
        """
        if now is None:
            now = _now()
        swept = 0
        remaining = 0
        for path in list(self._iter_patch_files()):
            patch = self._read_patch_file(path)
            if patch is None:
                continue
            if _is_expired(patch, now):
                try:
                    os.remove(path)
                except OSError:
                    remaining += 1
                    continue
                swept += 1
            else:
                remaining += 1
        return {"swept": swept, "deleted": swept, "expired": swept,
                "remaining": remaining, "kept": remaining}

    expire_patches = sweep
    purge_expired = sweep
