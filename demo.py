"""First end-to-end self-learning demo (plan.md section 92, MVP demo section 80).

Wires REAL components in sequence per task::

    retrieve.find_capabilities -> context.compile_context -> cerebras
    generate_candidate -> s_expr validate -> risk.classify ->
    pipeline.run_candidate (worker_fn=workers.run_lisp) -> repair_loop
    (max_repairs=1) -> patches.save_patch -> transfer.record_reuse

Three related Family A CSV exposure tasks run in sequence
(A-EXP-05 -> A-EXP-07 -> A-EXP-08); tasks 2-3 first attempt direct
reuse of the task-1 patch (zero model calls) and adapt it on failure.

Usage:
    uv run python demo.py                 # live run (<= 35 Cerebras calls)
    uv run python demo.py --smoke         # offline wiring check, 0 live calls
    uv run python demo.py --out NAME      # live run into artifacts/demo-e2e/NAME
    uv run python demo.py --only A-EXP-05 # single-task live run

All run outputs go to artifacts/demo-e2e/. Never prints secrets.
"""

import json
import os
import sys
import time

import cerebras_client
import context as context_mod
import patches
import pipeline
import repair
import retrieve
import risk
import s_expr
import transfer
import workers

ROOT = os.path.dirname(os.path.abspath(__file__))
ARTIFACTS = os.path.join(ROOT, "artifacts", "demo-e2e")
EXPOSURE_PATH = os.path.join(ROOT, "benchmarks", "family-a", "exposure.json")

TASK_ORDER = ["A-EXP-05", "A-EXP-07", "A-EXP-08"]
FAMILY = "csv"
INPUT_TYPES = ["csv-text"]
OUTPUT_TYPES = ["json-text"]
CALL_BUDGET = 35
GEN_MAX_TOKENS = 2048
GEN_TEMPERATURE = 0.0
GEN_REASONING = "none"
WORKER_TIMEOUT_S = 30.0
CTX_BUDGET_FRESH = 500
CTX_BUDGET_ADAPT = 1500

FN_NAME = "solve-csv"

# Test-harness prelude (fixed demo infrastructure, not the solution):
# compare JSON up to whitespace OUTSIDE string literals so the gate is
# robust to formatting while still exact on content, key order, escapes.
# NOTE: Lisp strings take literal chars only (no backslash escapes), so
# the strip set is spliced from real tab/CR/LF chars via chr().
_WS_STRIP = " " + chr(9) + chr(13) + chr(10)
NORMALIZE_PRELUDE = " ".join([
    "(defun gg-json-normalize (s)",
    "(with-output-to-string (out)",
    "(let ((in-str nil) (esc nil))",
    "(loop for ch across s do (cond",
    "(esc (write-char ch out) (setf esc nil))",
    "((and in-str (char= ch #\\\\)) (write-char ch out) (setf esc t))",
    "((char= ch #\\\") (write-char ch out) (setf in-str (not in-str)))",
    "((and (not in-str) (find ch " + '"%s"' % _WS_STRIP + ")) nil)",
    "(t (write-char ch out)))))))",
])

# Fixed JSON emitter (demo harness infrastructure, like the normalizer:
# constant across tasks, never learned or persisted as a patch/skill).
# The model's solve-csv parses CSV itself and returns
# (gg-emit-json header rows); the keyword :null renders as JSON null.
# NOTE: backslash-heavy literals are built from chr() parts, never typed
# as literal runs — literal runs miscount silently (see session history).
_BS = chr(92)
_Q = chr(34)
# Lisp source for the 2-char string value backslash+quote.
_LIT_ESC_QUOTE = _Q + _BS * 3 + _Q + _Q
# Lisp source for the 2-char string value quote+colon.
_LIT_COLON = _Q + _BS + _Q + ":" + _Q
EMIT_PRELUDE = " ".join([
    "(defun gg-json-escape-string (s)",
    "(with-output-to-string (out)",
    "(loop for ch across s do (cond",
    "((char= ch (code-char 34)) (write-string " + _LIT_ESC_QUOTE + " out))",
    "((char= ch #\\\\) (write-string \"\\\\\\\\\" out))",
    "((char= ch #\\Newline) (write-string \"\\\\n\" out))",
    "((char= ch #\\Return) (write-string \"\\\\r\" out))",
    "((char= ch #\\Tab) (write-string \"\\\\t\" out))",
    "(t (write-char ch out))))))",
    "(defun gg-emit-json (header rows)",
    "(with-output-to-string (out)",
    "(write-char #\\[ out)",
    "(loop for row in rows for i from 0 do",
    "(unless (zerop i) (write-char (code-char 44) out))",
    "(write-char #\\{ out)",
    "(loop for h in header for v in row for j from 0 do",
    "(unless (zerop j) (write-char (code-char 44) out))",
    "(write-char (code-char 34) out)",
    "(write-string (gg-json-escape-string h) out)",
    "(write-string " + _LIT_COLON + " out)",
    "(if (eq v :null) (write-string \"null\" out)",
    "(progn (write-char (code-char 34) out)",
    "(write-string (gg-json-escape-string v) out)",
    "(write-char (code-char 34) out))))",
    "(write-char #\\} out))",
    "(write-char #\\] out)))",
])

LOG_LINES = []


def log(text=""):
    print(text, flush=True)
    LOG_LINES.append(text)


def lisp_str(value):
    """Render a Python string as a Common Lisp string literal."""
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


