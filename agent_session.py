"""Interactive agent session: prompt -> model -> Lisp tool -> REPL rehearsal.

One user prompt becomes one session. The model sees the goal plus the
persistent tool registry (``artifacts/agent/tools.json``) and answers with
JSON, either ``use`` (call an existing tool) or ``build`` (define a new
Lisp tool with tests). Builds go through the real gates -- risk
classification and ``pipeline.run_candidate`` over ``workers.run_lisp`` --
with at most one repair round. A passing build is registered, so later
prompts load it into the REPL prelude and can compose it.

Every step is emitted as an event dict (also appended to a JSONL file) so
a UI can render the session as it happens. Event ``kind`` values: goal,
registry, model_call, model_reply, decision, repl, verdict, repair,
promoted, result, error, done.

Honest limits: each REPL eval is a fresh SBCL process that replays the
registry as a prelude (no long-lived image yet), and worker isolation is
process-level only (documents/adversarial-report.md).
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pipeline
import risk
import s_expr
import workers

ROOT = Path(__file__).resolve().parent
AGENT_DIR = ROOT / "artifacts" / "agent"
MAX_MODEL_CALLS = 5          # per session: quick check + plan + repairs
MAX_REPAIRS = 2
LIVE_SPEND_CAP_USD = 0.15   # per server process; live sessions refuse past it
WORKER_TIMEOUT_S = 15.0
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}$")

SYSTEM_PROMPT = (
    "You are the planner of a self-extending Common Lisp REPL (SBCL). "
    "Answer with ONE JSON object and nothing else. Either reuse a tool:\n"
    '{"action":"use","call":"(tool-name arg ...)","why":"..."}\n'
    "or build a new tool:\n"
    '{"action":"build","name":"kebab-name","description":"one line",'
    '"definition":"(defun kebab-name (...) ...)",'
    '"tests":[{"call":"(kebab-name ...)","expect":"printed result"}],'
    '"call":"(kebab-name ...)"}\n'
    "Rules: pure ANSI Common Lisp only (no I/O, files, network, processes, "
    "reader eval). Existing tools are already loaded and may be called by "
    "new tools. 'expect' is the PRIN1 text of the result, e.g. \"25\" or "
    "\"\\\"abc\\\"\" or \"(1 2 3)\". Give 2-4 tests on inputs whose results you can compute exactly by hand "
    "(Lisp prints exact rationals like 14/3, and integers without .0). 'call' answers the "
    "user's request using the literal data from the goal; quote list "
    "literals, e.g. '(1 2 3)."
)


SHORT_SYSTEM = (
    "Reply with ONE JSON object. If a listed tool answers the goal, reply "
    '{"action":"use","call":"(tool args)"}. Otherwise reply {"action":"none"}. '
    "Use the literal data given in the goal and quote list literals, e.g. "
    "'(1 2 3)."
)

_GENERIC = {"a", "an", "the", "of", "for", "to", "and", "or", "in", "on",
            "write", "make", "function", "list", "number", "numbers",
            "again", "please", "give", "me", "compute", "calculate", "find"}


def _stems(text):
    out = set()
    for w in re.findall(r"[a-z0-9]+", str(text).lower()):
        if w in _GENERIC:
            continue
        out.add(w[:-1] if w.endswith("s") and len(w) > 3 else w)
    return out


def retrieve(prompt, tools):
    """Tools whose name+description share >= 2 content words with PROMPT.

    Cheap local pre-filter: only when it fires do we spend a short
    'use an existing tool?' call before the full build prompt.
    """
    want = _stems(prompt)
    hits = []
    for t in tools:
        have = _stems("%s %s" % (t.get("name", ""), t.get("description", "")))
        if len(want & have) >= 2:
            hits.append(t)
    return hits


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

def normalize_prompt(text):
    return " ".join(re.findall(r"[a-z0-9]+", str(text).lower()))


def signature(definition):
    """``(name (args))`` header of a defun, enough to call it."""
    m = re.match(r"\s*\(defun\s+(\S+)\s+(\([^)]*\))", definition or "")
    return "(%s %s)" % (m.group(1), m.group(2)) if m else "(?)"

class ToolRegistry:
    """Persistent store of promoted Lisp tools (JSON file)."""

    def __init__(self, path=None):
        self.path = Path(path) if path else AGENT_DIR / "tools.json"
        self._lock = threading.Lock()

    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def add(self, tool):
        with self._lock:
            tools = [t for t in self.load() if t.get("name") != tool["name"]]
            tools.append(tool)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(tools, indent=2), encoding="utf-8")
            tmp.replace(self.path)

    def note_use(self, name, prompt, call):
        """Remember that PROMPT was answered by CALL on tool NAME."""
        with self._lock:
            tools = self.load()
            for t in tools:
                if t.get("name") == name:
                    if prompt and call:
                        t["prompts"] = sorted(set(t.get("prompts", []))
                                              | {normalize_prompt(prompt)})
                        t["call"] = call
                    t["uses"] = t.get("uses", 0) + 1
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(tools, indent=2), encoding="utf-8")
            tmp.replace(self.path)

    def find_cached(self, prompt):
        key = normalize_prompt(prompt)
        for t in self.load():
            if key in t.get("prompts", []) and t.get("call"):
                return t
        return None

    def _epoch_path(self):
        return self.path.with_name("epoch.txt")

    def epoch(self):
        """Time of the last reset; history before it is not shown."""
        try:
            return float(self._epoch_path().read_text())
        except (OSError, ValueError):
            return 0.0

    def clear(self):
        with self._lock:
            if self.path.exists():
                self.path.unlink()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._epoch_path().write_text(str(time.time()))

    def prelude(self):
        """Lisp source that defines every registered tool, in order."""
        return "\n".join(t["definition"] for t in self.load())


# --------------------------------------------------------------------------
# Model adapters: generate(system, user) -> {"text", "input_tokens", ...}
# --------------------------------------------------------------------------

def live_status():
    """Whether live mode can run, with a reason when it cannot.

    Never reads or returns the key value, only whether one is present.
    """
    import importlib.util
    import os
    for mod in ("openai", "dotenv"):
        if importlib.util.find_spec(mod) is None:
            return {"available": False, "reason":
                    "Python module '%s' missing: start the dashboard with "
                    "`uv run python dashboard/server.py`" % mod}
    has_key = bool(os.environ.get("CEREBRAS_API_KEY")
                   or os.environ.get("CEREBRAS"))
    if not has_key:
        try:
            for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
                name = line.split("=", 1)[0].strip().upper()
                if name in ("CEREBRAS_API_KEY", "CEREBRAS") and "=" in line \
                        and line.split("=", 1)[1].strip():
                    has_key = True
        except OSError:
            pass
    if not has_key:
        return {"available": False,
                "reason": "no CEREBRAS_API_KEY in the environment or .env"}
    return {"available": True, "reason": "", "model": "qwen-3.8-27b (Cerebras)"}


def live_generate(system, user):
    status = live_status()
    if not status["available"]:
        raise RuntimeError("live mode unavailable: " + status["reason"])
    import cerebras_client
    return cerebras_client.generate(user, system=system, max_tokens=1200,
                                    temperature=0.0, reasoning_effort="none")


# Extra scripted examples for the demo model. Each entry fires when ALL
# `words` appear in the goal and none of `not_words` do; `needs` names a
# saved tool it can compose (else a standalone `alt` definition is built).
DEMO_TOOLS = [
    {"name": "factorial", "words": ["factorial"], "not_words": [],
     "desc": "Factorial of a non-negative integer",
     "definition": "(defun factorial (n) (if (<= n 1) 1 (* n (factorial (- n 1)))))",
     "tests": [("(factorial 5)", "120"), ("(factorial 0)", "1")],
     "call": "(factorial 6)"},
    {"name": "palindrome-p", "words": ["palindrome"], "not_words": [],
     "desc": "True when a string reads the same backwards",
     "definition": "(defun palindrome-p (s) (string= s (reverse s)))",
     "tests": [('(palindrome-p "racecar")', "T"), ('(palindrome-p "abc")', "NIL")],
     "call": '(palindrome-p "level")'},
    {"name": "list-max", "words": ["largest"], "not_words": [],
     "desc": "Largest number in a list",
     "definition": "(defun list-max (xs) (reduce #'max xs))",
     "tests": [("(list-max '(3 9 2))", "9"), ("(list-max '(-5 -2))", "-2")],
     "call": "(list-max '(4 17 8))"},
    {"name": "count-evens", "words": ["count", "even"], "not_words": [],
     "desc": "Count the even numbers in a list",
     "needs": "is-even",
     "definition": "(defun count-evens (xs) (length (remove-if-not #'is-even xs)))",
     "alt": "(defun count-evens (xs) (count-if #'evenp xs))",
     "tests": [("(count-evens '(1 2 3 4))", "2"), ("(count-evens nil)", "0")],
     "call": "(count-evens '(1 2 3 4 6))"},
    {"name": "is-even", "words": ["even"], "not_words": ["count"],
     "desc": "True when an integer is even",
     "definition": "(defun is-even (n) (evenp n))",
     "tests": [("(is-even 10)", "T"), ("(is-even 7)", "NIL")],
     "call": "(is-even 10)"},
]


def _demo_example(goal, have, build, user):
    """Scripted build/use for the DEMO_TOOLS table, else None."""
    for ex in DEMO_TOOLS:
        if not all(w in goal for w in ex["words"]):
            continue
        if any(w in goal for w in ex["not_words"]):
            continue
        if ex["name"] in have:
            return _fake({"action": "use", "call": ex["call"],
                          "why": "existing tool covers it"}, user)
        definition = ex["definition"]
        if ex.get("needs") and ex["needs"] not in have:
            definition = ex["alt"]
        build.update(
            name=ex["name"], description=ex["desc"], definition=definition,
            tests=[{"call": c, "expect": e} for c, e in ex["tests"]],
            call=ex["call"])
        return _fake(build, user)
    return None


def _demo_short(goal, have):
    """Quick-reuse answer for DEMO_TOOLS (tool already saved), else None."""
    for ex in DEMO_TOOLS:
        if ex["name"] in have and all(w in goal for w in ex["words"]) \
                and not any(w in goal for w in ex["not_words"]):
            return ex["call"]
    return None


def demo_generate(system, user):
    """Offline scripted model: canned tools, then composition/reuse."""
    goal = user.split("GOAL:", 1)[-1].split("\n", 1)[0].lower()
    have = set(re.findall(r"^TOOL (\S+)", user, re.M))
    if system == SHORT_SYSTEM:
        call = None
        if "sum-of-squares" in have and "sum" in goal and "square" in goal:
            call = "(sum-of-squares '(3 4 5))"
        elif "mean-of-squares" in have and "mean" in goal and "square" in goal:
            call = "(mean-of-squares '(2 4 6 8))"
        elif "square" in have and "square" in goal and "sum" not in goal \
                and "mean" not in goal:
            call = "(square 12)"
        elif "reverse-words" in have and "reverse" in goal:
            call = '(reverse-words "one two three")'
        elif _demo_short(goal, have):
            call = _demo_short(goal, have)
        if call:
            return _fake({"action": "use", "call": call}, user, system)
        return _fake({"action": "none"}, user, system)
    build = {"action": "build"}
    if "square" in goal and "sum" in goal:
        if "square" in have and "sum-of-squares" not in have:
            build.update(
                name="sum-of-squares",
                description="Sum of squares of a list of numbers",
                definition="(defun sum-of-squares (xs) "
                           "(reduce #'+ (mapcar #'square xs)))",
                tests=[{"call": "(sum-of-squares '(1 2 3))", "expect": "14"},
                       {"call": "(sum-of-squares nil)", "expect": "0"}],
                call="(sum-of-squares '(3 4 5))")
            return _fake(build, user)
        if "sum-of-squares" in have:
            return _fake({"action": "use", "call": "(sum-of-squares '(3 4 5))",
                          "why": "existing tool covers it"}, user)
    if "cube" in goal:
        fixed = "PREVIOUS ATTEMPT FAILED" in user
        build.update(
            name="cube", description="Cube a number",
            definition="(defun cube (x) (* x x x))" if fixed
            else "(defun cube (x) (* x x))",
            tests=[{"call": "(cube 3)", "expect": "27"},
                   {"call": "(cube -2)", "expect": "-8"}],
            call="(cube 4)")
        return _fake(build, user)
    if "mean" in goal and "square" in goal:
        if "mean-of-squares" in have:
            return _fake({"action": "use", "call": "(mean-of-squares '(2 4 6 8))",
                          "why": "existing tool covers it"}, user)
        if "sum-of-squares" in have:
            build.update(
                name="mean-of-squares",
                description="Mean of squares of a list of numbers",
                definition="(defun mean-of-squares (xs) "
                           "(/ (sum-of-squares xs) (length xs)))",
                tests=[{"call": "(mean-of-squares '(2 4))", "expect": "10"},
                       {"call": "(mean-of-squares '(3))", "expect": "9"}],
                call="(mean-of-squares '(2 4 6 8))")
        else:
            build.update(
                name="mean-of-squares",
                description="Mean of squares of a list of numbers",
                definition="(defun mean-of-squares (xs) (/ (reduce #'+ "
                           "(mapcar (lambda (x) (* x x)) xs)) (length xs)))",
                tests=[{"call": "(mean-of-squares '(2 4))", "expect": "10"},
                       {"call": "(mean-of-squares '(3))", "expect": "9"}],
                call="(mean-of-squares '(2 4 6 8))")
        return _fake(build, user)
    if "square" in goal and "sum" in goal and "square" not in have:
        build.update(
            name="sum-of-squares",
            description="Sum of squares of a list of numbers",
            definition="(defun sum-of-squares (xs) "
                       "(reduce #'+ (mapcar (lambda (x) (* x x)) xs)))",
            tests=[{"call": "(sum-of-squares '(1 2 3))", "expect": "14"},
                   {"call": "(sum-of-squares nil)", "expect": "0"}],
            call="(sum-of-squares '(3 4 5))")
        return _fake(build, user)
    if "square" in goal:
        if "square" in have:
            return _fake({"action": "use", "call": "(square 12)",
                          "why": "existing tool covers it"}, user)
        build.update(
            name="square", description="Square a number",
            definition="(defun square (x) (* x x))",
            tests=[{"call": "(square 5)", "expect": "25"},
                   {"call": "(square -3)", "expect": "9"}],
            call="(square 12)")
        return _fake(build, user)
    if "reverse" in goal:
        if "reverse-words" in have:
            return _fake({"action": "use",
                          "call": '(reverse-words "one two three")',
                          "why": "existing tool covers it"}, user)
        build.update(
            name="reverse-words", description="Reverse word order in a string",
            definition=(
                "(defun reverse-words (s) (let ((words nil) (start 0)) "
                "(loop for i from 0 to (length s) do "
                "(when (or (= i (length s)) (char= (char s i) #\\Space)) "
                "(when (> i start) (push (subseq s start i) words)) "
                "(setf start (1+ i)))) "
                "(format nil \"~{~a~^ ~}\" words)))"),
            tests=[{"call": '(reverse-words "a b c")', "expect": '"c b a"'}],
            call='(reverse-words "one two three")')
        return _fake(build, user)
    scripted = _demo_example(goal, have, build, user)
    if scripted:
        return scripted
    raise RuntimeError(
        "The demo model only knows the example chips (square, sum/mean of "
        "squares, cube, reverse words, factorial, palindrome, largest, "
        "even numbers). Switch to Live for any other prompt.")


def _fake(obj, user="", system=None):
    text = json.dumps(obj)
    # Demo mode has no tokenizer: estimate ~4 chars/token so the token
    # chart is meaningful. The UI labels these as estimates.
    system = SYSTEM_PROMPT if system is None else system
    return {"text": text, "input_tokens": (len(system) + len(user)) // 4,
            "output_tokens": len(text) // 4, "estimated": True,
            "cost_usd": 0.0, "latency_ms": 0.0, "model": "demo-script",
            "request_id": None, "finish_reason": "stop"}


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------

def _worker_fn(code):
    return workers.run_lisp(code, timeout_s=WORKER_TIMEOUT_S)


def _risk_fn(parsed):
    return risk.classify(parsed, {"effects": ["pure"]})


def quote_literals(text):
    """Quote bare numeric-led lists such as ``(3 4 5)`` -> ``'(3 4 5)``.

    Real models often write list data without the quote, which Lisp reads
    as a call of the number 3. Lists already inside ``'(...)`` or
    ``(quote ...)`` and strings are left alone.
    """
    out, i, n = [], 0, len(text)
    depth, qdepth = 0, None
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
            continue
        if c == "(":
            rest = text[i + 1:].lstrip()
            if out and out[-1].endswith("'") and qdepth is None:
                qdepth = depth
            elif qdepth is None and text.startswith("(quote ", i):
                qdepth = depth
            elif qdepth is None and re.match(r"-?\d", rest):
                out.append("'")
                qdepth = depth
            depth += 1
        elif c == ")":
            depth -= 1
            if qdepth is not None and depth == qdepth:
                qdepth = None
        out.append(c)
        i += 1
    return "".join(out)


def complete_parens(text, max_missing=3):
    """Append up to MAX_MISSING ``)`` when TEXT has unclosed ``(``.

    Models often drop a trailing paren and cannot find it when asked to
    repair. Returns TEXT unchanged if balanced, over-closed, or too far off.
    Strings and ``;`` comments are skipped. The tests still judge behaviour.
    """
    depth, i, n = 0, 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == ";":
            while i < n and text[i] != "\n":
                i += 1
        elif c == "#" and text[i + 1:i + 2] == "\\":
            i += 2
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth < 0:
                return text
        i += 1
    if 0 < depth <= max_missing:
        return text + ")" * depth
    return text


def normalize_plan(plan):
    """Apply quote_literals to every call in a build/use plan, in place."""
    if isinstance(plan.get("definition"), str):
        plan["definition"] = complete_parens(plan["definition"])
    if isinstance(plan.get("call"), str):
        plan["call"] = quote_literals(plan["call"])
    for t in plan.get("tests") or []:
        if isinstance(t, dict) and isinstance(t.get("call"), str):
            t["call"] = quote_literals(t["call"])
    return plan


def extract_json(text):
    """Pull the first balanced JSON object out of model text."""
    if not isinstance(text, str):
        raise ValueError("empty model reply")
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in reply")
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
                return json.loads(text[start:i + 1])
    raise ValueError("unterminated JSON object")


def validate_build(plan):
    """Return an error string for a malformed build plan, else None."""
    name = plan.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return "name must be lowercase kebab-case"
    definition = plan.get("definition")
    if not isinstance(definition, str) or not definition.strip():
        return "definition missing"
    tests = plan.get("tests")
    if not isinstance(tests, list) or not tests:
        return "at least one test required"
    for t in tests:
        if not (isinstance(t, dict) and isinstance(t.get("call"), str)
                and isinstance(t.get("expect"), str)):
            return "each test needs string 'call' and 'expect'"
    if not isinstance(plan.get("call"), str):
        return "call missing"
    try:
        form = s_expr.parse(definition)
    except s_expr.SExprError as exc:
        return "definition does not parse as one form: %s" % exc
    if not isinstance(form, list) or not form or form[0] != "defun":
        return "definition must be a single (defun ...) form"
    return None


def candidate_text(plan):
    return ("(candidate (:target %s) (:parent 0) (:definition (%s)))"
            % (plan["name"], plan["definition"]))


class Session:
    """One prompt's lifecycle. Thread-safe event list for polling."""

    def __init__(self, prompt, generate, registry=None, worker_fn=None,
                 risk_fn=None, session_id=None, mode="demo", log_path=None,
                 arm="main", pair=None, expected=None, oracle=None):
        self.id = session_id or uuid.uuid4().hex[:10]
        self.prompt = prompt
        self.generate = generate
        self.registry = registry or ToolRegistry()
        self.worker_fn = worker_fn or _worker_fn
        self.risk_fn = risk_fn or _risk_fn
        self.mode = mode
        self.arm = arm
        self.expected = expected
        self.oracle = oracle
        self.pair = pair
        self.compare = None  # None | 'running' | 'done' (main arm only)
        self.events = []
        self.state = "running"
        self.model_calls = 0
        self.cost_usd = 0.0
        self.input_tokens = 0
        self.output_tokens = 0
        self._lock = threading.Lock()
        self._log_path = Path(log_path) if log_path else \
            AGENT_DIR / "sessions" / ("%s.jsonl" % self.id)

    # -- events ---------------------------------------------------------
    def emit(self, kind, **data):
        with self._lock:
            ev = {"i": len(self.events), "t": round(time.time(), 3),
                  "kind": kind, **data}
            self.events.append(ev)
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev) + "\n")
        except OSError:
            pass
        return ev

    def snapshot(self, since=0):
        with self._lock:
            return {"session_id": self.id, "state": self.state,
                    "mode": self.mode, "model_calls": self.model_calls,
                    "compare": self.compare, "arm": self.arm,
                    "cost_usd": round(self.cost_usd, 6),
                    "input_tokens": self.input_tokens,
                    "output_tokens": self.output_tokens,
                    "events": self.events[since:], "next": len(self.events)}

    # -- model ----------------------------------------------------------
    def _ask(self, user_text, label, system=None):
        if self.model_calls >= MAX_MODEL_CALLS:
            raise RuntimeError("model call budget (%d) exhausted"
                               % MAX_MODEL_CALLS)
        self.model_calls += 1
        self.emit("model_call", label=label, prompt=user_text)
        res = self.generate(system or SYSTEM_PROMPT, user_text)
        self.cost_usd += res.get("cost_usd") or 0.0
        self.input_tokens += res.get("input_tokens") or 0
        self.output_tokens += res.get("output_tokens") or 0
        self.emit("model_reply", text=res.get("text"),
                  model=res.get("model"),
                  input_tokens=res.get("input_tokens"),
                  output_tokens=res.get("output_tokens"),
                  estimated=bool(res.get("estimated")),
                  cost_usd=res.get("cost_usd"),
                  latency_ms=res.get("latency_ms"))
        return normalize_plan(extract_json(res.get("text")))

    def _user_prompt(self, extra=""):
        lines = []
        for t in self.registry.load():
            lines.append("TOOL %s %s: %s" % (t["name"],
                         signature(t["definition"]).split(" ", 1)[1][:-1],
                         t.get("description", "")))
        reg = "\n".join(lines) or "(no tools yet)"
        return "REGISTRY:\n%s\nGOAL: %s\n%s" % (reg, self.prompt, extra)

    # -- REPL -----------------------------------------------------------
    def _repl(self, code, label):
        env = self.worker_fn(code)
        self.emit("repl", label=label, code=code, ok=env.get("ok"),
                  value=env.get("return_value"), stdout=env.get("stdout"),
                  error=(env.get("error") or "")[:600],
                  elapsed_ms=env.get("elapsed_ms"))
        return env

    # -- main -----------------------------------------------------------
    def run(self):
        try:
            self._run()
        except Exception as exc:  # noqa: BLE001 - surface, never hang
            self.emit("error", message="%s: %s" % (type(exc).__name__, exc))
            self.state = "error"
        else:
            if self.state == "running":
                self.state = "done"
        self.emit("done", state=self.state, model_calls=self.model_calls,
                  input_tokens=self.input_tokens,
                  output_tokens=self.output_tokens,
                  cost_usd=round(self.cost_usd, 6))

    def _run(self):
        tools = self.registry.load()
        self.emit("goal", prompt=self.prompt, mode=self.mode, arm=self.arm,
                  pair=self.pair, expected=self.expected,
                  oracle=self.oracle)
        self.emit("registry", tools=[t["name"] for t in tools])
        cached = self.registry.find_cached(self.prompt)
        if cached:
            self.emit("decision", action="cache", plan={
                "why": "this exact prompt was solved before by tool '%s': "
                       "re-running it with zero model tokens" % cached["name"],
                "call": cached["call"]})
            worked = self._finish_call(cached["call"], self.registry.prelude())
            if worked:
                self.registry.note_use(cached["name"], self.prompt, worked)
            return
        hits = retrieve(self.prompt, tools)
        if hits:
            lines = ["TOOL %s %s: %s" % (
                t["name"], signature(t["definition"]).split(" ", 1)[1][:-1],
                t.get("description", "")) for t in hits]
            self.emit("retrieval", tools=[t["name"] for t in hits])
            quick = self._ask("%s\nGOAL: %s" % ("\n".join(lines), self.prompt),
                              "quick-reuse", system=SHORT_SYSTEM)
            if quick.get("action") == "use" and quick.get("call"):
                self.emit("decision", action="use", plan={
                    "why": "a saved tool matched; cheap reuse check hit",
                    "call": quick["call"]})
                worked = self._finish_call(quick["call"],
                                           self.registry.prelude())
                m = re.match(r"\s*\(\s*(\S+)", worked or "")
                if m:
                    self.registry.note_use(m.group(1), self.prompt, worked)
                return
            self.emit("retrieval_miss", tools=[t["name"] for t in hits])
        plan = self._ask(self._user_prompt(), "plan")
        prelude = self.registry.prelude()
        action = plan.get("action")
        self.emit("decision", action=action, plan=plan)
        if action == "use":
            worked = self._finish_call(plan.get("call"), prelude)
            m = re.match(r"\s*\(\s*(\S+)", worked or "")
            if m:
                self.registry.note_use(m.group(1), self.prompt, worked)
            return
        if action != "build":
            raise ValueError("model returned unknown action %r" % (action,))
        for attempt in range(MAX_REPAIRS + 1):
            problem = validate_build(plan)
            if problem:
                verdict = {"ok": False, "reason": problem, "stage": "schema"}
                self.emit("verdict", **verdict)
            else:
                verdict = self._rehearse(plan, prelude)
            if verdict["ok"]:
                self.registry.add({
                    "name": plan["name"],
                    "description": plan.get("description", ""),
                    "definition": plan["definition"],
                    "tests": plan["tests"], "session": self.id,
                    "prompts": [normalize_prompt(self.prompt)],
                    "call": plan["call"],
                    "created": round(time.time(), 3)})
                for dep in self.registry.load():
                    if dep["name"] != plan["name"] and re.search(
                            r"[\s(']%s[\s)]" % re.escape(dep["name"]),
                            plan["definition"]):
                        self.registry.note_use(dep["name"], "", "")
                self.emit("promoted", name=plan["name"],
                          tools=[t["name"] for t in self.registry.load()])
                self._finish_call(plan["call"],
                                  self.registry.prelude())
                return
            if attempt >= MAX_REPAIRS:
                self.state = "failed"
                return
            self.emit("repair", attempt=attempt + 1, reason=verdict["reason"])
            plan = self._ask(self._user_prompt(
                "PREVIOUS ATTEMPT FAILED (%s): %s\nFailing tests: %s\n"
                "Either the code or the expected value may be wrong: "
                "recompute by hand before deciding. Lisp prints exact "
                "rationals (14/3) and integers without .0.\n"
                "Previous JSON: %s\nReturn corrected build JSON."
                % (verdict.get("stage"), verdict["reason"],
                   verdict.get("detail") or "n/a",
                   json.dumps(plan))), "repair")
            self.emit("decision", action=plan.get("action"), plan=plan)
            if plan.get("action") != "build":
                self.state = "failed"
                return

    def _rehearse(self, plan, prelude):
        direct = []
        for t in plan["tests"]:
            code = "%s\n%s\n%s" % (prelude, plan["definition"], t["call"])
            direct.append({"code": code, "expect": t["expect"]})
        res = pipeline.run_candidate(
            candidate_text(plan), tests={"direct": direct},
            worker_fn=self._recording_worker(plan, prelude),
            risk_fn=self.risk_fn)
        stage_names = [(s["name"], s["status"]) for s in res.get("stages", [])]
        v = res.get("verdict", {})
        detail = []
        items = ((res.get("evidence") or {}).get("direct") or {}).get("items") or []
        for item in items:
            if isinstance(item, dict) and not item.get("pass", True):
                idx = item.get("index")
                call = plan["tests"][idx]["call"] if isinstance(idx, int) \
                    and 0 <= idx < len(plan["tests"]) else "?"
                got = item.get("return_value")
                msg = ("got %s, expected %s" % (got, plan["tests"][idx]["expect"])
                       if got not in (None, "") and isinstance(idx, int)
                       else (item.get("error") or "failed")[:120])
                detail.append("%s: %s" % (call, msg))
        out = {"ok": bool(res.get("ok")), "reason": v.get("reason"),
               "detail": "; ".join(detail[:4]),
               "tests": len(plan["tests"]),
               "stage": v.get("failed_stage"),
               "risk": (res.get("risk") or {}).get("level"),
               "stages": stage_names}
        self.emit("verdict", **out)
        return out

    def _recording_worker(self, plan, prelude):
        """Worker wrapper that logs each rehearsal eval as a REPL line."""
        def run(code):
            env = self.worker_fn(code)
            shown = code
            if prelude and shown.startswith(prelude):
                shown = shown[len(prelude):].lstrip("\n")
            self.emit("repl", label="rehearse", code=shown, ok=env.get("ok"),
                      value=env.get("return_value"),
                      stdout=env.get("stdout"),
                      error=(env.get("error") or "")[:600],
                      elapsed_ms=env.get("elapsed_ms"))
            return env
        return run

    def _finish_call(self, call, prelude, retry=True):
        if not isinstance(call, str) or not call.strip():
            raise ValueError("no call to evaluate")
        call = quote_literals(call)
        names = {t["name"] for t in self.registry.load()}
        problem = safe_call_check(call, names)
        if problem:
            env = {"ok": False, "return_value": "", "stdout": "",
                   "error": "not a call of a saved tool: %s" % problem,
                   "elapsed_ms": 0}
            self.emit("repl", label="answer", code=call, ok=False, value="",
                      stdout="", error=env["error"], elapsed_ms=0)
        else:
            env = self._repl("%s\n%s" % (prelude, call) if prelude else call,
                             "answer")
        self.emit("result", call=call, ok=env.get("ok"),
                  value=env.get("return_value"),
                  expected_ok=(None if self.expected is None else
                               env.get("return_value") == self.expected),
                  error=(env.get("error") or "")[:600])
        if env.get("ok"):
            return call
        if retry and self.model_calls < MAX_MODEL_CALLS:
            reason = (env.get("error") or "call failed").split("\n")[0]
            self.emit("repair", attempt=1, reason="answer call failed: %s"
                      % reason[:160])
            plan = self._ask(self._user_prompt(
                "YOUR CALL FAILED: %s\nCall was: %s\nReturn JSON "
                '{"action":"use","call":"..."} with a corrected call '
                "(literal data from the goal, quoted lists)."
                % (reason[:200], call)), "repair-call")
            fixed = plan.get("call")
            if isinstance(fixed, str) and fixed.strip():
                return self._finish_call(fixed, prelude, retry=False)
        self.state = "failed"


