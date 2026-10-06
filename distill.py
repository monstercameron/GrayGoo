"""Distill executable capabilities from exposure tasks (synthesis learning).

Pipeline per exposure task (session goal: no more hand-written
capabilities)::

    Qwen synthesis (solve/applies source + types + signature words)
    -> AST gate (pure-stdlib subset) + rehearsal (subprocess trial)
    -> verification (all exposure checks pass through execaps.compare)
    -> registration (ExecCapability built from source, saved to JSON)

then consolidation: behavioral set-cover over all exposure tasks plus
a cross-task harm check (claims-but-fails elsewhere quarantines).

The model writes exactly TWO functions plus two metadata comments::

    # TYPES: in=<type> out=<type>
    # SIG: <six lowercase words>
    def solve(text): ...
    def applies(text): ...

Trust boundary: untrusted model code NEVER runs in-process before
verification -- rehearsal executes it in a subprocess with a timeout.
Only gated + verified code loads into the driver registry (via
:func:`load_learned`, which re-gates on load).

CLI (live; costs tokens)::

    uv run python distill.py --tasks benchmarks/family-w --split exposure
"""

from __future__ import annotations

import ast
import io
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import execaps  # noqa: E402

ALLOWED_IMPORTS = frozenset({"json", "re", "csv", "io", "math"})

_FORBIDDEN_NAMES = frozenset({
    "eval", "exec", "open", "compile", "__import__", "globals",
    "locals", "vars", "dir", "input", "breakpoint", "exit", "quit",
    "help", "memoryview", "bytearray",
})

_REHEARSAL_TIMEOUT_S = 20

# Builtins available to learned code (both rehearsal and load): pure
# data handling only -- no open/eval/import machinery beyond the
# allowlisted-module importer.
_SAFE_BUILTINS = {
    "len": len, "str": str, "int": int, "float": float, "bool": bool,
    "list": list, "dict": dict, "set": set, "tuple": tuple,
    "range": range, "enumerate": enumerate, "zip": zip,
    "sorted": sorted, "sum": sum, "min": min, "max": max, "abs": abs,
    "all": all, "any": any, "isinstance": isinstance, "type": type,
    "repr": repr, "round": round, "iter": iter, "next": next,
    "ValueError": ValueError, "TypeError": TypeError,
    "KeyError": KeyError, "IndexError": IndexError,
    "AttributeError": AttributeError, "StopIteration": StopIteration,
    "Exception": Exception,
}


class DistillError(Exception):
    """Any synthesis/gate/rehearsal/verification failure."""


def type_vocabulary():
    """Shared type vocabulary (from seeds + glue specs)."""
    vocab = set()
    for cap in execaps.seed_capabilities():
        vocab.update(cap.descriptor.input_types)
        vocab.update(cap.descriptor.output_types)
    for _name, (glue_in, glue_out, _fn) in execaps.GLUE_SPECS.items():
        vocab.update(glue_in)
        vocab.update(glue_out)
    return frozenset(vocab)