def dumps_lisp(node):
    """Serialize a parsed s_expr node back to Lisp source text."""
    if isinstance(node, s_expr.SString):
        return lisp_str(str(node))
    if isinstance(node, str):
        return node
    if isinstance(node, bool):
        return "T" if node else "NIL"
    if isinstance(node, (int, float)):
        return repr(node)
    if isinstance(node, (list, tuple)):
        return "(%s)" % " ".join(dumps_lisp(x) for x in node)
    raise TypeError("cannot serialize %s" % type(node).__name__)


def dumps_body(definition):
    """Render a parsed :definition (a list of forms) as code text.

    ``s_expr.parse_candidate`` returns the definition as a list of
    forms — e.g. ``(:definition ((defun f ...)))`` yields
    ``[(defun-form)]``. Evaluating ``dumps_lisp`` of that list would
    wrap it in one paren level too many (``((defun ...))`` is an
    illegal function call), so splice the forms instead.
    """
    if not isinstance(definition, (list, tuple)) or not definition:
        raise ValueError("definition must be a non-empty list of forms")
    return "\n".join(dumps_lisp(form) for form in definition)


def load_tasks():
    with open(EXPOSURE_PATH, encoding="utf-8") as fh:
        all_tasks = {t["id"]: t for t in json.load(fh)}
    return [all_tasks[tid] for tid in TASK_ORDER]


def check_code(definition_lisp, check):
    # On mismatch the test returns (:mismatch got want) so the repair
    # counterexample carries actual-vs-expected evidence, not just NIL.
    assertion = (
        "(let ((got (gg-json-normalize (%s %s))) "
        "(want (gg-json-normalize %s))) "
        "(if (equal got want) T (list :mismatch got want)))"
        % (FN_NAME, lisp_str(check["input"]), lisp_str(check["expected"])))
    return "%s\n%s\n%s\n%s" % (NORMALIZE_PRELUDE, EMIT_PRELUDE,
                               definition_lisp, assertion)


def build_tests(task, prior_tasks, definition_lisp):
    """Direct checks for this task + regression checks from prior tasks."""
    direct = [{"code": check_code(definition_lisp, c), "expect": "T"}
              for c in task["checks"]]
    tests = {"direct": direct}
    reg = []
    for prior in prior_tasks:
        for c in prior["checks"]:
            reg.append({"code": check_code(definition_lisp, c), "expect": "T"})
    if reg:
        tests["regression"] = reg
    return tests


def worker_fn(code):
    return workers.run_lisp(code, timeout_s=WORKER_TIMEOUT_S)


def risk_fn(parsed):
    return risk.classify(parsed, {"effects": ["pure"]})


def first_failing_detail(evidence, stage):
    """One-line excerpt of the first failing item (minimal counterexample)."""
    stage_ev = (evidence or {}).get(stage)
    if not isinstance(stage_ev, dict):
        return ""
    items = stage_ev.get("items") or []
    for item in items:
        if isinstance(item, dict) and not item.get("pass", True):
            bit = "item %s: %s" % (item.get("index"),
                                   item.get("error", "failed"))
            ret = item.get("return_value")
            if ret:
                bit += " return=%r" % (ret[:600],)
            return bit[:900]
    return (stage_ev.get("error") or "")[:900]


def adapt_verdict(res):
    """Map a pipeline result to the repair_loop verdict shape."""
    if res.get("ok"):
        return {"passed": True, "status": "pass", "failures": [],
                "evidence": res.get("evidence", {}), "pipeline": res}
    fails = res.get("failures") or [{}]
    first = fails[0] if isinstance(fails[0], dict) else {}
    stage = first.get("stage", "?")
    reason = first.get("reason", "?")
    detail = first_failing_detail(res.get("evidence", {}), stage)
    return {
        "passed": False, "status": "fail",
        "failures": [{"property": "stage:%s" % stage,
                      "counterexample": detail or reason,
                      "expected": "all rehearsal stages pass",
                      "actual": reason, "callsite": stage}],
        "evidence": res.get("evidence", {}), "pipeline": res}


class CallBudget(object):
    """Counts live Cerebras calls; refuses to exceed CALL_BUDGET."""

    def __init__(self, limit=CALL_BUDGET):
        self.limit = limit
        self.calls = []
        self.results = []

    @property
    def used(self):
        return len(self.calls)

    def generate(self, prompt_text, tag):
        if self.used >= self.limit:
            raise RuntimeError("live-call budget exhausted (%d)" % self.limit)
        out = cerebras_client.generate_candidate(
            prompt_text, max_tokens=GEN_MAX_TOKENS,
            temperature=GEN_TEMPERATURE, reasoning_effort=GEN_REASONING)
        self.results.append(out)
        self.calls.append({
            "tag": tag,
            "model": out.get("model"),
            "finish_reason": out.get("finish_reason"),
            "latency_ms": out.get("latency_ms"),
            "input_tokens": out.get("input_tokens"),
            "output_tokens": out.get("output_tokens"),
            "cost_usd": out.get("cost_usd"),
            "request_id": out.get("request_id"),
            "ok": out.get("ok"),
            "error_type": (out.get("error") or {}).get("error_type"),
            "error": out.get("error"),
            "raw": out.get("raw"),
        })
        return out

    def totals(self):
        return cerebras_client.aggregate_totals(self.results)


