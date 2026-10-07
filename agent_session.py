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
import oracle as orc
import workers

ROOT = Path(__file__).resolve().parent
AGENT_DIR = ROOT / "artifacts" / "agent"
MAX_MODEL_CALLS = 5          # per session: quick check + plan + repairs
MAX_REPAIRS = 3
REWRITE_TEMPERATURE = 0.8    # fresh-rewrite repairs sample for a different idea
MAX_PLAN_STEPS = 6
MAX_MODEL_CALLS_PLAN = 34    # when a goal is split into small tools
MAX_DEEP_CALLS = 5           # per session: costly "thinking" rewrites
MAX_SPLIT_DEPTH = 1         # a failed step may be split once into smaller tools

# Tolerant test comparison, defined in every rehearsal: numbers compare within
# a relative 1e-4 (so 0.6 equals 3/5), lists elementwise, everything else EQUAL.
GG_CHECK = (
    "(proclaim '(sb-ext:muffle-conditions style-warning sb-ext:compiler-note))\n"
    "(defun gg-near (a b) (cond ((and (realp a) (realp b)) "
    "(<= (abs (- a b)) (* 1d-4 (max 1 (abs a) (abs b))))) "
    "((and (consp a) (consp b)) (and (gg-near (car a) (car b)) "
    "(gg-near (cdr a) (cdr b)))) (t (equal a b))))\n"
    "(defun gg-check (got want) (if (gg-near got want) t (list :got got)))")
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
    "reader eval). Keep each tool small and single-purpose; for a big task, build small helper tools one at a time and compose them. In LET, bindings cannot see each other: use LET* when a binding uses an earlier one. If the goal clearly needs several functions (for example a ray tracer, a parser, a simulation), do NOT build it in one go: reply with a PLAN instead: {\"action\":\"plan\",\"steps\":[{\"name\":\"vec-dot\",\"spec\":\"one function, exact name and arguments, with a concrete example and its result\"}]} of at most 6 small single-function steps in dependency order; the last step is the top-level function. Represent vectors, points, colors and records as plain quoted lists like '(0 0 -5); never use #( ) vector literals, structs or hash tables, so tools fit together. NEVER invent expected values you cannot compute by hand (hash outputs, random numbers, timestamps, crypto, floating-point digits). For those, write PROPERTY tests whose expect is T, e.g. call (let ((h (my-hash \"abc\"))) (and (integerp h) (= h (my-hash \"abc\")) (/= h (my-hash \"abd\")))) with expect T; only use an exact value when you are certain of it (a published test vector or simple arithmetic). SETF takes flat place/value pairs, (setf a 1 b 2), with no parentheses around a pair; a later pair must not be wrapped like (b 2). Existing tools are already loaded and may be called by "
    "new tools. 'expect' is the PRIN1 text of the result, e.g. \"25\" or "
    "\"\\\"abc\\\"\" or \"(1 2 3)\". Give 2-4 tests on inputs whose results you can compute exactly by hand "
    "(numbers are compared with a small tolerance, so 0.6 matches 3/5). 'call' answers the "
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
    """Persistent store of promoted Lisp tools (JSON file).

    With ``mode`` set (``demo`` or ``live``) only tools built in that mode are
    visible, so scripted demo tools can never answer a live prompt (or the
    other way round). Tools without a recorded mode count as ``demo``.
    """

    def __init__(self, path=None, mode=None):
        self.path = Path(path) if path else AGENT_DIR / "tools.json"
        self.mode = mode
        self._lock = threading.Lock()

    def for_mode(self, mode):
        clone = ToolRegistry(self.path, mode)
        clone._lock = self._lock
        return clone

    @staticmethod
    def _mode_of(tool):
        return tool.get("mode") or "demo"

    def _raw(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _write(self, tools):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(tools, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def _mine(self, tool):
        return not self.mode or self._mode_of(tool) == self.mode

    def load(self):
        return [t for t in self._raw() if self._mine(t)]

    def add(self, tool):
        with self._lock:
            tool = dict(tool)
            if self.mode:
                tool["mode"] = self.mode
            tools = [t for t in self._raw()
                     if not (t.get("name") == tool["name"] and self._mine(t))]
            tools.append(tool)
            self._write(tools)

    def note_use(self, name, prompt, call):
        """Remember that PROMPT was answered by CALL on tool NAME."""
        with self._lock:
            tools = self._raw()
            for t in tools:
                if t.get("name") == name and self._mine(t):
                    if prompt and call:
                        t["prompts"] = sorted(set(t.get("prompts", []))
                                              | {normalize_prompt(prompt)})
                        t["call"] = call
                    t["uses"] = t.get("uses", 0) + 1
            self._write(tools)

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
        """Lisp source that defines every visible tool, in order."""
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


_TEMP = threading.local()     # per-thread sampling temperature override
BASE_TEMPERATURE = 0.0        # evidence_run --temperature sets this
_RETRY_WAITS = (3, 8, 20)    # seconds between retries of transient API errors


def _with_retry(fn):
    """Call FN, retrying rate limits / overload / timeouts with visible progress."""
    for i in range(len(_RETRY_WAITS) + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - classify by message
            text = str(exc).lower()
            transient = any(x in text for x in (
                "429", "rate", "too_many", "traffic", "queue", "timed out",
                "timeout", "503", "502", "overloaded", "connection"))
            if not transient or i == len(_RETRY_WAITS):
                raise
            note = getattr(_TEMP, "notify", None)
            if note:
                note("The model API is busy (%s). Retrying in %ds (attempt %d of %d)..."
                     % (text.split(":")[0][:40] or "temporary error",
                        _RETRY_WAITS[i], i + 2, len(_RETRY_WAITS) + 1))
            time.sleep(_RETRY_WAITS[i])



def live_generate(system, user):
    status = live_status()
    if not status["available"]:
        raise RuntimeError("live mode unavailable: " + status["reason"])
    import cerebras_client
    deep = getattr(_TEMP, "deep", False)       # hard retries: let the model think
    temp = getattr(_TEMP, "value", None)
    temp = BASE_TEMPERATURE if temp is None else temp
    if not deep:
        return _with_retry(lambda: cerebras_client.generate(
            user, system=system, max_tokens=getattr(_TEMP, "max_tokens", 2200),
            temperature=temp, reasoning_effort="none"))
    res = _with_retry(lambda: cerebras_client.generate(
        user, system=system, max_tokens=8000, temperature=temp,
        reasoning_effort="medium", timeout=120.0))
    if not (res.get("text") or "").strip():       # thinking ate the budget
        res2 = _with_retry(lambda: cerebras_client.generate(
            user, system=system, max_tokens=8000, temperature=temp,
            reasoning_effort="low", timeout=120.0))
        res2["input_tokens"] = (res2.get("input_tokens") or 0) + (res.get("input_tokens") or 0)
        res2["output_tokens"] = (res2.get("output_tokens") or 0) + (res.get("output_tokens") or 0)
        res2["cost_usd"] = (res2.get("cost_usd") or 0) + (res.get("cost_usd") or 0)
        return res2
    return res


# Extra scripted examples for the demo model. Each entry fires when ALL
# `words` appear in the goal and none of `not_words` do; `needs` names a
# saved tool it can compose (else a standalone `alt` definition is built).
def _sphere_hits(n):
    """Independent count of grid points inside the unit disc (exact)."""
    from fractions import Fraction as F
    c = 0
    for j in range(n):
        for i in range(n):
            x = F(3 * i, n - 1) - F(3, 2)
            y = F(3 * j, n - 1) - F(3, 2)
            if x * x + y * y <= 1:
                c += 1
    return c


DEMO_TOOLS = [
    {"name": "vec-dot", "words": ["vec-dot"], "not_words": [],
     "desc": "Dot product of two number lists",
     "definition": "(defun vec-dot (a b) (reduce #'+ (mapcar #'* a b)))",
     "tests": [("(vec-dot '(1 2 3) '(4 5 6))", "32"), ("(vec-dot '(1 0 0) '(0 1 0))", "0")],
     "call": "(vec-dot '(1 2 3) '(4 5 6))"},
    {"name": "vec-sub", "words": ["vec-sub"], "not_words": [],
     "desc": "Elementwise difference of two number lists",
     "definition": "(defun vec-sub (a b) (mapcar #'- a b))",
     "tests": [("(vec-sub '(5 7 9) '(1 2 3))", "(4 5 6)")],
     "call": "(vec-sub '(5 7 9) '(1 2 3))"},
    {"name": "ray-hits-sphere-p", "words": ["ray-hits-sphere-p"], "not_words": [],
     "desc": "True when a ray hits a sphere",
     "definition": ("(defun ray-hits-sphere-p (origin dir center radius) "
                    "(let* ((oc (vec-sub origin center)) (a (vec-dot dir dir)) "
                    "(b (* 2 (vec-dot oc dir))) "
                    "(c (- (vec-dot oc oc) (* radius radius)))) "
                    "(>= (- (* b b) (* 4 a c)) 0)))"),
     "tests": [("(ray-hits-sphere-p '(0 0 -5) '(0 0 1) '(0 0 0) 1)", "T"),
               ("(ray-hits-sphere-p '(0 3 -5) '(0 0 1) '(0 0 0) 1)", "NIL")],
     "call": "(ray-hits-sphere-p '(0 0 -5) '(0 0 1) '(0 0 0) 1)"},
    {"name": "render-sphere-ascii", "words": ["render-sphere-ascii"], "not_words": [],
     "desc": "ASCII picture of a unit sphere traced with ray-hits-sphere-p",
     "definition": ("(defun render-sphere-ascii (n) (let ((rows nil)) "
                    "(dotimes (j n) (let ((line (make-string n :initial-element #\\.))) "
                    "(dotimes (i n) (let ((x (- (* 3 (/ i (- n 1))) 3/2)) "
                    "(y (- (* 3 (/ j (- n 1))) 3/2))) "
                    "(when (ray-hits-sphere-p (list x y -5) '(0 0 1) '(0 0 0) 1) "
                    "(setf (char line i) #\\#)))) (push line rows))) "
                    "(format nil \"~{~a~^~%~}\" (nreverse rows))))"),
     "tests": [("(count #\\# (render-sphere-ascii 9))", str(_sphere_hits(9))),
               ("(length (render-sphere-ascii 9))", str(9 * 9 + 8))],
     "call": "(render-sphere-ascii 9)"},
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
    if "all planned helper tools are built" in user.lower() \
            and "render-sphere-ascii" in have:
        return _fake({"action": "use", "call": "(render-sphere-ascii 9)",
                      "why": "the ray tracer's top-level tool is ready"}, user)
    if ("ray tracer" in goal or "raytracer" in goal) \
            and "render-sphere-ascii" not in have:
        return _fake({"action": "plan", "steps": [
            {"name": "vec-dot", "spec": "vec-dot (a b): dot product of two number lists, e.g. (vec-dot '(1 2 3) '(4 5 6)) is 32"},
            {"name": "vec-sub", "spec": "vec-sub (a b): elementwise difference of two number lists"},
            {"name": "ray-hits-sphere-p", "spec": "ray-hits-sphere-p (origin dir center radius): true when the ray hits the sphere"},
            {"name": "render-sphere-ascii", "spec": "render-sphere-ascii (n): n by n picture of a unit sphere, one ray per cell"}]}, user)
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
        if c == "'" and qdepth is not None and text[i + 1:i + 2] == "(":
            i += 1                 # nested quote inside quoted data: (a '(b)) -> (a (b))
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


def lisp_hint(detail):
    """Targeted advice for common Lisp slips, from the failing-test detail."""
    return " ".join(adv for _, adv in orc.lisp_hints(detail))


def _split_top(inner):
    """Split the inside of a Lisp form into its top-level argument strings."""
    args, depth, i, start, n = [], 0, 0, None, len(inner)
    while i < n:
        c = inner[i]
        if c.isspace() and depth == 0:
            if start is not None:
                args.append(inner[start:i]); start = None
            i += 1
            continue
        if start is None:
            start = i
        if c == '"':
            i += 1
            while i < n and inner[i] != '"':
                i += 2 if inner[i] == "\\" else 1
        elif c == "#" and inner[i + 1:i + 2] == "\\":
            i += 2
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        i += 1
    if start is not None:
        args.append(inner[start:])
    return args


def _form_end(text, open_idx):
    """Index of the ``)`` matching the ``(`` at OPEN_IDX, or -1."""
    depth, i, n = 0, open_idx, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "#" and text[i + 1:i + 2] == "\\":
            i += 2
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def fix_setf(text):
    """Flatten the SETF slip ``(setf a X (b Y))`` into ``(setf a X b Y)``.

    A SETF with an odd number of arguments whose last argument is a two-element
    list headed by a symbol is the model wrapping a place/value pair in
    parentheses; flattening it keeps the intended sequential assignment.
    """
    out, i = [], 0
    for m in re.finditer(r"\(setf\s", text, re.I):
        if m.start() < i:
            continue                      # inside a form already rewritten
        end = _form_end(text, m.start())
        if end < 0:
            continue
        inner = text[m.start() + len("(setf "):end]
        args = _split_top(inner)
        if len(args) % 2 == 1 and len(args) >= 3 and args[-1].startswith("("):
            last_end = _form_end(args[-1], 0)
            parts = _split_top(args[-1][1:last_end]) if last_end > 0 else []
            if last_end == len(args[-1]) - 1 and len(parts) == 2 and \
                    re.match(r"^[A-Za-z*+%-][\w*+%-]*$", parts[0]):
                args = args[:-1] + parts
                out.append((m.start(), end, "(setf " + " ".join(args) + ")"))
                i = end + 1
    for start, end, repl in reversed(out):
        text = text[:start] + repl + text[end + 1:]
    return text


def normalize_plan(plan):
    """Apply quote_literals to every call in a build/use plan, in place."""
    if isinstance(plan.get("definition"), str):
        plan["definition"] = fix_setf(complete_parens(plan["definition"]))
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


def validate_build(plan, frozen=None):
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
    seen = {}
    for t in tests:
        prev = seen.setdefault(t["call"], t["expect"])
        if prev != t["expect"]:
            return ("SPEC_INCONSISTENT: the same call %s has two different "
                    "expected values (%s and %s)" % (t["call"], prev, t["expect"]))
        if frozen and t["call"] in frozen and frozen[t["call"]] != t["expect"]:
            return ("SPEC_INCONSISTENT: the expected value for %s is frozen as "
                    "%s (verified earlier); do not change it"
                    % (t["call"], frozen[t["call"]]))
    for t in tests:
        if "#" in t["expect"]:
            return ("expected values cannot contain '#': write plain lists like "
                    "(0 0 -5), not #(0 0 -5) vectors or other reader syntax")
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
                 arm="main", pair=None, expected=None, oracle=None,
                 lessons=None, postmortem_dir=None):
        self.id = session_id or uuid.uuid4().hex[:10]
        self.prompt = prompt
        self.generate = generate
        self.registry = (registry or ToolRegistry()).for_mode(mode)
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
        self.max_calls = MAX_MODEL_CALLS
        self._deep_calls = 0
        self._built = []        # tools saved by this session: (name, tests)
        self._t0 = time.time()
        self._in_step = False
        self._shown = {}
        self.lessons = lessons
        self.postmortem_dir = postmortem_dir
        self._frozen = {}           # call -> expected value, verified or reference-derived
        self._seen_expect = {}      # call -> every expected value ever proposed
        self._fails = []            # one record per failed rehearsal
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
    def _ask(self, user_text, label, system=None, temperature=None):
        if self.model_calls >= self.max_calls:
            raise RuntimeError("model call budget (%d) exhausted"
                               % self.max_calls)
        self.model_calls += 1
        self.emit("model_call", label=label, prompt=user_text)
        if temperature is not None:
            # "thinking" retries are the expensive part: bound them per session
            if self._deep_calls >= MAX_DEEP_CALLS:
                temperature = None
            else:
                self._deep_calls += 1
        if temperature is not None:
            _TEMP.value = temperature
            _TEMP.deep = True
        _TEMP.notify = lambda msg: self.emit("model_wait", message=msg)
        try:
            res = self.generate(system or SYSTEM_PROMPT, user_text)
        finally:
            _TEMP.notify = None
            if temperature is not None:
                _TEMP.value = None
                _TEMP.deep = False
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
        try:
            return normalize_plan(extract_json(res.get("text")))
        except ValueError as exc:
            # cut off or malformed JSON: retry once, bigger budget, compact reply
            if self.model_calls >= self.max_calls:
                raise
            self.emit("json_retry", reason=str(exc))
            self.model_calls += 1
            self.emit("model_call", label=label + " (retry: invalid JSON)",
                      prompt=user_text)
            _TEMP.max_tokens = 5000
            try:
                res = self.generate(
                    system or SYSTEM_PROMPT,
                    user_text + "\nYOUR PREVIOUS REPLY WAS CUT OFF OR NOT VALID "
                    "JSON. Reply with ONE complete JSON object only, and keep "
                    "the Lisp definition compact.")
            finally:
                _TEMP.max_tokens = 2200
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

    def _user_prompt(self, extra="", goal=None):
        lines = []
        for t in self.registry.load():
            lines.append("TOOL %s %s: %s" % (t["name"],
                         signature(t["definition"]).split(" ", 1)[1][:-1],
                         t.get("description", "")))
        reg = "\n".join(lines) or "(no tools yet)"
        text = "REGISTRY:\n%s\nGOAL: %s\n%s" % (reg, goal or self.prompt, extra)
        if self.lessons is not None:
            advice = self.lessons.advice()
            if advice:
                text += "\n" + advice
        return text

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
        self.emit("summary", **self._summary())
        if self.state != "done" and self.postmortem_dir:
            try:
                orc.write_postmortem(self.postmortem_dir, self.id, self._postmortem())
            except OSError:
                pass
        self.emit("done", state=self.state, model_calls=self.model_calls,
                  input_tokens=self.input_tokens,
                  output_tokens=self.output_tokens,
                  cost_usd=round(self.cost_usd, 6))

    def _efficiency(self):
        """How much of the effort was repeated failure."""
        sigs = [f["sig"] for f in self._fails]
        wasted, prev = 0, 0
        for f in self._fails:
            if f["repeated"]:
                wasted += max(0, f["tokens"] - prev)   # the call that re-made the error
            prev = f["tokens"]
        return {"failed_attempts": len(self._fails),
                "unique_errors": len(set(sigs)),
                "repeated_errors": len(sigs) - len(set(sigs)),
                "wasted_tokens": wasted}

    def _postmortem(self):
        classes = [f["class"] for f in self._fails if f.get("class")]
        root = max(set(classes), key=classes.count) if classes else "UNKNOWN"
        orc_events = [{k: e[k] for k in ("kind", "call", "expected", "algo", "was", "got")
                       if k in e} for e in self.events
                      if e["kind"] in ("oracle_corrected", "oracle_reference")]
        drift = sorted(c for c, vals in self._seen_expect.items() if len(vals) > 1)
        fixes = []
        if "TEST_WRONG" in classes or drift:
            fixes.append("verify the expected values with an independent reference "
                         "or use property tests: " + ", ".join(drift[:3] or ["see failures"]))
        return {
            "prompt": self.prompt, "mode": self.mode, "outcome": self.state,
            "root_cause": root,
            "root_cause_meaning": orc.CLASS_LABELS.get(root, root),
            "failures": self._fails,
            "oracle_events": orc_events,
            "expected_value_drift": drift,
            "repeated_mistakes": [f["sig"] for f in self._fails if f["repeated"]],
            "lessons_counts": self.lessons.counts() if self.lessons else {},
            "tools_saved_before_stopping": [n for n, _ in self._built],
            "benchmark_fixes_required": fixes,
            "efficiency": self._efficiency()}

    def _summary(self):
        """Plain-data account of what this session did, for the UI card."""
        ev = list(self.events)
        count = lambda k: sum(1 for e in ev if e["kind"] == k)  # noqa: E731
        results = [e for e in ev if e["kind"] == "result"]
        last = results[-1] if results else None
        gave = [e for e in ev if e["kind"] == "gave_up"]
        errs = [e for e in ev if e["kind"] == "error"]
        decisions = [e.get("action") for e in ev if e["kind"] == "decision"]
        if "cache" in decisions:
            kind = "cache"
        elif "plan" in decisions:
            kind = "plan"
        elif "build" in decisions:
            kind = "build"
        elif "use" in decisions:
            kind = "reuse"
        else:
            kind = "other"
        planned = next((len(e["steps"]) for e in ev
                        if e["kind"] == "plan" and not e.get("sub")), 0)
        outcome = ("success" if self.state == "done" else
                   "error" if self.state == "error" else "failed")
        m = re.match(r"\s*\(\s*(\S+)", (last or {}).get("call") or "")
        return {
            "outcome": outcome, "flow": kind,
            "built": [{"name": n, "tests": t} for n, t in self._built],
            "planned": planned,
            "tests_passed": sum(t for _, t in self._built),
            "repairs": count("repair"), "splits": count("replan"),
            "model_calls": self.model_calls,
            "tokens": self.input_tokens + self.output_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "seconds": round(time.time() - self._t0, 1),
            "answer": ({"call": last.get("call"), "value": last.get("value"),
                        "ok": last.get("ok"), "tool": m.group(1) if m else None}
                       if last else None),
            "efficiency": self._efficiency(),
            "oracle_fixes": count("oracle_corrected") + count("oracle_reference"),
            "stopped": ({"detail": gave[-1].get("detail"),
                         "hint": gave[-1].get("hint")} if gave else None),
            "error": errs[-1].get("message") if errs else None,
        }

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
        if plan.get("action") == "plan":
            if not self._run_steps(plan):
                return
            plan = self._ask(self._user_prompt(
                "All planned helper tools are built and saved. Now finish the "
                "original goal: reply with action use and a call of the "
                "top-level tool using literal data from the goal, or build "
                "one last small tool that composes them."), "final")
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
        self._build_loop(plan, prelude)

    # -- planner: a big goal becomes a few small, individually tested tools ---
    def _run_steps(self, plan):
        steps = plan.get("steps")
        ok = isinstance(steps, list) and 1 <= len(steps) <= MAX_PLAN_STEPS and \
            all(isinstance(x, dict) and isinstance(x.get("spec"), str) for x in steps)
        if not ok:
            self.state = "failed"
            self.emit("gave_up", detail="the plan was malformed or had more "
                      "than %d steps" % MAX_PLAN_STEPS, hint="", attempts=1)
            return False
        self.max_calls = MAX_MODEL_CALLS_PLAN
        self.emit("decision", action="plan", plan={
            "why": "this goal needs several functions: building %d small "
                   "tools in order" % len(steps)})
        self.emit("plan", steps=[{"name": x.get("name", ""), "spec": x["spec"]}
                                 for x in steps])
        self._in_step = True
        try:
            for i, step in enumerate(steps, 1):
                self.emit("step", i=i, n=len(steps), name=step.get("name", ""),
                          spec=step["spec"])
                if not self._build_step(step, 0):
                    self.state = "failed"
                    fail = getattr(self, "_last_failure", {}) or {}
                    self.emit("gave_up", detail=fail.get("detail", ""),
                              hint=fail.get("hint", ""),
                              attempts=fail.get("attempts", 0), step=True)
                    return False
        finally:
            self._in_step = False
        return True

    def _build_step(self, step, depth):
        """Build one planned tool; on failure split it once into smaller tools."""
        spec = step["spec"]
        first = self._ask(self._user_prompt(
            "BUILD exactly this one small tool now (action build). "
            "Use LET* when a binding uses an earlier one. Vectors and "
            "points are plain lists like '(0 0 -5), never #( ) arrays; "
            "reuse the helper tools already in the registry.", goal=spec),
            "step" if depth == 0 else "sub-step")
        self.emit("decision", action=first.get("action"), plan=first)
        if first.get("action") != "build":
            return True               # the model says it already exists
        if self._build_loop(first, self.registry.prelude(), goal=spec, quiet=True):
            return True
        if depth >= MAX_SPLIT_DEPTH:
            return False
        fail = self._last_failure
        self.emit("replan", spec=spec, detail=fail.get("detail", ""))
        sp = self._ask(self._user_prompt(
            "The tool for this step kept failing its tests: %s\nLast "
            "failures: %s\nSplit it into at most 3 SMALLER single-function "
            "tools that it can then call (reply with action plan and steps, "
            "each with name and spec, in dependency order)."
            % (spec, fail.get("detail", "")[:400]), goal=spec), "split")
        subs = sp.get("steps") if sp.get("action") == "plan" else None
        if not (isinstance(subs, list) and 1 <= len(subs) <= 3 and all(
                isinstance(x, dict) and isinstance(x.get("spec"), str)
                for x in subs)):
            return False
        self.emit("plan", steps=[{"name": x.get("name", ""), "spec": x["spec"]}
                                 for x in subs], sub=True)
        for j, sub in enumerate(subs, 1):
            self.emit("step", i=j, n=len(subs), name=sub.get("name", ""),
                      spec=sub["spec"], sub=True)
            if not self._build_step(sub, depth + 1):
                return False
        again = self._ask(self._user_prompt(
            "Now build the ORIGINAL step again, calling the new helper tools: "
            "%s" % spec, goal=spec), "retry step")
        self.emit("decision", action=again.get("action"), plan=again)
        if again.get("action") != "build":
            return True
        return self._build_loop(again, self.registry.prelude(), goal=spec,
                                quiet=True)

    @staticmethod
    def _failure_text(verdict):
        """Detail plus the full error text: what hints and lessons search."""
        return "%s %s" % (verdict.get("detail") or "", verdict.get("error_text") or "")

    def _note_failure(self, verdict, attempt):
        """Fingerprint a failed rehearsal and feed recurring slips to lessons."""
        detail = verdict.get("detail") or verdict.get("reason") or ""
        sig = orc.error_signature(detail)
        tokens = self.input_tokens + self.output_tokens
        self._fails.append({
            "attempt": attempt, "class": verdict.get("class"), "sig": sig,
            "tokens": tokens, "detail": detail[:300],
            "repeated": any(f["sig"] == sig for f in self._fails)})
        if self.lessons is not None:
            self.lessons.note([k for k, _ in orc.lisp_hints(self._failure_text(verdict))])

    def _build_loop(self, plan, prelude, goal=None, quiet=False):
        """Rehearse PLAN, repairing up to MAX_REPAIRS times. True if saved."""
        prev_got, defs_seen, rescued = {}, set(), False
        self._last_stuck = False
        for attempt in range(MAX_REPAIRS + 1):
            problem = validate_build(plan, self._frozen)
            ndef = (orc.norm_definition(plan.get("definition")),
                    tuple((t.get("call"), t.get("expect"))
                          for t in (plan.get("tests") or [])
                          if isinstance(t, dict)))
            if problem:
                verdict = {"ok": False, "reason": problem, "stage": "schema",
                           "detail": problem, "got_map": {},
                           "class": ("SPEC_INCONSISTENT"
                                     if problem.startswith("SPEC_INCONSISTENT")
                                     else "SCHEMA")}
                self.emit("verdict", **verdict)
            elif ndef in defs_seen:
                # never re-run code that already failed: it would fail the same way
                verdict = {"ok": False, "stage": "repeat", "got_map": {},
                           "reason": "identical to an earlier failed attempt",
                           "detail": "the new definition is identical to a "
                                     "previous failed one",
                           "class": "REPEATED_CANDIDATE"}
                self.emit("repeat_candidate", name=plan.get("name"))
                self.emit("verdict", **verdict)
            else:
                defs_seen.add(ndef)
                verdict = self._rehearse(plan, self.registry.prelude())
            if verdict["ok"]:
                return self._promote(plan)
            self._note_failure(verdict, attempt)
            # identical code AND identical tests after a value mismatch: the
            # model is stuck defending an expectation, so blame the test
            repeat_stuck = verdict.get("class") == "REPEATED_CANDIDATE" and \
                bool(prev_got)
            test_wrong = verdict.get("class") == "TEST_WRONG" or \
                bool(verdict.get("drift")) or repeat_stuck
            if test_wrong:
                self._last_stuck = True
            if not rescued and (test_wrong or (attempt >= MAX_REPAIRS
                                               and self._last_stuck)):
                # The evidence points at the TEST, not the code: stop mutating the
                # code. Keep it as is and swap guessed values for property tests.
                rescued = True
                self.emit("rescue", reason=orc.CLASS_LABELS.get(
                    verdict.get("class"), "expected values look guessed"))
                saved = plan
                plan = self._ask(self._user_prompt(
                    "Your code returned the same results across attempts, so "
                    "the exact expected values in your tests were probably "
                    "guesses. Return build JSON with the definition EXACTLY "
                    "unchanged:\n%s\nbut replace every exact-value test you "
                    "cannot verify by hand with PROPERTY tests whose expect "
                    "is T (determinism, type, range, length, different "
                    "inputs give different results). Keep exact values only "
                    "for results you are certain of." % saved["definition"],
                    goal=goal), "property-tests")
                self.emit("decision", action=plan.get("action"), plan=plan)
                if plan.get("action") == "build" and \
                        plan.get("definition") == saved["definition"] and \
                        not validate_build(plan, self._frozen):
                    verdict = self._rehearse(plan, self.registry.prelude())
                    if verdict["ok"]:
                        return self._promote(plan)
                    self._note_failure(verdict, attempt)
            if attempt >= MAX_REPAIRS:
                self._last_failure = {
                    "detail": verdict.get("detail") or verdict.get("reason") or "",
                    "hint": (lisp_hint(self._failure_text(verdict)) or (
                        "the tests expected exact values the model could not "
                        "have computed (for example hash outputs), while the "
                        "code returned the same result every time. Ask for "
                        "property checks instead of exact values."
                        if self._last_stuck else "")),
                    "cls": verdict.get("class"),
                    "attempts": attempt + 1}
                if not quiet:
                    self.state = "failed"
                    self.emit("gave_up", step=bool(self._in_step),
                              **self._last_failure)
                return False
            self.emit("repair", attempt=attempt + 1, reason=verdict["reason"],
                      cls=verdict.get("class"))
            hint = lisp_hint(self._failure_text(verdict))
            stuck = [c for c, g in (verdict.get("got_map") or {}).items()
                     if prev_got.get(c) == g]
            prev_got = dict(verdict.get("got_map") or {})
            if stuck:
                hint = ((hint + " ") if hint else "") + (
                    "YOUR CODE RETURNED THE SAME VALUE FOR %s in two different "
                    "attempts, but the expected value never matched. That "
                    "strongly suggests the EXPECTED value is a guess you cannot "
                    "compute by hand (hashes, random, crypto). Replace those "
                    "exact-value tests with PROPERTY tests that evaluate to T "
                    "(determinism, type, range, length, different inputs give "
                    "different results), or delete them."
                    % ", ".join(stuck[:2]))
                self._last_stuck = True
            head = ("PREVIOUS ATTEMPT FAILED (%s, %s): %s\nFailing tests: %s\n"
                    % (verdict.get("stage"), verdict.get("class") or "?",
                       verdict["reason"], verdict.get("detail") or "n/a"))
            temp = None
            if verdict.get("class") == "COMPILER_ERROR":
                # syntax first: a narrow repair, not a rewrite of the algorithm
                body = ("The code does not COMPILE. Fix ONLY the compile "
                        "error(s) shown: keep the algorithm and the tests "
                        "exactly as they are and change as little as "
                        "possible.\n%sPrevious JSON: %s\nReturn corrected "
                        "build JSON." % (hint + "\n" if hint else "",
                                         json.dumps(plan)))
            elif verdict.get("class") == "REPEATED_CANDIDATE":
                body = ("Your last candidate was IDENTICAL to an earlier failed "
                        "one. Change the approach: restructure the code and "
                        "re-derive the formula before answering.\n%sReturn "
                        "build JSON." % (hint + "\n" if hint else ""))
                temp = REWRITE_TEMPERATURE
            elif attempt == 0:
                body = ("Either the code or the expected value may be wrong. "
                        "Trace the FIRST failing test through your code by "
                        "hand, line by line, find the line whose result "
                        "differs from what the test expects, and fix that "
                        "line. Numbers compare with a small tolerance, so 0.6 "
                        "equals 3/5. Do not change the expected value of a "
                        "test unless you can prove it wrong by hand.\n"
                        "%sPrevious JSON: %s\nReturn corrected build JSON."
                        % (hint + "\n" if hint else "", json.dumps(plan)))
            else:
                body = ("Your previous attempts failed the same way, so do "
                        "NOT patch them. Rewrite the tool from scratch with a "
                        "different structure: first re-derive the algorithm "
                        "and any formula from its definition (check signs and "
                        "operator order), then hand-trace the first failing "
                        "test through the NEW code before answering. Keep it "
                        "small; helper tools in the registry may be used.\n"
                        "%sReturn build JSON." % (hint + "\n" if hint else ""))
                temp = REWRITE_TEMPERATURE
            plan = self._ask(self._user_prompt(head + body, goal=goal),
                             "repair" if attempt == 0 else "rewrite",
                             temperature=temp)
            self.emit("decision", action=plan.get("action"), plan=plan)
            if plan.get("action") != "build":
                self._last_failure = {"detail": "the model stopped building",
                                      "hint": "", "attempts": attempt + 1}
                if not quiet:
                    self.state = "failed"
                return False
        return False

    def _promote(self, plan):
        """Save a tool whose tests passed and, at top level, answer with it."""
        tests = []
        for t in plan["tests"]:
            tests.append({
                "call": t["call"], "expect": t["expect"],
                "confidence": orc.oracle_confidence(t["call"], t["expect"]),
                "source": t.get("source") or (
                    "property test (model-written)"
                    if t["expect"].strip().upper() == "T"
                    else "model-written, matched by the code")})
        self.registry.add({
            "name": plan["name"],
            "description": plan.get("description", ""),
            "definition": plan["definition"],
            "tests": tests, "session": self.id,
            "prompts": ([] if self._in_step
                        else [normalize_prompt(self.prompt)]),
            "call": plan["call"],
            "created": round(time.time(), 3)})
        for dep in self.registry.load():
            if dep["name"] != plan["name"] and re.search(
                    r"[\s(']%s[\s)]" % re.escape(dep["name"]),
                    plan["definition"]):
                self.registry.note_use(dep["name"], "", "")
        self._built.append((plan["name"], len(plan.get("tests") or [])))
        self.emit("promoted", name=plan["name"],
                  tools=[t["name"] for t in self.registry.load()])
        if not self._in_step:
            self._finish_call(plan["call"], self.registry.prelude())
        return True

    def _rehearse(self, plan, prelude):
        """Run the tests; then let an independent reference overrule bad vectors."""
        out = self._rehearse_once(plan, prelude)
        if not out["ok"]:
            fixes = self._reference_pass(plan, out)
            if fixes["corrected"]:
                out = self._rehearse_once(plan, prelude)   # same code, true vectors
            if fixes["reference"]:
                note = "; ".join(
                    "the independent %s reference says %s for %s" % (a, v, c)
                    for c, v, a in fixes["reference"])
                out["detail"] = (out["detail"] + "; " if out["detail"] else "") + note
            out["oracle"] = {"corrected": [c for c, _, _ in fixes["corrected"]],
                             "reference": [c for c, _, _ in fixes["reference"]]}
        public = {k: v for k, v in out.items() if k not in ("infos", "error_text")}
        self.emit("verdict", **public)
        return out

    def _reference_pass(self, plan, out):
        """Replace guessed hash/checksum vectors by Python's reference values."""
        res = {"corrected": [], "reference": []}
        algo = orc.detect_algo(plan.get("name"), plan.get("description"), self.prompt)
        if not algo:
            return res
        for info in out.get("infos", []):
            idx = info["index"]
            if not (isinstance(idx, int) and 0 <= idx < len(plan["tests"])):
                continue
            t = plan["tests"][idx]
            arg = orc.single_string_arg(t["call"])
            if arg is None or info.get("got") is None:
                continue
            ref = orc.reference_value(algo, arg)
            refs = orc.format_reference(ref)
            if t["expect"].strip() == refs:
                continue
            was = t["expect"]
            t["expect"] = refs
            t["source"] = "independent reference: %s" % algo
            self._frozen[t["call"]] = refs
            if orc.matches_reference(info["got"], ref):
                res["corrected"].append((t["call"], refs, algo))
                self.emit("oracle_corrected", call=t["call"], expected=refs,
                          algo=algo, was=was)
            else:
                res["reference"].append((t["call"], refs, algo))
                self.emit("oracle_reference", call=t["call"], expected=refs,
                          algo=algo, got=info["got"])
        return res

    def _rehearse_once(self, plan, prelude):
        direct = []
        for t in plan["tests"]:
            self._seen_expect.setdefault(t["call"], set()).add(t["expect"])
            code = "%s\n%s\n%s\n(gg-check %s '%s)" % (
                GG_CHECK, prelude, plan["definition"], t["call"], t["expect"])
            self._shown[code] = "%s   ;; expect %s" % (t["call"], t["expect"])
            direct.append({"code": code, "expect": "T"})
        res = pipeline.run_candidate(
            candidate_text(plan), tests={"direct": direct},
            worker_fn=self._recording_worker(plan, prelude),
            risk_fn=self.risk_fn)
        stage_names = [(s["name"], s["status"]) for s in res.get("stages", [])]
        v = res.get("verdict", {})
        items = ((res.get("evidence") or {}).get("direct") or {}).get("items") or []
        infos, detail, got_map = [], [], {}
        for item in items:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            ok_idx = isinstance(idx, int) and 0 <= idx < len(plan["tests"])
            if item.get("pass", True):
                if ok_idx:                     # passed: this expectation is verified
                    self._frozen.setdefault(plan["tests"][idx]["call"],
                                            plan["tests"][idx]["expect"])
                continue
            t = plan["tests"][idx] if ok_idx else {"call": "?", "expect": ""}
            rv = (item.get("return_value") or "").strip()
            got = rv[6:-1] if rv.upper().startswith("(:GOT ") and ok_idx else None
            err = (item.get("error") or "")
            infos.append({"index": idx, "call": t["call"], "expected": t["expect"],
                          "got": got, "error": err,
                          "confidence": orc.oracle_confidence(t["call"], t["expect"])})
            if got is not None:
                got_map[t["call"]] = got
                detail.append("%s: got %s, expected %s" % (t["call"], got, t["expect"]))
            else:
                detail.append("%s: %s" % (t["call"], " ".join((err or "failed").split(
                    "--- backtrace ---")[0].split())[:200]))
        drift = [i["call"] for i in infos
                 if len(self._seen_expect.get(i["call"], ())) > 1]
        ok = bool(res.get("ok"))
        return {"ok": ok, "reason": v.get("reason"), "got_map": got_map,
                "detail": "; ".join(detail[:4]), "tests": len(plan["tests"]),
                "stage": v.get("failed_stage"),
                "risk": (res.get("risk") or {}).get("level"),
                "stages": stage_names, "infos": infos, "drift": drift,
                "error_text": " ".join(" ".join((i["error"] or "").split())
                                       for i in infos)[:2500],
                "class": None if ok else orc.failure_class(infos, drift)}

    def _recording_worker(self, plan, prelude):
        """Worker wrapper that logs each rehearsal eval as a REPL line."""
        def run(code):
            env = self.worker_fn(code)
            shown = self._shown.get(code) or code
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
                  expected_ok=(None if self.expected is None or self._in_step
                               else env.get("return_value") == self.expected),
                  error=(env.get("error") or "")[:600])
        if env.get("ok"):
            return call
        if retry and self.model_calls < self.max_calls:
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


def _heldout_all():
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
    ] + _harder_heldout()


def _harder_heldout():
    """Ten more held-out prompts, every answer computed by independent Python."""
    def is_prime(n):
        return n > 1 and all(n % d for d in range(2, int(n ** 0.5) + 1))

    def roman(n):
        out = ""
        for v, sym in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"),
                       (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"),
                       (5, "V"), (4, "IV"), (1, "I")):
            while n >= v:
                out += sym
                n -= v
        return out

    def fib(n):
        a, b = 0, 1
        for _ in range(n):
            a, b = b, a + b
        return a

    import math
    dedupe, seen = [], set()
    for v in (3, 1, 3, 2, 1):
        if v not in seen:
            seen.add(v)
            dedupe.append(v)
    zeros = sum(100 // 5 ** k for k in range(1, 4))
    med = sorted((7, 1, 5, 3, 9))[2]
    return [
        {"prompt": "write a function that counts the prime numbers up to n, then "
                   "count them up to 50",
         "expected": str(sum(is_prime(n) for n in range(2, 51))),
         "oracle": "python: trial-division prime count"},
        {"prompt": "write a function that converts a non-negative integer to a "
                   "binary string, then convert 37",
         "expected": '"%s"' % format(37, "b"),
         "oracle": "python: format(n, 'b')"},
        {"prompt": "write a function for the least common multiple of two "
                   "numbers, then compute it for 12 and 18",
         "expected": str(12 * 18 // math.gcd(12, 18)),
         "oracle": "python: lcm via gcd"},
        {"prompt": "write a function that converts an integer to a Roman "
                   "numeral string, then convert 1994",
         "expected": '"%s"' % roman(1994),
         "oracle": "python: greedy roman numerals"},
        {"prompt": "write a function that checks whether two words are "
                   "anagrams, then check listen and silent",
         "expected": "T" if sorted("listen") == sorted("silent") else "NIL",
         "oracle": "python: sorted letters"},
        {"prompt": "write a function for the sum of the first n square "
                   "numbers, then compute it for n = 10",
         "expected": str(sum(k * k for k in range(1, 11))),
         "oracle": "python: sum of squares"},
        {"prompt": "write a function that counts the trailing zeros of n "
                   "factorial, then compute it for n = 100",
         "expected": str(zeros),
         "oracle": "python: Legendre formula"},
        {"prompt": "write a function for the n-th Fibonacci number "
                   "(0-indexed), then compute it for n = 15",
         "expected": str(fib(15)),
         "oracle": "python: iterative Fibonacci"},
        {"prompt": "write a function that removes duplicates from a list "
                   "keeping first occurrences, then use it on (3 1 3 2 1)",
         "expected": "(%s)" % " ".join(map(str, dedupe)),
         "oracle": "python: ordered dedupe"},
        {"prompt": "write a function for the median of a list of an odd "
                   "number of numbers, then use it on (7 1 5 3 9)",
         "expected": str(med),
         "oracle": "python: sorted()[len//2]"},
    ]


def heldout_tasks(n=10):
    """The first N held-out prompts (default 10, at most 20), oracle-checked."""
    tasks = _heldout_all()
    return tasks[:max(1, min(int(n), len(tasks)))]


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


# --------------------------------------------------------------------------
# Safe typed REPL: a strict allow-list over what a user may evaluate.
# Not an OS sandbox: it narrows WHAT can run (pure data transformation plus the
# saved tools); the worker's timeout and memory limits still bound HOW MUCH.
# --------------------------------------------------------------------------
_REPL_FUNCS = {
    # numbers
    "+", "-", "*", "/", "1+", "1-", "mod", "rem", "abs", "min", "max", "floor",
    "ceiling", "round", "truncate", "sqrt", "isqrt", "expt", "exp", "log", "sin",
    "cos", "tan", "gcd", "lcm", "evenp", "oddp", "zerop", "plusp", "minusp",
    "numberp", "integerp", "rationalp", "float", "=", "/=", "<", ">", "<=", ">=",
    "logand", "logior", "logxor", "ash",
    # lists and sequences
    "list", "cons", "car", "cdr", "cadr", "cddr", "caddr", "first", "second",
    "third", "rest", "last", "nth", "nthcdr", "append", "reverse", "length",
    "member", "assoc", "subseq", "remove", "remove-if", "remove-if-not",
    "remove-duplicates", "mapcar", "mapc", "reduce", "count", "count-if", "find",
    "find-if", "position", "position-if", "some", "every", "sort", "null",
    "listp", "consp", "atom", "iota", "make-list", "copy-list", "butlast",
    # strings and characters
    "string=", "string<", "string>", "string-upcase", "string-downcase",
    "char", "char-code", "code-char", "stringp", "concatenate", "string",
    "princ-to-string", "prin1-to-string", "parse-integer", "char=", "upper-case-p",
    "lower-case-p", "alpha-char-p", "digit-char-p",
    # logic and comparison
    "equal", "eql", "equalp", "not", "identity",
}
_REPL_SPECIAL = {"quote", "function", "lambda", "let", "let*", "if", "when",
                 "unless", "cond", "and", "or", "progn", "dolist", "dotimes"}
#: higher-order functions: which argument is the function designator
_REPL_HOF = {"mapcar": 0, "mapc": 0, "reduce": 0, "remove-if": 0,
             "remove-if-not": 0, "count-if": 0, "find-if": 0, "position-if": 0,
             "some": 0, "every": 0, "sort": 1}
_REPL_KEYWORDS = {":key", ":test", ":initial-value", ":from-end", ":start", ":end"}
_REPL_VARS = {"t", "nil", "pi"}


def safe_expr_check(text, tool_names):
    """Return an error string if TEXT is outside the safe typed-REPL subset.

    Allowed: numbers, strings, quoted data, the vocabulary above, lambda, LET,
    IF/COND/AND/OR, DOLIST/DOTIMES and calls of saved tools. Everything that
    does I/O, reads or evals, builds symbols, reaches into packages, or could
    call a function by name is rejected, and a function passed to a
    higher-order operation must be a LAMBDA or (FUNCTION allowed-name), never a
    quoted symbol (``(mapcar 'run-program ...)`` would otherwise slip through).
    """
    if not isinstance(text, str) or not text.strip() or len(text) > 600:
        return "type one Lisp expression (up to 600 characters)"
    text = re.sub(r"#'([^\s()'#]+)", r"(function \1)", text)
    if "#" in text or "|" in text or "\\" in text or "`" in text or "," in text:
        return "reader syntax is not allowed here (#, |, backslash, backquote, comma)"
    try:
        form = s_expr.parse(text)
    except s_expr.SExprError as exc:
        return "could not parse: %s" % exc
    funcs = _REPL_FUNCS | {n.lower() for n in tool_names}

    def err(msg):
        return msg

    def designator(node):
        if isinstance(node, list) and len(node) == 2 and isinstance(node[0], str) \
                and node[0].lower() == "function" and isinstance(node[1], str):
            return None if node[1].lower() in funcs else \
                "function %s is not allowed" % node[1]
        if isinstance(node, list) and node and isinstance(node[0], str) \
                and node[0].lower() == "lambda":
            return None
        return ("pass a function as a lambda or #'name of an allowed function, "
                "not a quoted symbol")

    def walk(node, scope, depth):
        if depth > 40:
            return err("expression is nested too deeply")
        if isinstance(node, bool) or isinstance(node, (int, float, s_expr.SString)):
            return None
        if isinstance(node, str):
            low = node.lower()
            if low.startswith(":"):
                return None if low in _REPL_KEYWORDS else \
                    "keyword %s is not allowed" % node
            if ":" in low:
                return "package-qualified symbols are not allowed (%s)" % node
            if low in scope or low in _REPL_VARS:
                return None
            return "variable or function %s is not available" % node
        if not isinstance(node, list):
            return "unsupported value"
        if not node:
            return None
        head = node[0]
        if not isinstance(head, str) or isinstance(head, s_expr.SString):
            return "the first element of a form must be a function name"
        h = head.lower()
        rest = node[1:]
        if h == "quote":
            return None if len(rest) == 1 else "quote takes one argument"
        if h == "function":
            return designator(node)
        if h == "lambda":
            if not rest or not isinstance(rest[0], list) or \
                    not all(isinstance(p, str) and ":" not in p for p in rest[0]):
                return "lambda needs a plain parameter list"
            inner = scope | {p.lower() for p in rest[0]}
            for body in rest[1:]:
                bad = walk(body, inner, depth + 1)
                if bad:
                    return bad
            return None
        if h in ("let", "let*"):
            if not rest or not isinstance(rest[0], list):
                return "let needs a binding list"
            inner = set(scope)
            for b in rest[0]:
                if isinstance(b, str):
                    inner.add(b.lower())
                    continue
                if not (isinstance(b, list) and len(b) == 2 and isinstance(b[0], str)):
                    return "let bindings must look like (name value)"
                bad = walk(b[1], inner if h == "let*" else scope, depth + 1)
                if bad:
                    return bad
                inner.add(b[0].lower())
            for body in rest[1:]:
                bad = walk(body, inner, depth + 1)
                if bad:
                    return bad
            return None
        if h in ("dolist", "dotimes"):
            if not rest or not isinstance(rest[0], list) or len(rest[0]) < 2 \
                    or not isinstance(rest[0][0], str):
                return "%s needs (var form)" % h
            bad = walk(rest[0][1], scope, depth + 1)
            if bad:
                return bad
            inner = scope | {rest[0][0].lower()}
            for body in rest[0][2:] + rest[1:]:
                bad = walk(body, inner, depth + 1)
                if bad:
                    return bad
            return None
        if h == "cond":
            for clause in rest:
                if not isinstance(clause, list):
                    return "cond clauses must be lists"
                for part in clause:
                    if isinstance(part, str) and part.lower() == "t":
                        continue
                    bad = walk(part, scope, depth + 1)
                    if bad:
                        return bad
            return None
        if h in _REPL_SPECIAL:                # if when unless and or progn
            for part in rest:
                bad = walk(part, scope, depth + 1)
                if bad:
                    return bad
            return None
        if h not in funcs:
            return "function %s is not allowed in the typed REPL" % head
        hof = _REPL_HOF.get(h)
        for i, part in enumerate(rest):
            if hof is not None and i == hof:
                bad = designator(part)
                if bad is None and isinstance(part, list) and part and \
                        part[0].lower() == "lambda":
                    bad = walk(part, scope, depth + 1)
                if bad:
                    return bad
                continue
            bad = walk(part, scope, depth + 1)
            if bad:
                return bad
        return None

    return walk(form, set(), 0)


class SessionManager:
    """Runs sessions on background threads; one at a time."""

    def __init__(self, registry=None, generators=None):
        self.registry = registry or ToolRegistry()
        self.generators = generators or {"demo": demo_generate,
                                         "live": live_generate}
        self._sessions = {}
        self._busy = False
        self.lessons = orc.LessonStore(AGENT_DIR / "lessons.json")
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
                           expected=expected, oracle=oracle,
                           lessons=self.lessons,
                           postmortem_dir=AGENT_DIR / "postmortems")
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
                        expected=sess.expected, oracle=sess.oracle,
                        lessons=self.lessons,
                        postmortem_dir=AGENT_DIR / "postmortems")
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

    def call_tool(self, text, mode=None):
        """Run one allow-listed call of a saved tool. Zero model tokens."""
        reg = self.registry.for_mode(mode) if mode else self.registry
        names = {t["name"] for t in reg.load()}
        text = quote_literals(text) if isinstance(text, str) else text
        problem = safe_expr_check(text, names)
        if problem:
            return {"ok": False, "error": problem, "tokens": 0}
        prelude = reg.prelude()
        text = re.sub(r"#'([^\s()'#]+)", r"(function \1)", text)
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

    def tools(self, mode=None):
        reg = self.registry.for_mode(mode) if mode else self.registry
        return [{"name": t.get("name"), "description": t.get("description"),
                 "definition": t.get("definition"), "session": t.get("session"),
                 "created": t.get("created"), "uses": t.get("uses", 0),
                 "tests": t.get("tests", []),
                 "prompts": len(t.get("prompts", [])),
                 "mode": t.get("mode") or "demo"}
                for t in reg.load()]