def gate_source(src, *, what="candidate"):
    """Reject model source outside the pure-transform subset.

    Allows: two required functions (``solve``/``applies``, one arg
    each) plus ``_``-prefixed helpers, stdlib imports from
    :data:`ALLOWED_IMPORTS`, and ordinary data-flow statements.
    Rejects: classes, async, globals, dunder/private attribute
    access, dangerous builtins, star imports, and anything that
    fails to parse. Raises :class:`DistillError` on violation.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        raise DistillError("%s does not parse: %s" % (what, exc))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in ALLOWED_IMPORTS:
                    raise DistillError(
                        "%s imports forbidden module %r" % (what,
                                                             alias.name))
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root not in ALLOWED_IMPORTS:
                raise DistillError(
                    "%s imports forbidden module %r" % (what,
                                                         node.module))
            if any(a.name == "*" for a in node.names):
                raise DistillError("%s uses star import" % what)
        elif isinstance(node, (ast.ClassDef, ast.AsyncFunctionDef,
                               ast.AsyncFor, ast.AsyncWith,
                               ast.Global, ast.Nonlocal)):
            raise DistillError("%s uses forbidden %s" % (
                what, type(node).__name__))
        elif isinstance(node, ast.Name):
            if node.id in _FORBIDDEN_NAMES or (
                    node.id.startswith("__") and node.id.endswith("__")):
                raise DistillError("%s uses forbidden name %r" % (
                    what, node.id))
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise DistillError("%s touches private attribute %r" % (
                    what, node.attr))
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _FORBIDDEN_NAMES:
                raise DistillError("%s calls forbidden %r" % (
                    what, func.id))
    top = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    names = {fn.name for fn in top}
    if "solve" not in names or "applies" not in names:
        raise DistillError(
            "%s must define solve(text) and applies(text)" % what)
    for fn in top:
        if fn.name in ("solve", "applies"):
            args = fn.args
            if len(args.args) != 1 or args.vararg or args.kwarg:
                raise DistillError(
                    "%s.%s must take exactly one arg" % (what, fn.name))
        elif not fn.name.startswith("_"):
            raise DistillError(
                "%s helper %r must be _-prefixed" % (what, fn.name))


_HARNESS_SRC = r'''
import io
import json
import sys

_ALLOWED = {"json": json}
import re as _re
import csv as _csv
import math as _math
import io as _io
_ALLOWED.update({"re": _re, "csv": _csv, "math": _math, "io": _io})

_SAFE = {
    "len": len, "str": str, "int": int, "float": float, "bool": bool,
    "list": list, "dict": dict, "set": set, "tuple": tuple,
    "range": range, "enumerate": enumerate, "zip": zip,
    "sorted": sorted, "sum": sum, "min": min, "max": max, "abs": abs,
    "all": all, "any": any, "isinstance": isinstance, "type": type,
    "repr": repr, "round": round, "iter": iter, "next": next,
    "ValueError": ValueError, "TypeError": TypeError,
    "KeyError": KeyError, "IndexError": IndexError,
    "AttributeError": AttributeError, "StopIteration": StopIteration,
    "Exception": Exception,
}


def _safe_import(name, *args, **kwargs):
    root = name.split(".")[0]
    if root not in _ALLOWED:
        raise ImportError("forbidden import %r" % (name,))
    return _ALLOWED[root]


_SAFE["__import__"] = _safe_import


def main():
    payload = json.load(sys.stdin)
    namespace = {"__builtins__": dict(_SAFE)}
    namespace.update(_ALLOWED)
    # Candidate print() must not corrupt the JSON protocol.
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        exec(compile(payload["code"], "<candidate>", "exec"), namespace)
    except Exception as exc:  # noqa: BLE001 - report, don't crash
        sys.stdout = old
        json.dump({"ok": False, "error": "exec: %r" % (exc,)},
                  sys.stdout)
        return
    finally:
        sys.stdout = old
    try:
        solve = namespace["solve"]
        applies = namespace["applies"]
    except Exception as exc:  # noqa: BLE001 - report, don't crash
        json.dump({"ok": False, "error": "shape: %r" % (exc,)},
                  sys.stdout)
        return
    outputs = []
    flags = []
    errors = {}
    for i, text in enumerate(payload["inputs"]):
        try:
            flag = bool(applies(text))
        except Exception as exc:  # noqa: BLE001 - per-index report
            errors[i] = "applies: %r" % (exc,)
            flags.append(False)
            outputs.append(None)
            continue
        flags.append(flag)
        if not flag:
            outputs.append(None)
            continue
        try:
            outputs.append(solve(text))
        except Exception as exc:  # noqa: BLE001 - per-index report
            errors[i] = "solve: %r" % (exc,)
            outputs.append(None)
    json.dump({"ok": True, "outputs": outputs, "applies": flags,
               "errors": errors},
              sys.stdout)


main()
'''


def rehearse(code, inputs, timeout=_REHEARSAL_TIMEOUT_S):
    """Trial model code in a subprocess; return (outputs, flags, errors).

    ``outputs[i]`` is the solve() result (None when applies() is
    False); ``errors`` maps str(index) -> message for per-index
    crashes. Raises :class:`DistillError` only on timeout, protocol
    breach, or whole-batch failure. Inputs must be strings.
    """
    payload = json.dumps({"code": code, "inputs": list(inputs)})
    with tempfile.TemporaryDirectory(prefix="goo-rehearse-") as tmp:
        harness = Path(tmp) / "harness.py"
        harness.write_text(_HARNESS_SRC, encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, str(harness)], input=payload,
                capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise DistillError(
                "rehearsal timed out after %ss (infinite loop?)" %
                timeout)
    if proc.returncode != 0:
        raise DistillError("rehearsal harness failed: %s" %
                           (proc.stderr or proc.stdout)[-500:])
    try:
        result = json.loads(proc.stdout)
    except ValueError:
        raise DistillError("rehearsal protocol breach: %r" %
                           proc.stdout[-300:])
    if not result.get("ok"):
        raise DistillError("rehearsal error: %s" %
                           result.get("error", "?"))
    errors = {int(k): v for k, v in
              result.get("errors", {}).items()}
    return result["outputs"], result["applies"], errors


_SYNTH_SYSTEM = (
    "You distill reusable text-processing capabilities from task "
    "examples. Output exactly one ```python block and nothing else."
)

_SYNTH_TEMPLATE = """\
Task {task_id}: {prompt}