def heldout_tasks():
    """Prompts that are NOT in the guided demo, with answers computed by
    independent Python (not the model, not the demo script)."""
    import math
    digits, word, rev = "98765", "education", "stressed"
    sentence = "the quick brown fox jumps"
    triangular = 20 * 21 // 2
    nums = [7, 3, 9, 4]
    evens = [1, 2, 3, 4, 5, 6]
    prod = 1
    for v in (2, 3, 4, 5):
        prod *= v
    divisors = sum(1 for d in range(1, 37) if 36 % d == 0)
    return [
        {"prompt": "write a function that returns the sum of the digits of a "
                   "number, then compute it for %s" % digits,
         "expected": str(sum(int(d) for d in digits)),
         "oracle": "python: sum of decimal digits"},
        {"prompt": "write a function that counts the vowels in a string, then "
                   "count them in the word %s" % word,
         "expected": str(sum(1 for ch in word if ch in "aeiou")),
         "oracle": "python: vowel count"},
        {"prompt": "write a function that returns the greatest common divisor "
                   "of two numbers, then compute it for 84 and 36",
         "expected": str(math.gcd(84, 36)),
         "oracle": "python: math.gcd"},
        {"prompt": "write a function that reverses a string, then reverse the "
                   "word %s" % rev,
         "expected": '"%s"' % rev[::-1],
         "oracle": "python: string slice [::-1]"},
        {"prompt": "write a function that returns the number of divisors of a "
                   "number, then compute it for 36",
         "expected": str(divisors),
         "oracle": "python: divisor count"},
        {"prompt": "write a function that returns the product of the numbers "
                   "in a list, then use it on (2 3 4 5)",
         "expected": str(prod),
         "oracle": "python: product"},
        {"prompt": "write a function that returns the second largest number "
                   "in a list, then use it on (%s)" % " ".join(map(str, nums)),
         "expected": str(sorted(nums)[-2]),
         "oracle": "python: sorted()[-2]"},
        {"prompt": "write a function that returns the n-th triangular number, "
                   "then compute it for n = 20",
         "expected": str(triangular),
         "oracle": "python: n(n+1)/2"},
        {"prompt": "write a function that counts the words in a sentence, "
                   "then count them in: %s" % sentence,
         "expected": str(len(sentence.split())),
         "oracle": "python: str.split"},
        {"prompt": "write a function that returns the sum of the even numbers "
                   "in a list, then use it on (%s)" % " ".join(map(str, evens)),
         "expected": str(sum(v for v in evens if v % 2 == 0)),
         "oracle": "python: filtered sum"},
    ]


