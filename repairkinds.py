"""Catalogue of every repair the agent harness can apply to a model reply.

The harness repairs the model's JSON and Lisp for free before any test runs
(agent_session.normalize_plan and its helpers), and it has a few stronger
interventions that change what a test requires: the property-test rescue, the
test-call repair, the reference-value oracle, the rule that blames the test
for a near miss, and the dropping of tests. Each has a record in RULES that
says what it changes, whether it can weaken a test, and where it is
implemented. The records were written by reading the code, not by the names.

Terms used in the records:

* effect "syntax": makes malformed text well-formed without changing what it
  means (for example a missing closing paren).
* effect "shape": rewrites data into the layout the harness requires, same
  content (for example a state argument nested one level deeper).
* effect "meaning": changes what the code does or what a test expects.
* effect "removal": drops a test or a requirement.
* effect "cosmetic": cannot change any test result.
* weakens_tests: True when a test can be changed or removed so that it passes
  where it would have failed before, for a reason other than malformed
  syntax. A changed expected value, a relaxed expected value and a removed
  test all count. A rewrite of the CODE does not count on its own: the test
  stays as it was and judges the rewritten code.
* origin: "engineered" when a developer wrote the rule by hand from reading
  failure logs. "learned" would mean the system derived the rule itself and
  validated it. Every rule here is "engineered".

Only the functions named here are read; no model is called and no file is
written. Run ``uv run python repairkinds.py`` (add ``--json`` for JSON) to see
the catalogue with counts from the live session logs.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import textwrap
from collections import Counter
from pathlib import Path

import economics
import oracle as orc

ROOT = Path(__file__).resolve().parent

EFFECTS = ("syntax", "shape", "meaning", "removal", "cosmetic")
TARGETS = ("code", "test-call", "test-expectation", "test-set", "docs")
ORIGINS = ("engineered", "learned")

#: Modules whose normaliser functions emit fix ids (see find_fix_ids).
SOURCE_FILES = ("agent_session.py", "webkit.py", "lispstyle.py", "compaction.py", "oracle.py")

#: Functions whose ``fixes`` lists are the ids written into ``auto_fixes``.
FIX_SCOPES = ("normalize_plan", "_parsed")


def _rule(target, effect, weakens, where, what, origin="engineered"):
    return {"target": target, "effect": effect, "weakens_tests": weakens,
            "origin": origin, "where": where, "what": what}


RULES = {
    # ---- fix ids applied to the definition (the code) -----------------------
    "trimmed-surplus-paren": _rule(
        "code", "syntax", False, "agent_session.py:trim_surplus_parens",
        "Drops stray closing parens after the one top-level form, e.g. "
        "(defun f (x) x)) -> (defun f (x) x). A reader rejects the surplus "
        "outright, so the model would only ever see an unexpected-paren error."),
    "completed-missing-paren": _rule(
        "code", "syntax", False, "agent_session.py:complete_parens",
        "Appends up to three missing closing parens when the text ends with "
        "forms still open, e.g. (defun f (x) (+ x 1) -> (defun f (x) (+ x 1)). "
        "Borderline: the closers go at the end of the text, so where they "
        "belong is a guess; the tests still judge the result."),
    "newline-escape-in-string": _rule(
        "code", "meaning", False, "agent_session.py:newline_escapes",
        "Turns backslash-n inside a Lisp string into a real line break, e.g. "
        "\"a\\nb\" -> a line break between a and b. Lisp reads backslash-n as "
        "the letter n, so this changes what the code prints; no test is edited."),
    "css-rule-commas": _rule(
        "code", "syntax", False, "lispstyle.py:fix_css_commas",
        "Removes a comma put between CSS rules, e.g. a{x:1},b{y:2} -> "
        "a{x:1}b{y:2}; only a comma right after a closing brace of a rule is "
        "removed."),
    "doubled-percent": _rule(
        "code", "meaning", False, "agent_session.py:normalize_plan",
        "Turns a doubled percent sign inside a string literal into one, e.g. "
        "\"width:100%%\" -> \"width:100%\". This changes the string the code "
        "produces (a printf habit), so it is a change of code, not of a test."),
    "flattened-setf": _rule(
        "code", "syntax", False, "agent_session.py:fix_setf",
        "Flattens a place/value pair wrapped in parens inside SETF, e.g. "
        "(setf a 1 (b 2)) -> (setf a 1 b 2); only an odd-argument SETF whose "
        "last argument is a two-element list is touched."),
    "let-to-let-star": _rule(
        "code", "meaning", False, "lispstyle.py:let_to_let_star",
        "Changes LET to LET* when a binding uses an earlier binding of the same "
        "LET, e.g. (let ((a 1) (b a)) -> (let* ((a 1) (b a)). Borderline: if an "
        "outer variable has the same name as the earlier binding, LET read that "
        "outer value and LET* reads the new one."),
    "added-docstring": _rule(
        "code", "cosmetic", False, "lispstyle.py:ensure_docstring",
        "Inserts the tool's description as a docstring when the definition has "
        "none, e.g. (defun f (x) x) -> (defun f (x) \"Adds one.\" x). A docstring "
        "is never evaluated, so no result changes."),
    "repaired-json": _rule(
        "code", "syntax", False, "agent_session.py:_loads_tolerant",
        "Repairs a reply that is not strict JSON: a literal line break inside a "
        "string, or a closing quote the model forgot just before a brace. "
        "Borderline: the inserted quote ends the string at the last brace that "
        "gives a valid object, so the text up to that brace becomes the value, "
        "and the fix can touch any field of the reply."),
    # ---- fix ids applied to a test call ---------------------------------------
    "rebalanced-let-around-call": _rule(
        "test-call", "syntax", False, "agent_session.py:balance_let_call",
        "Rebalances a test call wrapped in LET, (let ((r (tool a b))) body), when "
        "the parens after the arguments are off by the three that close the call, "
        "the binding and the binding list. Only an unbalanced call of exactly this "
        "shape, whose arguments match the tool's defun, is touched."),
    "restored-paren-in-call": _rule(
        "test-call", "syntax", False, "agent_session.py:balance_call",
        "Puts back one closing paren dropped inside nested data, but only when "
        "exactly one placement gives the tool its argument count, e.g. a call one "
        "paren short -> the one placement that makes the call pass two arguments."),
    "trimmed-surplus-paren-in-call": _rule(
        "test-call", "syntax", False, "agent_session.py:trim_surplus_parens",
        "Drops stray closing parens after a test call's one form, e.g. "
        "(f 1)) -> (f 1)."),
    "quoted-data-list": _rule(
        "test-call", "syntax", False, "agent_session.py:quote_literals",
        "Adds a quote before bare list data in a call, e.g. (f (3 4 5)) -> "
        "(f '(3 4 5)), and removes a quote nested inside already quoted data, "
        "e.g. '(a '(b)) -> '(a (b)). Borderline: the second part changes the "
        "datum the test passes in; the build prompt calls nested quote marks a "
        "slip, so it is treated as syntax here."),
    "nested-state-data": _rule(
        "test-call", "shape", False, "webkit.py:fix_state_args",
        "Rewrites a state argument into the (name rows) table layout, same content, "
        "e.g. (:users ((\"a\" \"pw\"))) -> '((\"users\" ((\"a\" \"pw\")))); a state "
        "split over several quoted literals is merged into one list. Borderline: a "
        "keyword name becomes a string name (:users -> \"users\"), which assumes the "
        "keyword meant the table name. Also applied to step specs."),
    "dropped-unusable-test": _rule(
        "test-set", "removal", True, "agent_session.py:normalize_plan",
        "Drops tests that are prose or call a function other than the tool, but "
        "only while usable tests remain, e.g. three tests, one of them (no-such-fn 1), "
        "become two. The dropped calls are kept in dropped_tests and in the "
        "auto_fix event."),
    # ---- fix ids applied to an expected value ---------------------------------
    "quoted-bare-expected-string": _rule(
        "test-expectation", "syntax", False, "agent_session.py:quote_bare_string",
        "Quotes an expected value that is several bare words, e.g. No tasks. -> "
        "\"No tasks.\", because a printed value is one datum and cannot be several "
        "words. Borderline: a bare value with spaces that starts with a letter or a "
        "digit is always quoted."),
    "newline-escape-in-expected-string": _rule(
        "test-expectation", "syntax", False, "agent_session.py:newline_escapes",
        "Turns backslash-n inside an expected string into a real line break, e.g. "
        "\"a\\nb\" -> a line break; Lisp has no backslash-n escape, so the model "
        "meant a line break."),
    "trimmed-surplus-paren-in-expected-value": _rule(
        "test-expectation", "syntax", False, "agent_session.py:trim_surplus_parens",
        "Drops stray closing parens after an expected value, e.g. (1 2)) -> (1 2)."),
    "unescaped-quotes-in-expected-value": _rule(
        "test-expectation", "syntax", False, "agent_session.py:unescape_quotes",
        "Removes the backslashes of an expected value whose every quote mark is escaped, "
        "e.g. (:output \\\"Saved\\\") -> (:output \"Saved\"). Applied only when no quote "
        "mark is left unescaped, so no real string is touched."),
    "completed-paren-in-expected-value": _rule(
        "test-expectation", "syntax", False, "agent_session.py:complete_parens",
        "Appends missing closing parens to an expected value, e.g. (1 (2 3) -> "
        "(1 (2 3)). Borderline: where the parens belong is a guess, and the code's "
        "printed result must still equal the value."),
    "unchanged-state-in-expected-response": _rule(
        "test-expectation", "meaning", True, "agent_session.py:unchanged_state",
        "Rewrites an expected response's :state S to :state-unchanged S when S equals "
        "the state passed in, e.g. (:status 200 :state (...)) -> (:status 200 "
        ":state-unchanged (...)). The checker then accepts a response that leaves "
        ":state out, so this relaxes the expectation even though it follows the "
        "contract's own rule; it is treated as a changed expectation."),
    "quoted-spec-example": _rule(
        "docs", "syntax", False, "agent_session.py:_quote_spec_examples",
        "Quotes the call in an e.g. example of a step's spec, e.g. e.g. (f (1 2)) => 3 "
        "-> e.g. (f '(1 2)) => 3. The spec is prompt text, not a test, but it guides "
        "the build, so it is not counted as cosmetic."),
    # ---- stronger interventions -----------------------------------------------
    "rescue-property-tests": _rule(
        "test-expectation", "meaning", True, "agent_session.py:Session._build_loop",
        "When the evidence blames the tests, keeps the definition, asks the model for "
        "the tests again with exact expected values replaced by property tests "
        "(expect T), and rehearses the same code. The exact values first written are "
        "recorded as replaced in the ledger (relaxed_tests)."),
    "test-call-repair": _rule(
        "test-call", "meaning", True, "agent_session.py:Session._build_loop",
        "When a test call is not valid Lisp, keeps the definition and takes the "
        "model's new test list whole, expected values included. The prompt says to keep "
        "the values that passed before, but the freeze is enforced only for an identical "
        "call text, so a rewritten call escapes it."),
    "oracle-reference-value": _rule(
        "test-expectation", "meaning", True, "agent_session.py:Session._reference_pass",
        "For a known hash or checksum (FNV-1a 32 and 64, CRC-32, Adler-32, MD5, SHA-1, "
        "SHA-256), replaces the expected value of a failing test by the value Python "
        "computes for the same input, e.g. a guessed hex digest -> the real digest."),
    "tests-only-reply-replaces-list": _rule(
        "test-set", "removal", True, "compaction.py:_merge_build",
        "A reply that carries only tests (after a test-call or rescue repair) replaces the "
        "whole test list, so a test the reply leaves out is gone. The log keeps the full "
        "decision event, not a diff. This rule has no event of its own."),
    # ---- triggers: rules that choose the rescue, changing no test themselves -----
    "near-miss-blames-test": _rule(
        "test-expectation", "meaning", True, "oracle.py:failure_class",
        "Classes a failure as TEST_WRONG when the code's float is within five percent of "
        "the expected one but not equal, e.g. got 948.10406 for an expected 951.12, which "
        "selects the rescue. It changes no test itself."),
    "low-confidence-blames-test": _rule(
        "test-expectation", "meaning", True, "oracle.py:failure_class",
        "Classes a failure as TEST_WRONG when every wrong value looks low-confidence by a "
        "text rule: a long integer, a hex-like string, a float with six or more decimals, or "
        "a call name containing hash, random, nonce and similar. The rule ignores the size of "
        "the error, so a sign error returning -200.0 for an expected 10.008 is blamed on the "
        "test. It changes no test itself."),
    "stuck-result-blames-test": _rule(
        "test-expectation", "meaning", True, "agent_session.py:Session._build_loop",
        "When identical code returns the same value for a call in two attempts, treats the "
        "expected value as a guess and selects the rescue. It changes no test itself."),
    "expected-drift-blames-test": _rule(
        "test-expectation", "meaning", True, "agent_session.py:Session._build_loop",
        "When an expected value for a call has changed between attempts (verdict drift), the "
        "verdict is AMBIGUOUS and the test is blamed, which selects the rescue. The changing "
        "values are the model's own and are listed in the verdict's drift field."),
}

#: Which logged event carries each intervention (fix ids are auto_fix events).
_LOG_KIND = {
    "rescue-property-tests": "rescue",
    "test-call-repair": "test_call_repair",
    "oracle-reference-value": "oracle",
    "near-miss-blames-test": "trigger",
    "low-confidence-blames-test": "trigger",
    "stuck-result-blames-test": "trigger",
    "expected-drift-blames-test": "trigger",
    "tests-only-reply-replaces-list": None,
}
#: Log kinds whose events each count once in the totals.
_PRIMARY = ("auto_fix", "rescue", "test_call_repair", "oracle")


def _log_kind(rule_id):
    return _LOG_KIND.get(rule_id, "auto_fix")


# ---------------------------------------------------------------- source scan

def _is_str(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _is_name(node, name):
    return isinstance(node, ast.Name) and node.id == name


def _strings(node):
    """Strings written as the elements of list literals inside NODE (not call arguments)."""
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.List):
            out.extend(elt.value for elt in n.elts if _is_str(elt))
    return out


def _call_name(call):
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_fixes_append(call):
    func = call.func
    return (isinstance(func, ast.Attribute) and func.attr == "append"
            and _is_name(func.value, "fixes") and bool(call.args) and _is_str(call.args[0]))


def _ids_in_tree(tree):
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in FIX_SCOPES:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Assign) and any(_is_name(t, "fixes") for t in inner.targets):
                    ids.update(_strings(inner.value))
                elif isinstance(inner, ast.Call) and _is_fixes_append(inner):
                    ids.add(inner.args[0].value)
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name == "step" and node.args and _is_str(node.args[0]):
                ids.add(node.args[0].value)
            if name == "_note_changes" and len(node.args) >= 2:
                ids.update(_strings(node.args[1]))
    return ids


def find_fix_ids(root=None):
    """Every fix id, and every intervention id, the harness can write into a reply or a tool.

    Reads the source files named in SOURCE_FILES with ast: the first string
    argument of each ``step("...", ...)`` call, each string appended to or
    assigned into a ``fixes`` list inside normalize_plan or _parsed, and each
    string in the id list passed to ``_note_changes``. Intervention ids that
    are chosen by classification (near-miss and similar) are not literals in
    the code and are not found here; they are listed in RULES by hand.
    """
    base = Path(root) if root is not None else ROOT
    ids = set()
    for name in SOURCE_FILES:
        path = base / name
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, ValueError):
            continue
        ids |= _ids_in_tree(tree)
    return ids


# ------------------------------------------------------------------ classify

def classify(fix_ids):
    """Summary of one reply's or one build's ids: effects, weakening ids, unknown ids, origins."""
    ids = sorted({i for i in fix_ids if isinstance(i, str)})
    known = [i for i in ids if i in RULES]
    effects = {e: 0 for e in EFFECTS}
    origins = {o: 0 for o in ORIGINS}
    for i in known:
        effects[RULES[i]["effect"]] += 1
        origins[RULES[i]["origin"]] += 1
    return {"effects": effects,
            "weakening": [i for i in known if RULES[i]["weakens_tests"]],
            "unknown": [i for i in ids if i not in RULES],
            "engineered": origins["engineered"],
            "learned": origins["learned"]}


# ---------------------------------------------------------- log accounting

_ENTRY = re.compile(r"^(.*?): got (.*?), expected (.*)$", re.S)


def _test_wrong_rule(detail):
    """Which trigger a TEST_WRONG verdict stands for, read from its detail text.

    The detail lists at most four failing tests, so this is an approximation:
    near-miss when every listed wrong value is within the near-miss band,
    otherwise the low-confidence rule.
    """
    entries = []
    for part in (detail or "").split("; "):
        m = _ENTRY.match(part.strip())
        if m:
            entries.append((m.group(2), m.group(3)))
    if entries and all(orc.is_near_miss({"got": g, "expected": w}) for g, w in entries):
        return "near-miss-blames-test"
    return "low-confidence-blames-test"


def _events_in(events):
    """One rule id per logged change in one session's events."""
    for e in events:
        kind = e.get("kind")
        if kind == "auto_fix":
            fixes = e.get("fixes")
            if isinstance(fixes, list):
                for f in fixes:
                    if isinstance(f, str):
                        yield f
        elif kind == "rescue":
            yield "rescue-property-tests"
            # the rescue reason is the verdict's class label: identical code is the stuck trigger
            if e.get("reason") == orc.CLASS_LABELS["REPEATED_CANDIDATE"]:
                yield "stuck-result-blames-test"
        elif kind == "test_call_repair":
            yield "test-call-repair"
        elif kind in ("oracle_corrected", "oracle_reference"):
            yield "oracle-reference-value"
        elif kind == "verdict":
            if isinstance(e.get("drift"), list) and e["drift"]:
                yield "expected-drift-blames-test"
            if e.get("class") == "TEST_WRONG":
                yield _test_wrong_rule(e.get("detail"))


