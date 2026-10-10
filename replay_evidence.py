"""Replay logged live model replies against their own tests, with and without the harness's repairs.

    uv run python replay_evidence.py [project] [--limit N] [--no-save]

The question: on the same real replies, how often does a function pass its own
tests as the model wrote it (raw), and how often after today's normalize_plan
(fixed)? No model is called. Every reply is read back from the session logs
under artifacts/agent/sessions, and every test runs in SBCL.

Scope and limits, stated up front.
  - Only live main-arm sessions count: the goal event has mode "live" and arm
    "main", read the way economics.py reads them. Demo and no-memory sessions
    are skipped.
  - A case is a logged model reply whose text is a JSON object with action
    "build", a string definition and a non-empty tests list. The reply is read
    strictly: code fences are stripped, the outermost balanced object is parsed
    with json.loads, and nothing is repaired. Replies the harness could read only
    after its own JSON repair are therefore not cases; the notes count them.
  - The callees are today's, not the ones at the time of the reply. The prelude
    is the web kit plus the project's saved live tools as they are now, without
    the case's own name.
  - Each side is one SBCL evaluation of the checker, the prelude, the definition
    and every check. A check that raises counts as a failed test. A side that
    fails validate_build is never run.
"""
import argparse
import copy
import json
import os
import re
import statistics
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import agent_session as ag  # noqa: E402
import economics  # noqa: E402
import projects  # noqa: E402
import webkit  # noqa: E402

AGENT_DIR = ROOT / "artifacts" / "agent"
SESSIONS_DIR = AGENT_DIR / "sessions"
EVIDENCE_PATH = AGENT_DIR / "evidence" / "replay.json"
WORKERS = 4
PROGRESS_EVERY = 25
BROKEN_LISTED = 20
TOP_FIXES = 12
BUCKETS = ("first drafts", "repairs", "other")
STAGES = ("validate", "run", "tests")
_FENCE = re.compile(r"```[A-Za-z0-9_-]*")


# --------------------------------------------------------------------------
# Reading the logged replies
# --------------------------------------------------------------------------

def _balanced_end(text, start):
    """Index of the brace that closes the object opened at START, or -1."""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def parse_reply(text):
    """The JSON object inside a model reply, or None. Strict: no repairs.

    Code fences are stripped. The first balanced {...} is tried first, then the
    span from the first "{" to the last "}". Only a parsed dict is returned.
    """
    if not isinstance(text, str):
        return None
    cleaned = _FENCE.sub("", text)
    start = cleaned.find("{")
    if start < 0:
        return None
    candidates = []
    end = _balanced_end(cleaned, start)
    if end >= 0:
        candidates.append(cleaned[start:end + 1])
    last = cleaned.rfind("}")
    if last > start:
        candidates.append(cleaned[start:last + 1])
    for raw in candidates:
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _is_build_case(plan):
    return (plan.get("action") == "build" and isinstance(plan.get("definition"), str)
            and isinstance(plan.get("tests"), list) and len(plan["tests"]) > 0)


def _scan(sessions_dir=None, mode="live", project=None):
    """Every case in the matching sessions, oldest session first, plus the stats.

    The repair costs cover every repair call of the matching sessions, not only
    the cases, so the median does not depend on which replies parsed.
    """
    directory = Path(sessions_dir) if sessions_dir is not None else SESSIONS_DIR
    try:
        paths = sorted(directory.glob("*.jsonl"))
    except OSError:
        paths = []
    stats = {"sessions": 0, "skipped_sessions": 0, "replies": 0, "not_json": 0,
             "not_build": 0, "unparsed_build_like": 0}
    repair_costs = []
    found = []
    for path in paths:
        events = economics.read_events(path)
        meta = economics._meta(events)
        if meta["arm"] != "main" or (mode and meta["mode"] != mode) or \
                (project and meta["project"] != project):
            stats["skipped_sessions"] += 1
            continue
        stats["sessions"] += 1
        cases = []
        calls, _saves, _marks = economics._pair(events)
        for call in calls:
            if call["cat"] == "repairs":
                repair_costs.append(call["cost"])
            stats["replies"] += 1
            text = events[call["pos"]].get("text")
            plan = parse_reply(text)
            if plan is None:
                stats["not_json"] += 1
                if isinstance(text, str) and '"build"' in text and "definition" in text:
                    stats["unparsed_build_like"] += 1
                continue
            if not _is_build_case(plan):
                stats["not_build"] += 1
                continue
            name = plan.get("name") if isinstance(plan.get("name"), str) else ""
            cases.append({"session": path.stem, "project": meta["project"],
                          "label": call["label"], "kind": call["cat"], "name": name,
                          "plan": plan, "cost_usd": call["cost"]})
        found.append((meta["t"], path.stem, cases))
    found.sort(key=lambda item: (item[0], item[1]))
    flat = [case for _, _, cases in found for case in cases]
    return {"cases": flat, "repair_costs": repair_costs, "stats": stats}