_CALL_OK_HEADS = {"quote", "list", "+", "-", "*", "/", "length", "reverse"}


def safe_call_check(text, tool_names):
    """Return an error string if TEXT is not a plain call of a saved tool.

    Allowed: numbers, strings, nil/t, quoted data, a small arithmetic and
    list vocabulary, and calls of saved tools. Nothing else is evaluated.
    """
    if not isinstance(text, str) or not text.strip() or len(text) > 400:
        return "enter one call such as (square 7)"
    try:
        form = s_expr.parse(text)
    except s_expr.SExprError as exc:
        return "could not parse: %s" % exc
    if not isinstance(form, list) or not form:
        return "enter one call such as (square 7)"
    if form[0] not in tool_names:
        return "the call must start with a saved tool: %s" % (
            ", ".join(sorted(tool_names)) or "none saved yet")
    allowed = set(tool_names) | _CALL_OK_HEADS | {"nil", "t"}

    def walk(node):
        if isinstance(node, list):
            for child in node:
                bad = walk(child)
                if bad:
                    return bad
            return None
        if isinstance(node, bool):
            return None
        if isinstance(node, (int, float, s_expr.SString)):
            return None
        if isinstance(node, str):
            if node.startswith(":") or node.lower() in allowed:
                return None
            return "symbol %r is not allowed here" % node
        return "unsupported value"

    return walk(form)