def log_summary(sessions_dir=None, mode="live"):
    """Logged repairs per rule over live main-arm builds (one build is one session log).

    MODE is "live" or "demo" to keep only that mode, or anything else for both.
    Only sessions of arm "main" count, as in economics.project_economics.
    """
    directory = Path(sessions_dir) if sessions_dir is not None else economics.SESSIONS_DIR
    try:
        paths = sorted(directory.glob("*.jsonl"))
    except OSError:
        paths = []
    events_of, builds_of = Counter(), Counter()
    unknown = set()
    weakening_builds = 0
    sessions = 0
    weakening = {rid for rid, rec in RULES.items()
                 if rec["weakens_tests"] and _log_kind(rid) in _PRIMARY}
    for path in paths:
        events = economics.read_events(path)
        meta = economics._meta(events)
        if meta["arm"] != "main":
            continue
        if mode in ("live", "demo") and meta["mode"] != mode:
            continue
        sessions += 1
        hit = Counter(_events_in(events))
        for rid, n in hit.items():
            if rid not in RULES:
                unknown.add(rid)
                continue
            events_of[rid] += n
            builds_of[rid] += 1
        if weakening & set(hit):
            weakening_builds += 1
    rules = {}
    for rid, rec in RULES.items():
        row = dict(rec)
        row["events"] = events_of[rid]
        row["builds"] = builds_of[rid]
        row["log"] = _log_kind(rid)
        rules[rid] = row
    primary = [rid for rid in RULES if _log_kind(rid) in _PRIMARY]
    by_effect = {e: 0 for e in EFFECTS}
    by_origin = {o: 0 for o in ORIGINS}
    for rid in primary:
        by_effect[RULES[rid]["effect"]] += events_of[rid]
        by_origin[RULES[rid]["origin"]] += events_of[rid]
    return {
        "scope": {"mode": mode if mode in ("live", "demo") else "all", "arm": "main",
                  "sessions_dir": str(directory), "builds": sessions},
        "rules": rules,
        "by_effect": by_effect,
        "by_origin": by_origin,
        "weakening": {"builds": weakening_builds,
                      "events": sum(events_of[r] for r in weakening)},
        "unknown": sorted(unknown),
    }