def collect(sessions_dir=None, mode="live", project=None, limit=None):
    """The cases of the matching live main-arm sessions (see the module docstring)."""
    cases = _scan(sessions_dir, mode, project)["cases"]
    return cases if limit is None else cases[:limit]


# --------------------------------------------------------------------------
# Evaluating one side
# --------------------------------------------------------------------------

def _short(text, limit=240):
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[:limit - 3] + "..."


def _side(ok, stage=None, error="", fixes=None):
    text = str(error or "")
    return {"ok": bool(ok), "stage": stage, "error": _short(text),
            "fixes": [str(f) for f in (fixes or [])],
            "undefined": "UNDEFINED-FUNCTION" in text.upper()}


def _top_level(text):
    """The items of a printed Lisp list, split at depth zero, or None if unreadable."""
    s = str(text or "").strip()
    if len(s) < 2 or s[0] != "(" or s[-1] != ")":
        return None
    items, cur, depth, in_str, esc = [], [], 0, False, False
    for ch in s[1:-1]:
        if in_str:
            cur.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch.isspace() and depth == 0:
            if cur:
                items.append("".join(cur))
                cur = []
            continue
        if ch == '"':
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        cur.append(ch)
    if in_str or depth != 0:
        return None
    if cur:
        items.append("".join(cur))
    return items


def _describe(test, result):
    upper = result.upper()
    if upper.startswith("(:GOT "):
        return "%s gave %s, expected %s" % (test["call"], result[6:-1], test["expect"])
    if upper.startswith("(:ERROR "):
        return "%s raised %s" % (test["call"], result[8:-1])
    return "%s gave %s, expected %s" % (test["call"], result, test["expect"])


def _run_tests(plan, prelude, run, fixes):
    """One SBCL evaluation of every test of PLAN; a side passes when every check is T."""
    checks = ["(handler-case (gg-check %s '%s) (error (c) (list :error (type-of c))))"
              % (t["call"], t["expect"]) for t in plan["tests"]]
    code = "%s\n%s\n%s\n(list %s)" % (ag.GG_CHECK, prelude, plan["definition"], " ".join(checks))
    try:
        env = run(code)
    except Exception as exc:
        return _side(False, "run", "the worker raised %s: %s" % (type(exc).__name__, exc), fixes)
    if not isinstance(env, dict):
        return _side(False, "run", "the worker returned no envelope", fixes)
    if not env.get("ok"):
        if env.get("timed_out"):
            return _side(False, "run", "timed out", fixes)
        return _side(False, "run", env.get("error") or "the evaluation failed", fixes)
    results = _top_level(env.get("return_value"))
    if results is None or len(results) != len(checks):
        return _side(False, "tests", "could not read the %d test results" % len(checks), fixes)
    failed = [(t, r) for t, r in zip(plan["tests"], results) if r != "T"]
    if not failed:
        return _side(True, None, "", fixes)
    test, result = failed[0]
    side = _side(False, "tests", "%d of %d checks failed; first: %s"
                 % (len(failed), len(checks), _describe(test, result)), fixes)
    # any failed check that names an undefined function counts, not only the first
    side["undefined"] = side["undefined"] or any(
        "UNDEFINED-FUNCTION" in r.upper() for _, r in failed)
    return side


def _check_side(plan, prelude, run, fixes):
    try:
        problem = ag.validate_build(plan)
    except Exception as exc:
        return _side(False, "validate", "validate_build raised %s: %s"
                     % (type(exc).__name__, exc), fixes)
    if problem:
        return _side(False, "validate", problem, fixes)
    return _run_tests(plan, prelude, run, fixes)