Examples (input -> expected output, {compare} comparison):
{examples}

Allowed input/output types (pick exactly one each): {types}

Write a GENERAL solution as two pure functions plus two metadata comments:

# TYPES: in=<input-type> out=<output-type>
# SIG: <six distinct lowercase words describing this procedure>
def solve(text):
    \"\"\"Transform one check input into the expected output string.\"\"\"
    ...
def applies(text):
    \"\"\"True ONLY for inputs this procedure genuinely handles.\"\"\"
    ...

Rules:
- Solve the GENERAL procedure shown by the task, not these examples.
- Allowed imports: json, re, csv, io, math. No I/O, no network, no
  classes, no globals, no dunder access, no print(). Extra helpers
  must be _-prefixed. Both functions take exactly one string arg.
  Available builtins: len str int float bool list dict set tuple
  range enumerate zip sorted sum min max abs all any isinstance type
  repr round iter next plus ValueError/TypeError/KeyError.
- applies() must return False for inputs outside the procedure
  (wrong shape, unparsable, unsupported variants) AND for inputs that
  do not need the procedure at all (no duplicates for a dedup, every
  value already clean for a normalizer, no cursor pages for a pager).
  Firing where a DIFFERENT procedure is needed is the worst failure:
  when in doubt, abstain. applies() must never raise -- guard all
  parsing with try/except and return False.
- SIG words must be distinct lowercase alphanumerics (2+ chars) naming
  the procedure steps (nouns/verbs from the task, not "input/output").