def row_from_log(path):
    """Summarize one session JSONL log into a history row (or None)."""
    row = {"session_id": Path(path).stem, "prompt": "", "action": None,
           "model_calls": 0, "cost_usd": 0.0, "promoted": None,
           "input_tokens": 0, "output_tokens": 0, "estimated": False,
           "tests_passed": 0, "tests_failed": 0, "arm": "main",
           "pair": None, "state": "running", "result": None, "repairs": 0}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        k = ev.get("kind")
        if k == "goal":
            row["prompt"] = ev.get("prompt", "")
            row["t"] = ev.get("t", 0)
            row["arm"] = ev.get("arm", "main")
            row["pair"] = ev.get("pair")
            row["expected"] = ev.get("expected")
            row["oracle"] = ev.get("oracle")
            row["mode"] = ev.get("mode")
        elif k == "model_call":
            row["model_calls"] += 1
        elif k == "model_reply":
            row["cost_usd"] += ev.get("cost_usd") or 0.0
            row["input_tokens"] += ev.get("input_tokens") or 0
            row["output_tokens"] += ev.get("output_tokens") or 0
            row["estimated"] = row["estimated"] or bool(ev.get("estimated"))
        elif k == "decision" and row["action"] is None:
            row["action"] = ev.get("action")
        elif k == "verdict" and ev.get("tests"):
            row["tests_passed" if ev.get("ok") else "tests_failed"] += ev["tests"]
        elif k == "repair":
            row["repairs"] += 1
        elif k == "promoted":
            row["promoted"] = ev.get("name")
        elif k == "result":
            row["call"] = ev.get("call")
            m = re.match(r"\s*\(\s*(\S+)", ev.get("call") or "")
            row["tool"] = m.group(1) if m else None
            row["result"] = ev.get("value")
            row["expected_ok"] = ev.get("expected_ok")
        elif k == "done":
            row["state"] = ev.get("state")
    return row