def evaluate(case, prelude, run=None):
    """Both sides of one case: {"raw": side, "fixed": side}. Neither side changes the case.

    RUN takes one code string and returns a worker envelope; it defaults to the
    real SBCL worker, so tests can inject a fake.
    """
    run = run or ag._worker_fn
    plan = case["plan"]
    raw = _check_side(plan, prelude, run, [])
    try:
        work = copy.deepcopy(plan)
        out = ag.normalize_plan(work)
        if isinstance(out, dict):
            work = out
    except Exception as exc:
        fixed = _side(False, "validate", "normalize_plan raised %s: %s"
                      % (type(exc).__name__, exc), [])
    else:
        fixed = _check_side(work, prelude, run, work.get("auto_fixes") or [])
    return {"raw": raw, "fixed": fixed}


# --------------------------------------------------------------------------
# Preludes: the callees each case may use
# --------------------------------------------------------------------------

class _Preludes:
    """The prelude of a (project, function name) pair, built once and cached.

    The web kit comes first, then the project's saved live tools as they are
    now. A tool with the case's own name is left out, and a kit tool that the
    project has its own copy of is replaced by that copy. A prelude that does
    not load falls back to the kit alone; a kit that does not load either gives
    an empty prelude. Each fallback is noted once.
    """

    def __init__(self, agent_dir, run, notes):
        self.agent_dir = Path(agent_dir)
        self.run = run
        self.notes = notes
        self._store = projects.ProjectStore(self.agent_dir)
        self._cache = {}
        self._warned = set()

    def get(self, project, name):
        pid = self._store.resolve(project or projects.BUILTIN)
        key = (pid, name)
        if key not in self._cache:
            self._cache[key] = self._build(pid, name)
        return self._cache[key]

    def _saved(self, pid):
        path = self._store.tools_path(pid)
        registry = ag.ToolRegistry(path, mode="live")
        return [t for t in registry.load() if isinstance(t.get("definition"), str)]

    def _load_error(self, text):
        """None when TEXT loads in SBCL, else a short reason."""
        try:
            env = self.run("%s\n%s\n(list t)" % (ag.GG_CHECK, text))
        except Exception as exc:
            return "%s: %s" % (type(exc).__name__, exc)
        if isinstance(env, dict) and env.get("ok"):
            return None
        if isinstance(env, dict) and env.get("timed_out"):
            return "timed out"
        return _short((env or {}).get("error") or "the load failed", 160)

    def _warn(self, key, text):
        if key not in self._warned:
            self._warned.add(key)
            self.notes.append(text)

    def _build(self, pid, name):
        saved = [t for t in self._saved(pid) if t.get("name") != name]
        have = {t.get("name") for t in saved}
        kit_text = "\n".join(t["definition"] for t in webkit.KIT
                             if t["name"] != name and t["name"] not in have)
        saved_text = "\n".join(t["definition"] for t in saved)
        full = "\n".join(part for part in (kit_text, saved_text) if part)
        error = self._load_error(full)
        if error is None:
            return full
        self._warn(("saved", pid), "The saved tools of project %s do not load (%s); its cases "
                   "use the web kit alone as their prelude." % (pid, error))
        error = self._load_error(kit_text)
        if error is None:
            return kit_text
        self._warn(("kit", pid), "The web kit does not load (%s); the cases of project %s run "
                   "with an empty prelude." % (error, pid))
        return ""


# --------------------------------------------------------------------------
# The replay
# --------------------------------------------------------------------------

def _bucket(kind):
    return kind if kind in ("first drafts", "repairs") else "other"


def _one(index, case, preludes, run):
    try:
        prelude = preludes.get(case["project"], case["name"])
        return index, evaluate(case, prelude, run), False
    except Exception as exc:
        side = _side(False, "run", "replay error: %s: %s" % (type(exc).__name__, exc))
        return index, {"raw": side, "fixed": dict(side)}, True


def _rate(part, whole):
    return round(part / whole, 3) if whole else 0.0


