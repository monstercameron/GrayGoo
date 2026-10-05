"""Executable patch memory adapter for Baselines C/D (Family A).

Persists Phase 1 exposure successes as executable-memory patches via
the root ``patches.py`` PatchStore, retrieves patches for Phase 2
related tasks, and records reuse outcomes (helped / negative
transfer). Retrieval is fully offline (tag overlap, no model calls).

For Family A data-transformation tasks the stored "candidate" is the
verified procedure exemplar: the task prompt plus one solved
input->output pair. "Reuse/adapt" means injecting the retrieved patch
exemplars into the Phase 2 system prompt so the model adapts the
stored procedure to the new input — one model call per check, exactly
as in Baseline A.

Patch layout (via :class:`PatchStore`)::

    task_id      Phase 1 task id, e.g. "A-EXP-05"
    candidate    {"prompt":..., "input":..., "output":..., "check":n}
    task_family  "A"
    tags         [category, "family-a", task-id]

Reuse outcome (via :meth:`PatchStore.record_reuse`)::

    patch_id, task_id (Phase 2 id), helped (Phase 2 task passed),
    held_out (split == "transfer"), success alias, split/category.
"""

import os
import sys
from pathlib import Path

# Import the root patches module without touching it (read-only reuse).
# The root file may be edited concurrently by another lane, so the
# import is lazy and failures raise a clear error.
_PatchStore = None
_patches_import_error = None


def _get_patch_store_cls():
    global _PatchStore, _patches_import_error
    if _PatchStore is not None:
        return _PatchStore
    if _patches_import_error is not None:
        raise RuntimeError(
            "patch memory unavailable: %s" % _patches_import_error
        )
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import patches  # noqa: PLC0415
    except Exception as exc:
        _patches_import_error = exc
        raise RuntimeError(
            "patch memory unavailable: %s" % exc
        ) from exc
    cls = getattr(patches, "PatchStore", None)
    if cls is None:
        _patches_import_error = "patches.PatchStore not found"
        raise RuntimeError(
            "patch memory unavailable: patches.PatchStore not found"
        )
    _PatchStore = cls
    return _PatchStore