def task_spec_text(task):
    lines = [task["prompt"], "",
             "You write ONLY the CSV parser. A helper is already defined "
             "in the test environment (do NOT define it again):",
             "(gg-emit-json header rows) takes a header string list and a "
             "list of rows and returns the compact JSON array string.",
             "Each row is a list of plain strings, except the keyword "
             ":null which renders as JSON null (use :null exactly when "
             "the task needs JSON null for a field).",
             'Example: (gg-emit-json (quote ("a" "b")) '
             '(quote (("1" :null)))) returns [{"a": "1", "b": null}].',
             "",
             "Function contract: define (defun %s (text) ...) taking the "
             "whole CSV input as one string; parse it with your own code "
             "and return (gg-emit-json header rows). Pure function: no "
             "printing, no file or network access, no globals. Parse "
             "the CSV yourself in standard Common Lisp (no libraries) "
             "and honor quoting, escapes, and newlines exactly as the "
             "task describes." % FN_NAME,
             "Lisp string rules: a literal backslash is written \\\\ and a "
             "quote as \\\"; there is NO \\n escape. Emit a real "
             "newline only via #\\Newline or a literal line break.",
             "VALIDATOR RULES (the output is machine-checked before it "
             "runs, and these break the checker): never write #\\\" "
             "anywhere — use (code-char 34) for the quote character; "
             "never write #\\, — use (code-char 44) for comma; avoid "
             "#\\' — use (code-char 39). Other character literals "
             "(#\\Newline #\\Return #\\Tab #\\\\ #\\[ #\\] #\\{ #\\}) "
             "are fine."]
    for i, c in enumerate(task["checks"]):
        lines.append("")
        lines.append("Check %d input:" % (i + 1))
        lines.append(c["input"])
        lines.append("Check %d expected output:" % (i + 1))
        lines.append(c["expected"])
    lines.append("")
    lines.append("Emit exactly one (candidate ...) form with (:target %s) "
                 "(:parent 0) and (:definition ((defun %s (text) ...))). "
                 "Helpers allowed via "
                 "(:definition ((progn (defun ...) (defun %s ...)))). "
                 "Start your answer with (candidate and end with its "
                 "closing paren: no ``` fences, no prose before or after. "
                 "Use only standard Common Lisp with no libraries; define "
                 "every helper you call."
                 % (FN_NAME, FN_NAME, FN_NAME))
    return "\n".join(lines)


def compile_task_context(task, retrieved, failures, budget_words):
    goal = {"text": "Solve %s: %s" % (task["id"], task["prompt"]),
            "contracts": [
                "(defun %s (text)) parses CSV itself, then returns "
                "(gg-emit-json header rows)" % FN_NAME,
                "header/rows are string lists; the keyword :null "
                "renders as JSON null",
                "pure: no I/O, no globals, no network"],
            "task": task_spec_text(task)}
    return context_mod.compile_context(
        goal, retrieved, failures, ["pure"],
        {"max_tokens": budget_words, "max_capabilities": 2})


def strip_one_fence_pair(text):
    """Remove exactly one ```...``` fence pair (baseline-A rule).

    Returns the inner text when ``text`` contains an opening fence line
    and a later closing fence line, else None. Minimal and auditable:
    one layer only, no recursion, no other trimming.
    """
    if not isinstance(text, str):
        return None
    lines = text.split("\n")
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("```"):
            start = i
            break
    if start is None:
        return None
    for j in range(start + 1, len(lines)):
        if lines[j].strip().startswith("```"):
            inner = "\n".join(lines[start + 1:j])
            return inner if inner.strip() else None
    return None


# s_expr has two known gaps vs real Common Lisp: it cannot parse the
# `#\"` character literal (the quote opens a runaway string) and it
# tokenizes `,` as whitespace (so `#\,` degrades to `#\`). Valid model
# output using those idioms fails validation through no fault of the
# model, so as a last normalization resort the three exact constructs
# are rewritten to their code-char equivalents (guarded so an escaped
# backslash before them is left alone). Mechanical, reported, and the
# raw text is always preserved in artifacts for audit.
_CHARLIT_RES = None


def _charlit_res():
    global _CHARLIT_RES
    if _CHARLIT_RES is None:
        import re
        _CHARLIT_RES = [
            (re.compile("(?<!\\\\)#\\\\\""), "(code-char 34)"),
            (re.compile("(?<!\\\\)#\\\\,"), "(code-char 44)"),
            (re.compile("(?<!\\\\)#\\\\'"), "(code-char 39)"),
        ]
    return _CHARLIT_RES


def rewrite_charlits(text):
    """Rewrite `#\"`/`#\\,`/`#\\'` to code-char calls (see note above)."""
    out = text
    for pattern, replacement in _charlit_res():
        out = pattern.sub(replacement, out)
    return out


def complete_parens(text):
    """Append missing closing parens when 1-6 forms hang open at EOF.

    Returns the completed text, or None when there is nothing safe to
    do (runaway string, balanced/over-closed text, or more than 6
    unclosed opens). Appending at EOF is the unique minimal completion:
    it closes the open forms in LIFO order without touching any
    existing structure. Like fence-stripping, this is output
    extraction in the HumanEval tradition — the model writes all the
    logic; the completion is reported (paren_complete=N) and the
    completed text must still validate AND pass rehearsal.
    """
    depth = _scan_depth(text)
    if depth is None or depth <= 0 or depth > 6:
        return None
    return text + ")" * depth