# ------------------------------------------------------------------- output

_PARAGRAPH = (
    "What the logs show: the harness applied these repairs and interventions in the "
    "builds counted above, and %(weak_builds)d of %(builds)d builds had at least one "
    "test-weakening intervention. Every entry in the catalogue is engineered: a developer "
    "wrote its code by hand after reading failure logs, and no entry was derived by the "
    "system. The one part the system derives by itself is the lessons ledger "
    "(oracle.LessonStore, counts in artifacts/agent/lessons.json). It counts how often each "
    "hand-written hint key recurs, chooses which hand-written hint texts to show, and flags "
    "hints that do not stop the slip. It writes no new advice and no new repair rule. "
    "lessons.py has refinement and decay code, but agent_session.py does not import it, so "
    "that code is not on the live path. So the logs show that these engineered rules fire "
    "and how often; they do not show that the harness learned them. A higher pass rate after "
    "repair is evidence that the engineered rules help, and nothing in these logs separates "
    "that effect from learning. Two limits apply. The logs do not record which trigger caused "
    "a rescue, so the trigger counts are approximate. Saved tools now carry relaxed_tests, which "
    "records each replaced or dropped expected value, but the session events do not carry that "
    "diff. The reference-value oracle logged %(oracle)d events in these builds."
)


def render(summary):
    """The catalogue grouped by effect, with origins and log counts, then the totals and a paragraph."""
    rules = summary["rules"]
    lines = ["Repair catalogue: %d ids (fix ids and interventions)." % len(rules),
             "Log counts: live main-arm builds (sessions) = %d, mode %s."
             % (summary["scope"]["builds"], summary["scope"]["mode"]), ""]
    for effect in EFFECTS:
        ids = sorted(r for r in rules if rules[r]["effect"] == effect)
        lines.append("%s (%d)" % (effect, len(ids)))
        for rid in ids:
            rec = rules[rid]
            note = " trigger" if rec["log"] == "trigger" else ""
            lines.append("  %-38s %-16s %-10s weakens=%-5s events=%-4d builds=%d%s"
                         % (rid, rec["target"], rec["origin"],
                            "yes" if rec["weakens_tests"] else "no",
                            rec["events"], rec["builds"], note))
            lines.append(textwrap.fill(rec["what"], width=100, initial_indent="      ",
                                       subsequent_indent="      "))
        lines.append("")
    effects = ", ".join("%s %d" % (e, summary["by_effect"][e]) for e in EFFECTS)
    origins = ", ".join("%s %d" % (o, summary["by_origin"][o]) for o in ORIGINS)
    lines.append("Totals (each logged change counted once; trigger rules excluded):")
    lines.append("  by effect: " + effects)
    lines.append("  by origin: " + origins)
    lines.append("  builds with a test-weakening intervention: %d of %d (%d events)"
                 % (summary["weakening"]["builds"], summary["scope"]["builds"],
                    summary["weakening"]["events"]))
    lines.append("  ids seen in the logs but missing from RULES: %s"
                 % (", ".join(summary["unknown"]) or "none"))
    lines.append("")
    lines.append(textwrap.fill(_PARAGRAPH % {
        "weak_builds": summary["weakening"]["builds"],
        "builds": summary["scope"]["builds"],
        "oracle": summary["rules"]["oracle-reference-value"]["events"]}, width=100))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=(
        "List every repair the agent harness can apply, with its effect, origin and log counts."))
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = ap.parse_args(argv)
    summary = log_summary()
    if args.json:
        print(json.dumps(summary, indent=1, sort_keys=True))
    else:
        print(render(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