def replay(sessions_dir=None, mode="live", project=None, limit=None, progress=None,
           run=None, agent_dir=None):
    """Replay the cases and return the result dict (see the module docstring for its keys)."""
    started = time.time()
    run = run or ag._worker_fn
    agent_dir = Path(agent_dir) if agent_dir is not None else AGENT_DIR
    scan = _scan(sessions_dir, mode, project)
    cases = scan["cases"] if limit is None else scan["cases"][:limit]
    notes = []
    preludes = _Preludes(agent_dir, run, notes)
    total = len(cases)
    outcomes = [None] * total
    internal = 0
    done = 0
    if total:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = [pool.submit(_one, i, cases[i], preludes, run) for i in range(total)]
            for fut in as_completed(futures):
                index, outcome, failed = fut.result()
                outcomes[index] = outcome
                if failed:
                    internal += 1
                done += 1
                if progress is not None:
                    progress(done, total)

    raw_pass = fixed_pass = rescued = broken = 0
    by_kind = {name: {"cases": 0, "raw_pass": 0, "fixed_pass": 0} for name in BUCKETS}
    raw_fail = {name: 0 for name in STAGES}
    fixed_fail = {name: 0 for name in STAGES}
    fix_stats = {}
    broken_cases = []
    sessions = set()
    for case, res in zip(cases, outcomes):
        raw, fixed = res["raw"], res["fixed"]
        row = by_kind[_bucket(case["kind"])]
        row["cases"] += 1
        sessions.add(case["session"])
        if raw["ok"]:
            raw_pass += 1
            row["raw_pass"] += 1
        else:
            raw_fail[raw["stage"]] = raw_fail.get(raw["stage"], 0) + 1
        if fixed["ok"]:
            fixed_pass += 1
            row["fixed_pass"] += 1
        else:
            fixed_fail[fixed["stage"]] = fixed_fail.get(fixed["stage"], 0) + 1
        for fix in fixed["fixes"]:
            fix_stats.setdefault(fix, {"cases": 0, "rescued": 0})["cases"] += 1
        if not raw["ok"] and fixed["ok"]:
            rescued += 1
            for fix in fixed["fixes"]:
                fix_stats[fix]["rescued"] += 1
        if raw["ok"] and not fixed["ok"]:
            broken += 1
            broken_cases.append({"session": case["session"], "name": case["name"],
                                 "label": case["label"], "error": fixed["error"]})

    by_fix = sorted(({"fix": f, "cases": s["cases"], "rescued": s["rescued"]}
                     for f, s in fix_stats.items() if s["rescued"] > 0),
                    key=lambda r: (-r["rescued"], -r["cases"], r["fix"]))[:TOP_FIXES]
    repair_costs = scan["repair_costs"]
    median_repair = statistics.median(repair_costs) if repair_costs else 0.0
    stats = scan["stats"]
    undefined = {"raw": sum(1 for r in outcomes if r["raw"].get("undefined")),
                 "fixed": sum(1 for r in outcomes if r["fixed"].get("undefined"))}
    if undefined["raw"] or undefined["fixed"]:
        notes.append("%d raw and %d fixed case(s) failed because a function they call was undefined "
                     "in the replay. Some are callees saved live when the reply was made but missing "
                     "from today's prelude; the rest call functions the reply itself did not define."
                     % (undefined["raw"], undefined["fixed"]))
    if internal:
        notes.append("%d case(s) hit an internal error in the replay itself; they count as run "
                     "failures on both sides." % internal)
    notes.append("Scanned %d live main-arm session(s) and skipped %d (demo, no-memory or other "
                 "scope). Read %d model replies: %d build cases, %d not JSON, %d other JSON."
                 % (stats["sessions"], stats["skipped_sessions"], stats["replies"],
                    len(scan["cases"]), stats["not_json"], stats["not_build"]))
    notes.append("%d reply(ies) that look like builds could not be read strictly (no repair "
                 "applied) and are not cases; the harness's own JSON repair may have rescued "
                 "some of them." % stats["unparsed_build_like"])
    if limit is not None:
        notes.append("Limited to the first %d case(s) in session order." % total)
    notes.append("Preludes use TODAY's saved live tools of each project, not the tools that existed "
                 "when the reply was made; a case's own name is left out of its prelude.")
    notes.append("rescued_usd is the rescued count times the median cost of all %d logged repair "
                 "call(s) in the matching sessions ($%.4f)." % (len(repair_costs), median_repair))
    notes.append("Raw and fixed sides both pass through validate_build, as the harness does; the "
                 "fixed side runs normalize_plan on a copy first.")

    return {
        "cases": total,
        "sessions": len(sessions),
        "raw_pass": raw_pass,
        "fixed_pass": fixed_pass,
        "raw_rate": _rate(raw_pass, total),
        "fixed_rate": _rate(fixed_pass, total),
        "rescued": rescued,
        "broken": broken,
        "rescued_usd": round(rescued * median_repair, 4),
        "by_kind": by_kind,
        "by_fix": by_fix,
        "raw_failures": raw_fail,
        "fixed_failures": fixed_fail,
        "undefined_callee": undefined,
        "broken_cases": broken_cases[:BROKEN_LISTED],
        "notes": notes,
        "generated": time.time(),
        "seconds": round(time.time() - started, 1),
    }