def normalize_candidate(raw):
    """Validate raw model output; fall back to documented normalizations.

    Order: raw first; then exactly one fence-pair strip (the rule
    empirically adopted for baselines B-D in documents/baseline-a.md);
    then the mechanical char-literal rewrite above (raw and stripped);
    finally paren completion of the most-processed text (small N only).
    Returns ``(text, info)`` where ``text`` is what rehearsal runs and
    ``info`` records ``{fenced, stripped_ok, charlit_rewrite,
    paren_complete, error}`` honestly either way; the raw text is
    always kept in artifacts.
    """
    attempts = [(raw, False, False, 0)]
    stripped = strip_one_fence_pair(raw)
    if stripped is not None and stripped != raw:
        attempts.append((stripped, True, False, 0))
    rewritten = rewrite_charlits(raw)
    if rewritten != raw:
        attempts.append((rewritten, False, True, 0))
    if stripped is not None and stripped != raw:
        rewritten_stripped = rewrite_charlits(stripped)
        if rewritten_stripped != stripped:
            attempts.append((rewritten_stripped, True, True, 0))
    base, base_fenced, base_charlit, _ = attempts[-1]
    completed = complete_parens(base)
    if completed is not None and completed != base:
        attempts.append((completed, base_fenced, base_charlit,
                         _scan_depth(base)))
    errors = []
    for text, fenced, charlit, paren_n in attempts:
        try:
            s_expr.parse_candidate(text)
            err = None if not errors else "; ".join(errors)
            return text, {"fenced": fenced, "stripped_ok": fenced,
                          "charlit_rewrite": charlit,
                          "paren_complete": paren_n,
                          "error": err, "valid": True}
        except s_expr.SExprError as exc:
            errors.append("%s: %s" % (type(exc).__name__, exc))
    # Nothing validates: return the MOST-processed attempt so the repair
    # counterexample describes the genuine residual error (e.g. paren
    # balance) instead of an already-bridged fence/char-literal artifact.
    text, fenced, charlit, paren_n = attempts[-1]
    return text, {"fenced": fenced, "stripped_ok": False,
                  "charlit_rewrite": charlit, "paren_complete": paren_n,
                  "error": "; ".join(errors),
                  "valid": False, "residual_error": errors[-1]}


def definition_lisp_of(candidate_text):
    """Parse candidate text and render its :definition back to Lisp."""
    parsed = s_expr.parse_candidate(candidate_text)
    return dumps_body(parsed["definition"]), parsed


def _scan_depth(text):
    """String-aware paren depth; None when a string runs away."""
    depth = 0
    instr = False
    esc = False
    for ch in text:
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
        elif ch == '"':
            instr = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
    return None if instr else depth


def parse_failure_note(summary_actual, previous_text):
    """Build precise, minimal-diff repair guidance for a parse failure."""
    import re as _re
    actual = str(summary_actual or "malformed candidate")
    lines = (previous_text or "").split("\n")
    match = _re.search(r"\(line (\d+), column (\d+)\)", actual)
    note = ["Validator error: %s" % actual]
    if "unterminated '('" in actual:
        depth = _scan_depth(previous_text or "")
        tail = "\n".join(lines[-6:])
        note.append("This means %s unclosed ( forms reach the end of the "
                    "output; the fix is to append the missing closing "
                    "parens, nothing else."
                    % ("some" if depth is None else depth))
        note.append("Tail of your output:\n%s" % tail)
    elif match:
        lineno = max(int(match.group(1)) - 1, 0)
        start = max(lineno - 1, 0)
        excerpt = "\n".join("%d: %s" % (i + 1, lines[i])
                            for i in range(start, min(lineno + 2, len(lines)))
                            if 0 <= i < len(lines))
        note.append("Offending region:\n%s" % excerpt)
    note.append("Re-emit the PREVIOUS CANDIDATE below VERBATIM with ONLY "
                "the syntax fix applied: do not rewrite logic, do not "
                "add or remove helpers, no fences, no prose.")
    return "\n".join(note)


def run_rehearsal(candidate_text, task, prior_tasks):
    """Parse -> risk -> rehearse one candidate; returns adapted verdict."""
    try:
        definition_lisp, parsed = definition_lisp_of(candidate_text)
    except s_expr.SExprError as exc:
        res = {"ok": False,
               "failures": [{"stage": "parse",
                             "reason": "malformed candidate: %s" % exc}],
               "evidence": {}, "risk": {"level": "unclassified"},
               "stages": [{"name": "parse", "status": "fail"}],
               "verdict": {"pass": False, "reason": "parse failure",
                           "failed_stage": "parse"}}
        return adapt_verdict(res), None
    tests = build_tests(task, prior_tasks, definition_lisp)
    res = pipeline.run_candidate(candidate_text, tests=tests,
                                 worker_fn=worker_fn, risk_fn=risk_fn)
    return adapt_verdict(res), parsed


def retrieve_for_task(task, index):
    goal = {"text": "Solve %s: %s" % (task["id"], task["prompt"]),
            "input_types": INPUT_TYPES, "output_types": OUTPUT_TYPES,
            "family": FAMILY}
    ranked = retrieve.find_capabilities(goal, {"index": index}, k=3)
    return goal, [{"id": c.id, "score": round(s, 4)} for c, s in ranked]


def register_capability(index, patch, task):
    cap = retrieve.Capability(
        id=patch["patch_id"], intent="Solve %s: %s" % (task["id"], task["id"]),
        input_types=tuple(INPUT_TYPES), output_types=tuple(OUTPUT_TYPES),
        effects=(), family=FAMILY, version=1, success_count=1, use_count=1)
    index.add(cap)
    return cap