- TYPES must come from the allowed list above.
- csv.writer defaults to \r\n line endings: pass lineterminator="\n"
  when the expected output uses plain newlines.
{feedback}\
"""

_CODE_FENCE = re.compile(r"```python\s*(.*?)```", re.DOTALL)
_TYPES_LINE = re.compile(r"^#\s*TYPES:\s*in=([a-z0-9-]+)\s+out=([a-z0-9-]+)",
                         re.MULTILINE)
_SIG_LINE = re.compile(r"^#\s*SIG:\s*([A-Za-z0-9 ]+)", re.MULTILINE)
_SIG_WORD = re.compile(r"^[a-z0-9]{2,}$")


def parse_candidate(text, vocab):
    """Split model output into (in_type, out_type, sig, code).

    Raises :class:`DistillError` on missing fences, malformed
    metadata, unknown types, or bad signature words.
    """
    match = _CODE_FENCE.search(text)
    code = match.group(1) if match else text
    if "def solve" not in code or "def applies" not in code:
        raise DistillError("no ```python block with solve/applies")
    types = _TYPES_LINE.search(code)
    if not types:
        raise DistillError("missing '# TYPES: in=<t> out=<t>' line")
    in_type, out_type = types.group(1), types.group(2)
    if in_type not in vocab or out_type not in vocab:
        raise DistillError("types %r/%r outside vocabulary" % (
            in_type, out_type))
    sig = _SIG_LINE.search(code)
    if not sig:
        raise DistillError("missing '# SIG: <six words>' line")
    raw = sig.group(1)
    words = tuple(raw.lower().split())
    if (len(words) != 6 or len(set(words)) != 6
            or not all(_SIG_WORD.match(w) for w in words)):
        if len(words) != 6:
            hint = ("you wrote %d words; %s" % (
                len(words),
                "delete %d word%s" % (len(words) - 6,
                                      "" if len(words) == 7 else "s")
                if len(words) > 6
                else "add %d more word%s" % (6 - len(words),
                                             "" if len(words) == 5
                                             else "s")))
        elif len(set(words)) != 6:
            hint = "words must be distinct (no repeats)"
        else:
            hint = ("every word must match [a-z0-9]{2,} "
                    "(no hyphens/capitals/single letters)")
        raise DistillError(
            "SIG must be exactly six distinct words, got %r: %s "
            "(e.g. 'paginate pages cursor items collect next')" % (
                raw, hint))
    return in_type, out_type, words, code


def _examples_block(task, max_chars=600):
    lines = []
    for check in task.get("checks", []):
        lines.append("IN: %s" % check["input"][:max_chars])
        lines.append("OUT: %s" % check["expected"][:max_chars])
    return "\n".join(lines)


def failure_feedback(task, failures):
    """Repair-loop feedback from verification failures."""
    lines = ["\nYour previous attempt FAILED. Fix it:"]
    for index, kind, detail in failures:
        lines.append("- check %d: %s: %s" % (index, kind, detail))
    lines.append("Return the full corrected ```python block.")
    return "\n".join(lines)


def sig_prompt_overlap(sig, task):
    """Fraction of SIG words present in the task prompt (runtime gate).

    Delegates to :func:`execaps.sig_overlap` so the synthesis gate
    scores exactly what the runtime retrieval gates score
    (inflection-tolerant stem matching).
    """
    words = set(re.findall(r"[a-z0-9]+",
                           str(task.get("prompt", "")).lower()))
    return execaps.sig_overlap(tuple(sig), words)


def synthesize_one(task, generate, vocab=None, max_attempts=3,
                   negatives=None):
    """Synthesize + verify one capability for an exposure task.

    ``generate(prompt)`` returns ``(text, usage_dict)``. ``negatives``
    is a list of OTHER exposure task dicts: firing (full runtime
    gate: category + prompt overlap + applies) on a negative check
    but solving it wrong is an over-application failure (teaches
    safe abstention). Retries with failure feedback up to
    ``max_attempts``. Returns ``(candidate_dict, attempts,
    usage_list)``; raises :class:`DistillError` when all attempts fail.
    """
    vocab = vocab or type_vocabulary()
    feedback = ""
    usage_all = []
    last_error = "no attempt"
    # Escalating temperature breaks deterministic failure loops: the
    # same prompt + same temperature reproduces the same bad SIG.
    temps = (0.2, 0.5, 0.7, 0.9)
    for _attempt in range(max_attempts):
        prompt = _SYNTH_TEMPLATE.format(
            task_id=task.get("id", "?"),
            prompt=task.get("prompt", ""),
            compare=task["checks"][0]["compare"],
            examples=_examples_block(task),
            types=", ".join(sorted(vocab)),
            feedback=feedback)
        temperature = temps[min(_attempt, len(temps) - 1)]
        text, usage = generate(prompt, temperature=temperature)
        usage_all.append(usage)
        try:
            in_type, out_type, sig, code = parse_candidate(text, vocab)
            gate_source(code, what=task["id"])
            overlap = sig_prompt_overlap(sig, task)
            if overlap < 0.5:
                words = set(re.findall(
                    r"[a-z0-9]+", str(task.get("prompt", "")).lower()))
                hits = execaps.sig_word_hits(sig, words)
                missing = sorted(w for w in sig if hits[w] is None)
                raise DistillError(
                    "SIG prompt overlap %.2f < 0.50: replace %s with "
                    "words copied from the task prompt (at least 3 of "
                    "6 must appear there; the runtime fires only "
                    "above this bar)" % (overlap, missing))

            def _gate(neg_task, _check, flag, _sig=sig,
                      _cat=task.get("category")):
                return bool(
                    flag and neg_task.get("category") == _cat
                    and sig_prompt_overlap(_sig, neg_task) >= 0.5)

            failures = verify_code(code, task, negatives=negatives,
                                   gate=_gate if negatives else None)
            if failures:
                feedback = failure_feedback(task, failures)
                last_error = "; ".join(
                    "%s %s" % (k, d) for _, k, d in failures)
                continue
            return ({"task_id": task["id"], "in_type": in_type,
                     "out_type": out_type, "sig": list(sig),
                     "code": code},
                    _attempt + 1, usage_all)
        except DistillError as exc:
            feedback = ("\nYour previous attempt FAILED: %s\nReturn the "
                        "full corrected ```python block." % exc)
            last_error = str(exc)
    raise DistillError("%s: %d attempts failed (%s)" % (
        task["id"], max_attempts, last_error))


def verify_code(code, task, negatives=None, gate=None):
    """Verify gated code against a task; return failure list.

    Each failure is ``(check_index, kind, detail)``. Empty list means
    verified: ``applies`` true on every check input and ``solve``
    matching every expected output through :func:`execaps.compare`.
    ``negatives`` (full task dicts from other tasks) add
    over-application failures: firing on a foreign check but solving
    it wrong means the precondition is too lax. Firing and solving
    correctly is bonus coverage, not a failure. ``gate(task, check,
    flag)`` decides firing; default is the raw flag. Callers pass
    the full runtime gate (category + prompt + flag) so same-shape
    siblings that the prompt gate disambiguates do not count.
    """
    inputs = [c["input"] for c in task.get("checks", [])]
    try:
        outputs, flags, errors = rehearse(code, inputs)
    except DistillError as exc:
        return [(0, "rehearsal", str(exc))]
    failures = []
    for j, check in enumerate(task.get("checks", [])):
        if j in errors:
            failures.append((j, "raised", errors[j][:200]))
            continue
        if not flags[j]:
            failures.append((j, "abstains",
                             "applies() False on a task input"))
            continue
        if not isinstance(outputs[j], str):
            failures.append((j, "type",
                             "solve() returned non-string"))
            continue
        if not execaps.compare(check["expected"], outputs[j],
                               check["compare"]):
            failures.append((j, "mismatch",
                             "expected %r got %r" % (
                                 check["expected"][:120],
                                 outputs[j][:120])))
    if failures or not negatives:
        return failures
    neg_inputs = []
    neg_meta = []
    for neg_task in negatives:
        for j, check in enumerate(neg_task.get("checks", [])):
            neg_inputs.append(check["input"])
            neg_meta.append((neg_task, j, check))
    if not neg_inputs:
        return failures
    try:
        neg_outputs, neg_flags, neg_errors = rehearse(code, neg_inputs)
    except DistillError as exc:
        return [(0, "rehearsal-negatives", str(exc))]
    for i, (flag, out, (neg_task, j, check)) in enumerate(zip(
            neg_flags, neg_outputs, neg_meta)):
        fires = flag if gate is None else gate(neg_task, check, flag)
        if not fires:
            continue
        task_id = neg_task["id"]
        if i in neg_errors:
            failures.append((j, "over-applies",
                             "fires on %s check %d but crashes: %s; "
                             "tighten applies()" % (
                                 task_id, j, neg_errors[i][:120])))
        elif not isinstance(out, str) or not execaps.compare(
                check["expected"], out, check["compare"]):
            failures.append((j, "over-applies",
                             "fires on %s check %d but solves it "
                             "wrongly; tighten applies()" % (task_id, j)))
    return failures


def _safe_import(name, *args, **kwargs):
    root = name.split(".")[0]
    if root not in ALLOWED_IMPORTS:
        raise ImportError("forbidden import %r" % (name,))
    return __import__(name, *args, **kwargs)


def _exec_trusted(code, what):
    """Exec gated code in a restricted namespace; return (solve, applies).

    Only for code that passed :func:`gate_source` AND verification.
    Untrusted candidates always go through :func:`rehearse` first.
    """
    gate_source(code, what=what)
    builtins = dict(_SAFE_BUILTINS)
    builtins["__import__"] = _safe_import
    namespace = {"__builtins__": builtins}
    exec(compile(code, "<%s>" % what, "exec"), namespace)  # noqa: S102
    return namespace["solve"], namespace["applies"]


def learned_cap_id(source_task_id):
    """Deterministic learned id, e.g. W-EXP-01 -> lc-w-exp-01."""
    slug = re.sub(r"[^a-z0-9]+", "-", source_task_id.lower()).strip("-")
    return "lc-" + slug


def to_exec_capability(candidate, task):
    """Build an ExecCapability from a verified candidate + source task."""
    solve, applies = _exec_trusted(candidate["code"], candidate["task_id"])
    intent = "distilled from %s: %s" % (
        task["id"], str(task.get("prompt", ""))[:80])
    cap = execaps.ExecCapability(
        learned_cap_id(task["id"]), intent, task.get("category"),
        (candidate["in_type"],), (candidate["out_type"],),
        solve, applies, candidate["sig"], task["id"])
    cap.learned_source = candidate["code"]
    return cap


def coverage_and_harm(cap, tasks):
    """Behavioral coverage vs harm of one capability over tasks.

    Covers a task when it applies (full gate) to EVERY check input
    and solves every check. Harms a task when it applies to ANY
    check but fails to solve the task. Returns (covered_ids,
    harmed_ids). Pure measurement over trusted checks.
    """
    covered = set()
    harmed = set()
    for task in tasks:
        fired = []
        solved = []
        for check in task.get("checks", []):
            try:
                applies = cap.applies_to(task, check["input"])
            except Exception:
                applies = False
            fired.append(bool(applies))
            if applies:
                try:
                    out = cap.execute(check["input"])
                except Exception:
                    solved.append(False)
                    continue
                solved.append(bool(execaps.compare(
                    check["expected"], out, check["compare"])))
            else:
                solved.append(None)
        if fired and all(fired) and all(s is True for s in solved):
            covered.add(task["id"])
        elif any(fired) and not all(s is not False for s in solved):
            harmed.add(task["id"])
    return covered, harmed


def consolidate(caps, tasks):
    """Greedy set-cover over covered tasks; quarantine harmers.

    Drops candidates that harm any task (severe negative transfer
    vetoes promotion, directive §9), then keeps the smallest subset
    covering every coverable task -- the generalization mechanism:
    one broad capability absorbs every task it behaviorally covers.
    Returns ``(kept, dropped_redundant, quarantined, uncovered)``.
    """
    cover = {}
    harm = {}
    for cap in caps:
        covered, harmed = coverage_and_harm(cap, tasks)
        if harmed:
            harm[cap.id] = sorted(harmed)
        else:
            cover[cap.id] = covered
    safe = {cid: cov for cid, cov in cover.items()}
    uncovered = set()
    for cov in safe.values():
        uncovered.update(cov)
    kept = []
    remaining = set(uncovered)
    candidates = sorted(safe.items(), key=lambda kv: kv[0])
    while remaining:
        best = None
        best_gain = 0
        for cid, cov in candidates:
            gain = len(set(cov) & remaining)
            if gain > best_gain:
                best = cid
                best_gain = gain
        if best is None or best_gain == 0:
            break
        kept.append(best)
        remaining -= set(safe[best])
    by_id = {c.id: c for c in caps}
    kept_caps = [by_id[cid] for cid in kept]
    dropped = sorted(set(safe) - set(kept))
    uncoverable = sorted({t["id"] for t in tasks} - set(uncovered))
    return kept_caps, dropped, harm, uncoverable


LEARNED_FORMAT_VERSION = 1


def save_learned(path, caps, run_meta):
    """Persist learned capabilities (source form) to ``path`` JSON."""
    payload = {
        "version": LEARNED_FORMAT_VERSION,
        "run": dict(run_meta),
        "caps": [{
            "id": cap.id,
            "intent": cap.descriptor.intent,
            "category": cap.category,
            "input_types": list(cap.descriptor.input_types),
            "output_types": list(cap.descriptor.output_types),
            "prompt_keywords": sorted(cap.prompt_keywords),
            "source_task": cap.source_task,
            "code": cap.learned_source,
        } for cap in caps],
    }
    Path(path).write_text(json.dumps(payload, indent=2) + "\n",
                          encoding="utf-8")
    return payload


def load_learned(path):
    """Load learned capabilities from JSON; re-gate every source.

    Returns ``(caps, run_meta)``. Raises :class:`DistillError` on
    version mismatch or any gate failure -- loaded code is only as
    trusted as its gate.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("version") != LEARNED_FORMAT_VERSION:
        raise DistillError("learned format %r != %r" % (
            payload.get("version"), LEARNED_FORMAT_VERSION))
    caps = []
    for entry in payload.get("caps", []):
        solve, applies = _exec_trusted(entry["code"], entry["id"])
        cap = execaps.ExecCapability(
            entry["id"], entry["intent"], entry["category"],
            tuple(entry["input_types"]), tuple(entry["output_types"]),
            solve, applies, entry["prompt_keywords"],
            entry["source_task"])
        cap.learned_source = entry["code"]
        caps.append(cap)
    return caps, payload.get("run", {})