# --------------------------------------------------------------------------
# Saving, loading and the command line
# --------------------------------------------------------------------------

def save(result, path=None):
    """Write RESULT as JSON to PATH (default artifacts/agent/evidence/replay.json), atomically."""
    path = Path(path) if path is not None else EVIDENCE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def load(path=None):
    """The saved result dict, or None when the file is missing or unreadable."""
    path = Path(path) if path is not None else EVIDENCE_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def summary_lines(result):
    """A short plain-text report of a result dict, one str per line."""
    total = result["cases"]
    lines = ["Replayed %d live build repl%s from %d session(s) in %.1f s."
             % (total, "y" if total == 1 else "ies", result["sessions"], result["seconds"]),
             "Raw (as the model wrote it): %d of %d pass (%.1f%%). Fixed (after normalize_plan): "
             "%d of %d pass (%.1f%%)."
             % (result["raw_pass"], total, 100 * result["raw_rate"],
                result["fixed_pass"], total, 100 * result["fixed_rate"])]
    for name in BUCKETS:
        row = result["by_kind"][name]
        lines.append("  %s: %d cases, raw %d pass, fixed %d pass"
                     % (name, row["cases"], row["raw_pass"], row["fixed_pass"]))
    lines.append("Rescued by the machinery: %d (about $%.4f at the median repair-call cost). "
                 "Broken by it: %d." % (result["rescued"], result["rescued_usd"], result["broken"]))
    fixes = ", ".join("%s %d" % (f["fix"], f["rescued"]) for f in result["by_fix"]) or "none"
    lines.append("Auto-fixes on rescued cases (rescued count): " + fixes)
    lines.append("Raw failures: validate %d, run %d, tests %d. Fixed failures: validate %d, run %d, "
                 "tests %d."
                 % (result["raw_failures"].get("validate", 0), result["raw_failures"].get("run", 0),
                    result["raw_failures"].get("tests", 0), result["fixed_failures"].get("validate", 0),
                    result["fixed_failures"].get("run", 0), result["fixed_failures"].get("tests", 0)))
    undefined = result.get("undefined_callee") or {}
    lines.append("Failed on an undefined function (a callee missing from the prelude, or never defined): "
                 "raw %d, fixed %d."
                 % (undefined.get("raw", 0), undefined.get("fixed", 0)))
    if result["broken"]:
        lines.append("Broken cases (raw passes, fixed fails):")
        for b in result["broken_cases"]:
            lines.append("  %s | %s | %s | %s" % (b["session"], b["name"], b["label"], b["error"]))
        if result["broken"] > len(result["broken_cases"]):
            lines.append("  ... and %d more" % (result["broken"] - len(result["broken_cases"])))
    lines.append("Notes:")
    lines += ["  - " + note for note in result["notes"]]
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("project", nargs="?", default=None, help="only this project's sessions")
    ap.add_argument("--limit", type=int, default=None, help="replay only the first N cases")
    ap.add_argument("--no-save", action="store_true", help="print the summary only")
    args = ap.parse_args(argv)
    started = time.time()

    def progress(done, total):
        if done % PROGRESS_EVERY == 0 or done == total:
            elapsed = time.time() - started
            eta = elapsed / done * (total - done) if done else 0.0
            print("replayed %d/%d cases, %.0f s elapsed, ETA %.0f s"
                  % (done, total, elapsed, eta), file=sys.stderr, flush=True)

    result = replay(project=args.project, limit=args.limit, progress=progress)
    print("\n".join(summary_lines(result)))
    if not args.no_save:
        print("Saved to %s" % save(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