def run_task(task, prior_tasks, index, store, tracker, budget,
             seed_patch_id=None, baseline=None):
    """Run the full section-92 chain for one task. Returns evidence dict.

    ``baseline`` is ``(tokens, latency_ms)`` from the task-1 from-scratch
    run, used as the tokens_saved/latency_saved reference for reuse.
    """
    tid = task["id"]
    log("")
    log("=== %s ===" % tid)
    ev = {"task_id": tid, "calls_used": 0, "patch_reused": False,
          "reused_patch_id": None, "outcome": None,
          "baseline": list(baseline or [0, 0.0])}
    calls_before = budget.used
    base_tokens, base_lat = baseline or (0, 0.0)

    # 1. Retrieve: rank known capabilities + live patches for this task.
    goal, ranked = retrieve_for_task(task, index)
    live = store.find_for_task(task_family=FAMILY)
    ev["retrieved"] = ranked
    ev["live_patches"] = [p["patch_id"] for p in live]
    if ranked:
        log("retrieve: %d candidate(s): %s"
            % (len(ranked), ", ".join("%s(%.2f)" % (r["id"][:14], r["score"])
                                      for r in ranked)))
    else:
        log("retrieve: no suitable capability exists")

    # 2. Direct reuse attempt (zero model calls) against the seed patch.
    reuse_verdict = None
    seed = None
    if seed_patch_id:
        seed = store.get_patch(seed_patch_id)
    if seed is not None:
        log("reuse: trying seed patch %s as-is (0 new calls) ..."
            % seed["patch_id"][:19])
        reuse_verdict, _ = run_rehearsal(seed["candidate"]["raw"], task,
                                         prior_tasks)
        pipe = reuse_verdict.get("pipeline") or {}
        log("reuse: %s (risk %s)"
            % ("PASS" if reuse_verdict.get("passed") else "FAIL",
               (pipe.get("risk") or {}).get("level")))
        ev["reuse_attempt"] = {
            "patch_id": seed["patch_id"],
            "passed": bool(reuse_verdict.get("passed")),
            "risk": (pipe.get("risk") or {}).get("level"),
            "verdict_reason": (pipe.get("verdict") or {}).get("reason"),
        }
        if reuse_verdict.get("passed"):
            ev["patch_reused"] = True
            ev["reused_patch_id"] = seed["patch_id"]
            ev["outcome"] = "reused"
            record = tracker.record_reuse(
                seed["patch_id"], tid, helped=True,
                tokens_saved=float(base_tokens),
                latency_saved_ms=float(base_lat),
                held_out=True, kind="direct")
            ev["transfer_outcomes"] = [record]
            ev["calls_used"] = budget.used - calls_before
            ev["calls"] = []
            ev["normalizations"] = []
            ev["gen_tokens"] = 0
            ev["gen_latency_ms"] = 0.0
            return ev

    # 3. Generate (fresh or adaptation): compile context, call model.
    failures = []
    adapt_from = None
    if reuse_verdict is not None and not reuse_verdict.get("passed"):
        failures = [repair.summarize_failure(reuse_verdict)]
        adapt_from = seed
        log("adapt: seed failed as-is; adapting with counterexample: %s"
            % failures[0])
    ctx_budget = CTX_BUDGET_ADAPT if adapt_from else CTX_BUDGET_FRESH
    retrieved_caps = [index.get(r["id"]) for r in ranked]
    retrieved_caps = [c for c in retrieved_caps if c is not None]
    compiled = compile_task_context(task, retrieved_caps, failures, ctx_budget)
    if adapt_from is not None:
        compiled["text"] += (
            "\n\nPRIOR SOLUTION (patch %s from %s; adapt it minimally):\n%s"
            % (adapt_from["patch_id"][:19], adapt_from["task_id"],
               dumps_body(s_expr.parse_candidate(
                   adapt_from["candidate"]["raw"])["definition"])))
        compiled["tokens"] = context_mod.estimate_tokens(compiled["text"])
    ev["context"] = {"tokens": compiled["tokens"], "budget": ctx_budget,
                     "dropped": compiled["dropped"],
                     "sections": compiled["sections"],
                     "text": compiled["text"]}
    log("context: %d words (budget %d, dropped %s)"
        % (compiled["tokens"], ctx_budget, compiled["dropped"] or "none"))

    first = budget.generate(compiled["text"], "%s:generate" % tid)
    candidate_text, norm0 = normalize_candidate(first.get("raw") or "")
    ev["normalizations"] = [norm0]
    log("generate: ok=%s finish=%s in/out=%s/%s (call %d)%s" % (
        first.get("ok"), first.get("finish_reason"),
        first.get("input_tokens"), first.get("output_tokens"), budget.used,
        "" if first.get("ok") else
        " err=%s" % ((first.get("error") or {}).get("message"),)))
    if norm0["fenced"]:
        log("normalize: fenced output, stripped_ok=%s" % norm0["stripped_ok"])
    attempted = [candidate_text]

    # 4. Rehearse + bounded repair (max 1) from scratch each attempt.
    last = {}

    def pipeline_fn(text, _tests):
        verdict, _ = run_rehearsal(text, task, prior_tasks)
        last["verdict"] = verdict
        return verdict

    def generate_fn(previous_text, summary):
        repair_goal = dict(goal)
        if str(summary.get("property", "")).startswith("stage:parse"):
            body = parse_failure_note(summary.get("actual"), previous_text)
            applied = []
            if norm0.get("fenced"):
                applied.append("fences stripped")
            if norm0.get("charlit_rewrite"):
                applied.append("char-literals rewritten to code-char calls")
            if applied:
                body += ("\nMechanical normalizations already applied to "
                         "the text below (%s); keep them, do not revert "
                         "them." % (", ".join(applied),))
        else:
            body = ("MINIMAL COUNTEREXAMPLE (fix exactly this): %s\n\n"
                    % (summary,) + task_spec_text(task))
        repair_goal["task"] = (
            body
            + "\n\nPREVIOUS CANDIDATE (failed; fix it minimally):\n"
            + previous_text)
        repair_ctx = context_mod.compile_context(
            repair_goal, retrieved_caps, [summary], ["pure"],
            {"max_tokens": CTX_BUDGET_ADAPT, "max_capabilities": 2})
        out = budget.generate(repair_ctx["text"], "%s:repair" % tid)
        fixed, norm = normalize_candidate(out.get("raw") or "")
        ev["normalizations"].append(norm)
        log("repair: ok=%s finish=%s in/out=%s/%s (call %d)%s" % (
            out.get("ok"), out.get("finish_reason"),
            out.get("input_tokens"), out.get("output_tokens"), budget.used,
            "" if out.get("ok") else
            " err=%s" % ((out.get("error") or {}).get("message"),)))
        if norm["fenced"]:
            log("normalize: fenced output, stripped_ok=%s"
                % norm["stripped_ok"])
        attempted.append(fixed)
        return fixed

    loop = repair.repair_loop(candidate_text, None, pipeline_fn=pipeline_fn,
                              generate_fn=generate_fn, max_repairs=1)
    # repair_loop drops the verdict on escalation; last[] keeps it.
    pipe = (last.get("verdict") or {}).get("pipeline") or {}
    risk_info = pipe.get("risk") or {}
    reasons = risk_info.get("reasons") or []
    ev["repair"] = {"status": loop.get("status"),
                    "repairs_used": loop.get("repairs_used"),
                    "reason": loop.get("reason"),
                    "risk": risk_info.get("level"),
                    "risk_reason": (reasons[0][:160] if reasons else None),
                    "verdict_reason": (pipe.get("verdict") or {}).get(
                        "reason"),
                    "stages": [(s.get("name"), s.get("status"))
                               for s in pipe.get("stages", [])],
                    "history": loop.get("history"),
                    "failure": loop.get("failure")}
    log("rehearse: %s risk=%s repairs_used=%d (%s)"
        % (loop.get("status"), ev["repair"]["risk"],
           loop.get("repairs_used"), ev["repair"]["verdict_reason"]))
    ev["candidates"] = attempted
    ev["final_candidate_raw"] = loop.get("candidate", attempted[-1])
    ev["calls"] = [c for c in budget.calls if c["tag"].startswith(tid)]

    # 5. Persist successful candidates as patches; record transfer reuse.
    ev["transfer_outcomes"] = []
    if loop.get("status") == "success":
        try:
            parsed = s_expr.parse_candidate(loop["candidate"])
            parsed_json = json.loads(json.dumps(parsed, default=str))
        except s_expr.SExprError:
            parsed_json = None
        patch = store.save_patch(
            tid, {"raw": loop["candidate"], "parsed": parsed_json},
            task_family=FAMILY, tags=["csv", "json", "records"],
            status="patch")
        ev["patch_id"] = patch["patch_id"]
        register_capability(index, patch, task)
        log("patch: persisted %s" % patch["patch_id"][:19])
        ev["outcome"] = "adapted" if adapt_from else "fresh"
        if adapt_from is not None:
            helped_direct = False
            record_direct = tracker.record_reuse(
                adapt_from["patch_id"], tid, helped=helped_direct,
                tokens_saved=0.0, latency_saved_ms=0.0,
                held_out=True, kind="direct")
            gen_here = [c for c in budget.calls
                        if c["tag"].startswith(tid)]
            gen_tokens = sum((c.get("input_tokens") or 0)
                             + (c.get("output_tokens") or 0)
                             for c in gen_here)
            gen_lat = sum(c.get("latency_ms") or 0.0 for c in gen_here)
            record_adapt = tracker.record_reuse(
                adapt_from["patch_id"], tid, helped=True,
                tokens_saved=float(base_tokens - gen_tokens),
                latency_saved_ms=float(base_lat - gen_lat),
                held_out=True, kind="adaptation")
            ev["transfer_outcomes"] = [record_direct, record_adapt]
    else:
        ev["outcome"] = "failed"
        if adapt_from is not None:
            ev["transfer_outcomes"] = [tracker.record_reuse(
                adapt_from["patch_id"], tid, helped=False,
                tokens_saved=0.0, latency_saved_ms=0.0,
                held_out=True, kind="direct")]

    ev["calls_used"] = budget.used - calls_before
    # Baselines for later tasks' tokens_saved accounting.
    gen = [c for c in budget.calls if c["tag"].startswith(tid)]
    ev["gen_tokens"] = sum((c.get("input_tokens") or 0)
                           + (c.get("output_tokens") or 0) for c in gen)
    ev["gen_latency_ms"] = sum(c.get("latency_ms") or 0.0 for c in gen)
    return ev