def cerebras_generate(prompt, max_tokens=2048, temperature=0.2):
    """Live Qwen call returning (text, usage)."""
    import cerebras_client  # deferred: tests must not need a key
    result = cerebras_client.generate(
        prompt, system=_SYNTH_SYSTEM, max_tokens=max_tokens,
        temperature=temperature, reasoning_effort="none")
    usage = {"input_tokens": result.get("input_tokens") or 0,
             "output_tokens": result.get("output_tokens") or 0,
             "latency_ms": result.get("latency_ms") or 0.0,
             "cost_usd": result.get("cost_usd") or 0.0,
             "model": result.get("model")}
    return result["text"] or "", usage


def fake_generate(responses):
    """Deterministic generate() from {task_id: [texts]} (tests)."""
    counts = {}

    def run(prompt, temperature=None):
        _ = temperature
        for task_id, texts in responses.items():
            if task_id in prompt:
                i = counts.get(task_id, 0)
                counts[task_id] = i + 1
                return texts[min(i, len(texts) - 1)], {
                    "input_tokens": 0, "output_tokens": 0,
                    "latency_ms": 0.0, "cost_usd": 0.0, "model": "fake"}
        raise DistillError("fake has no response for prompt: %r" %
                           prompt[:80])

    run.counts = counts
    return run