class SessionManager:
    """Runs sessions on background threads; one at a time."""

    def __init__(self, registry=None, generators=None):
        self.registry = registry or ToolRegistry()
        self.generators = generators or {"demo": demo_generate,
                                         "live": live_generate}
        self._sessions = {}
        self._busy = False
        self.live_spend = 0.0
        self._lock = threading.Lock()

    def start(self, prompt, mode="demo", compare=False, expected=None,
              oracle=None):
        """Returns (session_id, None) or (None, error).

        ``compare`` also runs the same prompt against an empty, throwaway
        registry afterwards (the no-memory arm) so savings and answer
        agreement are measured rather than estimated.
        """
        if not isinstance(prompt, str) or not prompt.strip():
            return None, "empty prompt"
        if len(prompt) > 2000:
            return None, "prompt too long"
        if mode not in self.generators:
            return None, "unknown mode"
        if mode == "live" and self.live_spend >= LIVE_SPEND_CAP_USD:
            return None, ("live spend cap reached ($%.2f); restart the "
                          "dashboard to reset it" % LIVE_SPEND_CAP_USD)
        with self._lock:
            if self._busy:
                return None, "busy"
            self._busy = True
            sess = Session(prompt.strip(), self.generators[mode],
                           registry=self.registry, mode=mode,
                           expected=expected, oracle=oracle)
            sess.compare = "running" if compare else None
            self._sessions[sess.id] = sess
        threading.Thread(target=self._chain, args=(sess, compare),
                         daemon=True).start()
        return sess.id, None

    def _chain(self, sess, compare):
        try:
            sess.run()
            if sess.mode == "live":
                self.live_spend += sess.cost_usd
            if compare:
                scratch = tempfile.mkdtemp(prefix="gg-nomem-")
                try:
                    twin = Session(
                        sess.prompt, sess.generate,
                        registry=ToolRegistry(Path(scratch) / "tools.json"),
                        mode=sess.mode, arm="nomem", pair=sess.id,
                        expected=sess.expected, oracle=sess.oracle)
                    twin.run()
                    self.live_spend += twin.cost_usd if sess.mode == "live" else 0.0
                finally:
                    shutil.rmtree(scratch, ignore_errors=True)
                sess.compare = "done"
        finally:
            with self._lock:
                self._busy = False

    # -- evidence snapshots -------------------------------------------
    def snapshots(self):
        """Shipped recordings (dashboard/evidence) plus user pins."""
        out = []
        for folder, kind in ((ROOT / "dashboard" / "evidence", "recorded"),
                             (AGENT_DIR / "snapshots", "pinned")):
            for path in sorted(folder.glob("*.json")):
                try:
                    snap = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(snap, dict) and isinstance(snap.get("rows"), list):
                    snap["id"] = "%s:%s" % (kind, path.stem)
                    snap["kind"] = kind
                    out.append(snap)
        return out

    def pin(self, label):
        """Save the current (post-reset) history as a named snapshot."""
        label = (label or "").strip()[:80] or "pinned run"
        rows = self.history(500)
        if not rows:
            return None, "nothing to pin yet"
        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "run"
        folder = AGENT_DIR / "snapshots"
        folder.mkdir(parents=True, exist_ok=True)
        modes = {r.get("mode") for r in rows}
        snap = {"label": label, "created": round(time.time(), 3),
                "mode": "live" if "live" in modes else "demo",
                "estimated": any(r.get("estimated") for r in rows),
                "rows": rows}
        (folder / ("%s.json" % slug)).write_text(json.dumps(snap, indent=1),
                                                 encoding="utf-8")
        return slug, None

    def get(self, session_id, since=0):
        sess = self._sessions.get(session_id)
        return sess.snapshot(since) if sess else None

    def call_tool(self, text):
        """Run one allow-listed call of a saved tool. Zero model tokens."""
        names = {t["name"] for t in self.registry.load()}
        text = quote_literals(text) if isinstance(text, str) else text
        problem = safe_call_check(text, names)
        if problem:
            return {"ok": False, "error": problem, "tokens": 0}
        prelude = self.registry.prelude()
        env = _worker_fn("%s\n%s" % (prelude, text))
        return {"ok": bool(env.get("ok")), "value": env.get("return_value"),
                "error": (env.get("error") or "")[:300],
                "elapsed_ms": env.get("elapsed_ms"), "tokens": 0}

    def history(self, limit=40):
        """Per-session summaries from the JSONL logs, oldest first."""
        out = []
        files = sorted((AGENT_DIR / "sessions").glob("*.jsonl"),
                       key=lambda p: p.stat().st_mtime)[-limit:]
        for path in files:
            row = row_from_log(path)
            if row and row.get("t", 0) >= self.registry.epoch():
                out.append(row)
        return out

    def tools(self):
        return [{"name": t.get("name"), "description": t.get("description"),
                 "definition": t.get("definition"), "session": t.get("session"),
                 "created": t.get("created"), "uses": t.get("uses", 0),
                 "tests": t.get("tests", []),
                 "prompts": len(t.get("prompts", []))}
                for t in self.registry.load()]