def summarize_transfer(tracker, patch_ids):
    gates = {}
    for pid in patch_ids:
        outcomes = tracker.get_outcomes(pid)
        gate = tracker.promote_or_hold(pid, baseline_success=0.0)
        gates[pid] = {"outcomes": len(outcomes),
                      "independent_reuses":
                          gate["metrics"]["independent_reuses"],
                      "decision": gate["decision"],
                      "reasons": gate["reasons"]}
    return gates


def run_demo(out_dir, only=None):
    started = time.time()
    os.makedirs(out_dir, exist_ok=True)
    store = patches.PatchStore(os.path.join(out_dir, "patches"))
    tracker = transfer.TransferTracker(os.path.join(out_dir, "transfer"))
    index = retrieve.CapabilityIndex()
    budget = CallBudget()
    tasks = load_tasks()
    if only:
        tasks = [t for t in tasks if t["id"] in only]

    log("demo: %d tasks in sequence: %s"
        % (len(tasks), ", ".join(t["id"] for t in tasks)))
    log("demo: live-call budget %d; model %s; temp=%.1f reasoning=%s" % (
        CALL_BUDGET, cerebras_client.DEFAULT_MODEL, GEN_TEMPERATURE,
        GEN_REASONING))

    evidences = []
    prior = []
    seed_patch_id = None
    baseline = None
    for task in tasks:
        ev = run_task(task, prior, index, store, tracker, budget,
                      seed_patch_id=seed_patch_id, baseline=baseline)
        evidences.append(ev)
        with open(os.path.join(out_dir, "%s.json" % task["id"]), "w",
                  encoding="utf-8") as fh:
            json.dump(ev, fh, indent=2, sort_keys=True, default=str)
        prior.append(task)
        if seed_patch_id is None and ev.get("patch_id"):
            seed_patch_id = ev["patch_id"]
            baseline = (ev["gen_tokens"], ev["gen_latency_ms"])

    totals = budget.totals()
    patch_ids = [e["patch_id"] for e in evidences if e.get("patch_id")]
    gates = summarize_transfer(tracker, patch_ids)
    summary = {
        "tasks": [{"task_id": e["task_id"], "outcome": e["outcome"],
                   "calls_used": e["calls_used"],
                   "patch_reused": e["patch_reused"],
                   "reused_patch_id": e["reused_patch_id"],
                   "patch_id": e.get("patch_id"),
                   "repairs_used": (e.get("repair") or {}).get("repairs_used"),
                   "risk": (e.get("repair") or {}).get("risk")}
                  for e in evidences],
        "live_calls": budget.calls,
        "totals": totals,
        "budget": CALL_BUDGET,
        "transfer_gates": gates,
        "elapsed_s": round(time.time() - started, 1),
    }
    with open(os.path.join(out_dir, "summary.json"), "w",
              encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True, default=str)

    log("")
    log("=== section-92 chain evidence ===")
    for e in evidences:
        log("%s outcome=%s calls=%d reused=%s patch=%s" % (
            e["task_id"], e["outcome"], e["calls_used"], e["patch_reused"],
            (e.get("reused_patch_id") or e.get("patch_id") or "-")[:19]))
    for pid, g in gates.items():
        log("transfer %s: outcomes=%d independent=%d decision=%s (%s)" % (
            pid[:19], g["outcomes"], g["independent_reuses"], g["decision"],
            "; ".join(g["reasons"])))
    log("totals: %d calls (budget %d), %s tokens, $%s, %.1fs elapsed" % (
        totals["calls"], CALL_BUDGET, totals["total_tokens"],
        totals["cost_usd"], summary["elapsed_s"]))
    with open(os.path.join(out_dir, "transcript.log"), "w",
              encoding="utf-8") as fh:
        fh.write("\n".join(LOG_LINES) + "\n")
    return summary