def distill_task(task, generate, vocab=None, max_attempts=3,
                 negatives=None):
    """Full per-task rung: synthesize -> register. Returns record."""
    candidate, attempts, usage = synthesize_one(
        task, generate, vocab=vocab, max_attempts=max_attempts,
        negatives=negatives)
    cap = to_exec_capability(candidate, task)
    return {"cap": cap, "candidate": candidate, "attempts": attempts,
            "usage": usage}


def distill_run(tasks, generate, out_dir, vocab=None, max_attempts=3,
                run_meta=None):
    """Distill + consolidate over exposure tasks; write artifacts.

    Returns the report dict. ``learned.json`` holds kept capabilities;
    ``report.json`` holds per-task attempts/usage plus consolidation.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    vocab = vocab or type_vocabulary()
    records = []
    failures = []
    for task in tasks:
        negatives = [other for other in tasks
                     if other["id"] != task["id"]]
        try:
            record = distill_task(task, generate, vocab=vocab,
                                  max_attempts=max_attempts,
                                  negatives=negatives)
            record["task_id"] = task["id"]
            records.append(record)
        except DistillError as exc:
            failures.append({"task_id": task["id"],
                             "error": str(exc)})
    caps = [r["cap"] for r in records]
    kept, dropped, quarantined, uncoverable = consolidate(caps, tasks)
    kept_ids = {c.id for c in kept}
    report = {
        "run": dict(run_meta or {}),
        "targets": [t["id"] for t in tasks],
        "synthesized": sorted(r["cap"].id for r in records),
        "failed": failures,
        "kept": sorted(kept_ids),
        "dropped_redundant": dropped,
        "quarantined": quarantined,
        "uncoverable": uncoverable,
        "per_task": [{
            "task_id": r["task_id"], "cap_id": r["cap"].id,
            "attempts": r["attempts"],
            "input_tokens": sum(u.get("input_tokens", 0)
                                for u in r["usage"]),
            "output_tokens": sum(u.get("output_tokens", 0)
                                 for u in r["usage"]),
            "cost_usd": round(sum(u.get("cost_usd", 0.0)
                                  for u in r["usage"]), 6),
        } for r in records],
    }
    save_learned(out_dir / "learned.json", kept, report["run"])
    (out_dir / "report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    """CLI: distill exposure tasks into learned.json + report.json."""
    import argparse
    parser = argparse.ArgumentParser(description="Distill capabilities")
    parser.add_argument("--tasks", action="append", default=[],
                        help="task dirs (repeatable)")
    parser.add_argument("--split", default="exposure")
    parser.add_argument("--ids", default=None,
                        help="comma-separated task ids to distill")
    parser.add_argument("--adapter", choices=("cerebras", "fake"),
                        default="cerebras")
    parser.add_argument("--fake", default=None,
                        help="JSON {task_id: [response texts]} for --adapter fake")
    parser.add_argument("--artifacts", default="artifacts/distill")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--include-synthetic-table", action="store_true",
                        help="also distill the table-csv inverse contract")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).resolve().parent
                           / "benchmarks"))
    import runner  # noqa: E402

    dirs = args.tasks or ["benchmarks/family-w", "benchmarks/family-a"]
    tasks = []
    for dirname in dirs:
        tasks.extend(runner.load_tasks(Path(dirname)))
    tasks = [t for t in tasks if t.get("split") == args.split]
    if args.ids:
        wanted = set(args.ids.split(","))
        tasks = [t for t in tasks if t["id"] in wanted]
    if args.include_synthetic_table:
        tasks.append(execaps.TABLE_CSV_TASK)
    tasks = sorted(tasks, key=lambda t: t["id"])
    if not tasks:
        raise SystemExit("no distill targets selected")
    if args.adapter == "fake":
        if not args.fake:
            raise SystemExit("--adapter fake needs --fake FILE")
        responses = json.loads(Path(args.fake).read_text(
            encoding="utf-8"))
        # Prompts carry "Task <id>:" so the fake routes per task.
        generate = fake_generate(responses)
    else:
        generate = cerebras_generate
    report = distill_run(
        tasks, generate, args.artifacts, max_attempts=args.max_attempts,
        run_meta={"adapter": args.adapter, "split": args.split})
    print("distilled %d/%d, kept %d, dropped %s, quarantined %s" % (
        len(report["synthesized"]), len(report["targets"]),
        len(report["kept"]), report["dropped_redundant"],
        sorted(report["quarantined"])), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