def summarize_transfer_outcomes(outcomes, baseline_success=0.0):
    """Summarize reuse outcomes with the root transfer module.

    Falls back to a local equivalent when transfer.py is unavailable,
    so the offline path never hard-depends on another lane's file.
    Returns the metrics dict (reuse_count, success_rate,
    negative_transfer_count, severe_regressions, ...).
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import transfer  # noqa: PLC0415
    except Exception:
        transfer = None
    summarize = getattr(transfer, "summarize_outcomes", None) if transfer else None
    if callable(summarize):
        return summarize(list(outcomes or []), baseline_success=baseline_success)
    # Local fallback: same core counts as transfer.summarize_outcomes.
    rows = [o for o in (outcomes or []) if isinstance(o, dict)]
    helped = sum(
        1 for o in rows if bool(o.get("helped", o.get("success", False)))
    )
    severe = sum(1 for o in rows if bool(o.get("severe", False)))
    return {
        "reuse_count": len(rows),
        "success_count": helped,
        "failure_count": len(rows) - helped,
        "success_rate": (helped / len(rows)) if rows else 0.0,
        "negative_transfer_count": len(rows) - helped,
        "severe_regressions": severe,
    }


class PatchMemory:
    """PatchStore-backed executable memory for one baseline run."""

    def __init__(self, directory):
        cls = _get_patch_store_cls()
        self.directory = os.path.abspath(directory)
        os.makedirs(self.directory, exist_ok=True)
        self.store = cls(self.directory)

    # -- Phase 1: persist successes ----------------------------------
    def add_success(self, task, check_index, check_input, output):
        """Persist one verified-successful check as a patch."""
        task_id = task.get("id", "?")
        category = task.get("category", "")
        candidate = {
            "prompt": task.get("prompt", ""),
            "input": check_input,
            "output": output,
            "check": int(check_index),
        }
        tags = [t for t in (category, "family-a", task_id) if t]
        return self.store.save_patch(
            task_id, candidate, task_family="A", tags=tags
        )

    # -- Phase 2: retrieve + reuse -----------------------------------
    def retrieve(self, task, limit=2):
        """Live patches for a related (same-category) Phase 2 task.

        Equivalent-transform tasks additionally prefer patches from
        their ``derived_from`` base exposure task when present.
        """
        category = task.get("category", "")
        patches = self.store.find_for_task(
            task_family="A", tags=[category] if category else None
        )
        base = task.get("derived_from")
        if base:
            patches = sorted(
                patches,
                key=lambda p: (p.get("task_id") != base, p.get("created_at", 0.0)),
            )
        if limit is not None and limit >= 0:
            patches = patches[:limit]
        return patches

    def format_block(self, patches, max_chars_per_example=320):
        """Format retrieved patches as a tiny prompt-injection block.

        Empty retrieval yields "" (caller then solves from scratch).
        """
        if not patches:
            return ""
        lines = ["Retrieved procedures from solved tasks (adapt to this input):"]
        for i, patch in enumerate(patches, 1):
            candidate = patch.get("candidate", {}) or {}
            if not isinstance(candidate, dict):
                candidate = {"output": candidate}
            text = "Procedure %d (%s, %s):\ninput: %s\noutput: %s" % (
                i,
                patch.get("task_id", "?"),
                ",".join(patch.get("tags", [])[:2]),
                candidate.get("input", ""),
                candidate.get("output", ""),
            )
            if len(text) > max_chars_per_example:
                text = text[: max_chars_per_example - 1] + "…"
            lines.append(text)
        return "\n".join(lines) + "\n"

    def record_reuse(self, patch, task, helped, **extra):
        """Append one reuse outcome for a retrieved patch."""
        fields = {
            "held_out": task.get("split") == "transfer",
            "split": task.get("split"),
            "category": task.get("category"),
            "success": bool(helped),
        }
        fields.update(extra)
        return self.store.record_reuse(
            patch.get("patch_id"), task.get("id"), helped=bool(helped), **fields
        )

    # -- reuse-without-calls fast path (matched-rerun experiment) ---
    # Additive: existing retrieve/format_block/record_reuse behavior is
    # unchanged. On a high-confidence hit the caller reuses the recorded
    # output with ZERO model calls and records the hit separately from
    # assisted (model-call) checks.
    def reuse_history(self):
        """Per-patch helped/hurt counts from the store's reuse log.

        Returns ``{patch_id: {"helped": n, "hurt": m}}``. Callers
        snapshot this BEFORE the run (frozen confidence signal) so rows
        recorded during the run do not move later decisions.
        """
        history = {}
        for row in self.get_reuses():
            if not isinstance(row, dict):
                continue
            patch_id = row.get("patch_id")
            if not patch_id:
                continue
            slot = history.setdefault(patch_id, {"helped": 0, "hurt": 0})
            if bool(row.get("helped", row.get("success", False))):
                slot["helped"] += 1
            else:
                slot["hurt"] += 1
        return history

    def find_fast_path(self, task, history=None):
        """High-confidence patch for zero-call reuse, or None.

        Hit rule: same task family, exact category-tag match, at least
        one helped reuse on record, and zero hurt reuses. Adversarial
        tasks are NEVER fast-pathed (always returns None). ``history``
        is a :meth:`reuse_history` snapshot; when omitted it is built
        from the live store.
        """
        if (task.get("split") or "") == "adversarial":
            return None
        if history is None:
            history = self.reuse_history()
        for patch in self.retrieve(task, limit=2):
            if patch.get("task_family") != "A":
                continue
            category = task.get("category", "")
            if category and category not in (patch.get("tags") or []):
                continue
            slot = history.get(patch.get("patch_id"), {"helped": 0, "hurt": 0})
            if slot["helped"] >= 1 and slot["hurt"] == 0:
                return patch
        return None

    def fast_path_output(self, patch):
        """Recorded output text for a fast-path patch (zero-call reuse)."""
        candidate = patch.get("candidate", {}) if isinstance(patch, dict) else {}
        if not isinstance(candidate, dict):
            return candidate if isinstance(candidate, str) else ""
        output = candidate.get("output", "")
        return output if isinstance(output, str) else str(output)

    # -- library metrics ----------------------------------------------
    def capability_count(self):
        """Number of live (unexpired) patches in the library."""
        return len(self.store.list_patches())

    def get_reuses(self):
        return self.store.get_reuses()