# A hand-written reference candidate proving the harness offline: a small
# RFC-4180 CSV reader for A-EXP-05 that calls the harness gg-emit-json.
SMOKE_CANDIDATE = """(candidate (:target solve-csv) (:parent 0)
 (:reason smoke-wiring)
 (:definition ((progn
 (defun gg-smoke-parse (text)
   (let ((rows (list)) (row (list)) (field (make-string-output-stream))
         (in-q nil) (i 0) (n (length text)))
     (labels ((push-field ()
                (push (get-output-stream-string field) row)
                (setf field (make-string-output-stream)))
              (push-row () (push-field)
                (push (nreverse row) rows) (setf row (list))))
       (loop while (< i n) do
         (let ((ch (char text i)))
           (cond
             (in-q (cond ((char= ch (code-char 34))
                          (if (and (< (1+ i) n)
                                   (char= (char text (1+ i)) (code-char 34)))
                              (progn (write-char (code-char 34) field) (incf i))
                              (setf in-q nil)))
                         (t (write-char ch field))))
             ((char= ch (code-char 34)) (setf in-q t))
             ((char= ch (code-char 44)) (push-field))
             ((char= ch #\\Return) nil)
             ((char= ch #\\Newline) (push-row))
             (t (write-char ch field))))
         (incf i))
       (push-row)
       (nreverse rows))))
 (defun gg-smoke-escape (s)
   (with-output-to-string (out)
     (loop for ch across s do
       (cond ((char= ch (code-char 34)) (write-string "\\\\\\"" out))
             ((char= ch #\\\\) (write-string "\\\\\\\\" out))
             ((char= ch #\\Newline) (write-string "\\\\n" out))
             ((char= ch #\\Return) (write-string "\\\\r" out))
             ((char= ch #\\Tab) (write-string "\\\\t" out))
             (t (write-char ch out))))))
 (defun gg-smoke-json (header rows)
   (with-output-to-string (out)
     (write-char #\\[ out)
     (loop for r on rows for idx from 0 do
       (unless (zerop idx) (write-char (code-char 44) out))
       (write-char #\\{ out)
       (loop for h in header for f in (car r) for j from 0 do
         (unless (zerop j) (write-char (code-char 44) out))
         (write-char (code-char 34) out) (write-string h out)
         (write-string "\\":\\"" out)
         (write-string (gg-smoke-escape f) out)
         (write-char (code-char 34) out))
       (write-char #\\} out))
     (write-char #\\] out)))
 (defun solve-csv (text)
   (let ((rows (gg-smoke-parse text)))
     (gg-emit-json (car rows) (cdr rows))))))))"""

SMOKE_BROKEN = ("(candidate (:target solve-csv) (:parent 0) "
                "(:definition ((defun solve-csv (text) "
                "(declare (ignore text)) \"wrong\"))))")


def run_smoke(out_dir):
    """Offline wiring check: no live calls; asserts the full chain works."""
    os.makedirs(out_dir, exist_ok=True)
    log("smoke: offline wiring check (0 live calls)")
    tasks = load_tasks()
    task = tasks[0]

    # dumps_lisp round-trip.
    parsed = s_expr.parse_candidate(SMOKE_CANDIDATE)
    Definition = dumps_lisp(parsed["definition"])  # noqa: N806 - display name
    reparsed = s_expr.parse_candidate(
        "(candidate (:target solve-csv) (:parent 0) (:definition %s))"
        % Definition)
    assert reparsed["definition"] == parsed["definition"], \
        "dumps_lisp round-trip mismatch"
    log("smoke: dumps_lisp round-trip OK")

    # normalize_candidate: raw-first, fence fallback, best-attempt fallback.
    fenced = "```lisp\n" + SMOKE_CANDIDATE + "\n```\n"
    _text, _info = normalize_candidate(fenced)
    assert _info["valid"] and _info["fenced"], _info
    _text2, _info2 = normalize_candidate("```\n(not a candidate\n```")
    assert not _info2["valid"] and _info2["fenced"], _info2
    _text3, _info3 = normalize_candidate(SMOKE_CANDIDATE[:-1])
    assert _info3["valid"] and _info3["paren_complete"] == 1, _info3
    log("smoke: normalize_candidate fallbacks OK")

    # risk gate.
    level = risk_fn(parsed)["level"]
    log("smoke: risk level %s" % level)
    assert level in ("R0", "R1"), "unexpected risk %s" % level

    # pipeline rehearsal of the reference candidate vs task-1 checks.
    verdict, _ = run_rehearsal(SMOKE_CANDIDATE, task, [])
    pipe = verdict.get("pipeline") or {}
    log("smoke: rehearsal %s (%s)"
        % ("PASS" if verdict.get("passed") else "FAIL",
           (pipe.get("verdict") or {}).get("reason")))
    assert verdict.get("passed"), "smoke candidate failed: %r" % (pipe,)

    # bounded repair: broken candidate + stub generator -> success, 1 repair.
    box = {"repairs": 0}

    def stub_pipeline(text, _tests):
        verdict0, _ = run_rehearsal(text, task, [])
        return verdict0

    def stub_generate(_previous, summary):
        box["repairs"] += 1
        assert summary.get("property"), "empty repair summary"
        return SMOKE_CANDIDATE

    loop = repair.repair_loop(SMOKE_BROKEN, None, pipeline_fn=stub_pipeline,
                              generate_fn=stub_generate, max_repairs=1)
    assert loop["status"] == "success" and loop["repairs_used"] == 1, \
        "repair loop: %r" % (loop,)
    log("smoke: repair_loop success repairs_used=1")

    # patches + retrieve + transfer wiring (sandboxed dirs).
    store = patches.PatchStore(os.path.join(out_dir, "patches"))
    tracker = transfer.TransferTracker(os.path.join(out_dir, "transfer"))
    index = retrieve.CapabilityIndex()
    patch = store.save_patch(task["id"], {"raw": SMOKE_CANDIDATE},
                             task_family=FAMILY, tags=["csv"])
    assert store.get_patch(patch["patch_id"]) is not None
    assert store.find_for_task(task_family=FAMILY)
    register_capability(index, patch, task)
    _goal, ranked = retrieve_for_task(tasks[1], index)
    assert ranked and ranked[0]["id"] == patch["patch_id"], ranked
    tracker.record_reuse(patch["patch_id"], tasks[1]["id"], helped=True,
                         held_out=True)
    gate = tracker.promote_or_hold(patch["patch_id"])
    assert gate["decision"] == "hold", gate  # 1 reuse < 3 required
    log("smoke: patches/retrieve/transfer OK (gate=%s)" % gate["decision"])

    # reuse of the reference patch against task 2 as-is (informational).
    verdict2, _ = run_rehearsal(SMOKE_CANDIDATE, tasks[1], [task])
    log("smoke: reference patch on %s: %s"
        % (tasks[1]["id"], "PASS" if verdict2.get("passed") else "FAIL"))

    summary = {"ok": True, "risk": level, "repair_repairs_used": 1,
               "gate": gate["decision"],
               "task2_reuse_pass": bool(verdict2.get("passed"))}
    with open(os.path.join(out_dir, "smoke.json"), "w",
              encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)
    with open(os.path.join(out_dir, "transcript.log"), "w",
              encoding="utf-8") as fh:
        fh.write("\n".join(LOG_LINES) + "\n")
    log("smoke: ALL CHECKS PASSED")
    return summary


def main(argv):
    args = list(argv[1:])
    if "--smoke" in args:
        run_smoke(os.path.join(ARTIFACTS, "smoke"))
        return 0
    out_dir = ARTIFACTS
    only = None
    rest = []
    i = 0
    while i < len(args):
        if args[i] == "--out" and i + 1 < len(args):
            # Iteration runs nest under artifacts/demo-e2e/<name>.
            out_dir = os.path.join(ARTIFACTS, args[i + 1])
            i += 2
        elif args[i] == "--only" and i + 1 < len(args):
            only = args[i + 1].split(",")
            i += 2
        else:
            rest.append(args[i])
            i += 1
    if rest and rest != ["--live"]:
        print("usage: demo.py [--smoke|--live] [--out NAME] [--only IDS]",
              file=sys.stderr)
        return 2
    run_demo(out_dir, only)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
