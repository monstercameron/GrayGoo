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
import projects
import risk
import s_expr
import toolmeta
import webkit
import lispstyle
import oracle as orc
import workers

ROOT = Path(__file__).resolve().parent
AGENT_DIR = ROOT / "artifacts" / "agent"
MAX_MODEL_CALLS = 5          # per session: quick check + plan + repairs
MAX_REPAIRS = 3
REWRITE_TEMPERATURE = 0.7    # fresh-rewrite repairs sample for a different idea
MAX_PLAN_STEPS = 6
MAX_MODEL_CALLS_PLAN = 80    # when a goal is split into small tools
# Measured on 80 live sessions: a "thinking" rewrite passed its tests 23% of
# the time, a plain repair 24%, at four times the cost and over three times
# the wait. So thinking is the exception: only a step's LAST attempt, once a
# run. A low-effort deep call gets 3500 tokens (was 5000): one run spent all
# 5000 on reasoning and returned an empty reply.
MAX_DEEP_CALLS = 1
MAX_SPLIT_DEPTH = 1         # a failed step may be split once into smaller tools

# Tolerant test comparison, defined in every rehearsal: numbers compare within
# a relative 1e-4 (so 0.6 equals 3/5), lists elementwise, everything else EQUAL.
GG_CHECK = (
    "(proclaim '(sb-ext:muffle-conditions style-warning sb-ext:compiler-note))\n"
    "(defun gg-near (a b) (cond ((and (realp a) (realp b)) "
    "(<= (abs (- a b)) (* 1d-4 (max 1 (abs a) (abs b))))) "
    "((and (consp a) (consp b)) (and (gg-near (car a) (car b)) "
    "(gg-near (cdr a) (cdr b)))) "
    # a test cannot type a line break, so the model writes ~% in the expected
    # string: accept that spelling for a real newline in the result
    "((and (stringp a) (stringp b)) (or (string= a b) (string= a (gg-nl b)))) "
    "(t (equal a b))))\n"
    "(defun gg-nl (s) (let ((p (search \"~%\" s))) (if p (concatenate 'string "
    "(subseq s 0 p) (string #\\Newline) (gg-nl (subseq s (+ p 2)))) s)))\n"
    "(defun gg-check (got want) (if (gg-near got want) t (list :got got)))")
LIVE_SPEND_CAP_USD = 1.00   # per server process; live sessions refuse past it
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
    "literals, e.g. '(1 2 3). "
) + lispstyle.STYLE_GUIDE


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


def tool_line(t, with_example=True):
    """``TOOL name (args): description  e.g. (call) => value`` for prompts.

    The example is the tool's first passing test, so the model sees the exact
    argument SHAPES (lists, nesting, units) that are known to work.
    """
    line = "TOOL %s %s: %s" % (t["name"], signature(t["definition"]).split(" ", 1)[1][:-1],
                              t.get("description", ""))
    if with_example:
        tests = [x for x in (t.get("tests") or []) if isinstance(x, dict)
                 and x.get("call") and (x.get("expect") or "").strip().upper() != "T"]
        tests = tests or [x for x in (t.get("tests") or []) if isinstance(x, dict) and x.get("call")]
        if tests:
            ex = tests[0]
            line += "  e.g. %s => %s" % (ex["call"][:110], str(ex.get("expect"))[:50])
    return line


FOCUS_OVER = 8          # registries larger than this are sent focused
FOCUS_RECENT = 4        # the newest tools are the likeliest dependencies


def compact_line(t):
    """``name (args): first words of the description`` - a tool the call may use."""
    desc = " ".join((t.get("description") or "").split())
    return "%s %s%s" % (t["name"], signature(t["definition"]).split(" ", 1)[1][:-1],
                        ": " + (desc[:57] + "..." if len(desc) > 60 else desc) if desc else "")


def relevant_tools(tools, text):
    """Names of the tools TEXT is about: mentioned by name, or the newest few."""
    text = text or ""
    named = {t["name"] for t in tools
             if re.search(r"(?<![^\s('\"#])%s(?![^\s)\".,;:])" % re.escape(t["name"]),
                          text, re.I)}
    recent = {t["name"] for t in tools[-FOCUS_RECENT:]}
    return named | recent


def registry_text(tools, focus=None):
    """The REGISTRY block. With FOCUS (the call's own text) and a large registry,
    only relevant tools get a full line with an example; the rest are listed
    compactly so the model still knows they exist and how to call them."""
    if not tools:
        return "(no tools yet)"
    if focus is None or len(tools) <= FOCUS_OVER:
        return "\n".join(tool_line(t) for t in tools)
    keep = relevant_tools(tools, focus)
    full = [tool_line(t) for t in tools if t["name"] in keep]
    rest = [compact_line(t) for t in tools if t["name"] not in keep]
    return "\n".join(full) + ("\nOTHER SAVED TOOLS (callable the same way): "
                              + "; ".join(rest) if rest else "")


# One line for every non-planning call of a web app (the full contract goes to the planner).
WEB_REMINDER = (
    "WEB APP: REQUEST is a plist (read it with the kit tools or GETF, never "
    "ASSOC); STATE is a list of (name rows) tables, e.g. '((\"posts\" ()) "
    "(\"users\" ())); responses are plists made with html-page, redirect-to, "
    "with-state and with-cookie.")

# Fixing test calls needs the output format and the data rules, not the whole build brief.
TEST_SYSTEM = (
    "You repair the TESTS of a Common Lisp tool. Answer with ONE JSON object and "
    "nothing else: {\"action\":\"build\",\"name\":\"...\",\"description\":\"...\","
    "\"definition\":\"(defun ...)\",\"tests\":[{\"call\":\"(name ...)\","
    "\"expect\":\"printed result\"}],\"call\":\"(name ...)\"}. Keep the name, "
    "description and definition exactly as given. Each call is exactly one Lisp "
    "form that calls the tool. Quote data lists with one leading quote, e.g. "
    "'(1 \"a\"), and put no quote marks inside quoted data. 'expect' is the "
    "PRIN1 text of the result, e.g. \"25\" or \"\\\"abc\\\"\" or \"(1 2)\"; use "
    "a property test with expect T when the exact value is long or uncertain. "
    "Only call functions that exist: the tool itself and the tools listed in "
    "the REGISTRY.")


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

    def load(self, retired=False):
        """Visible tools. Retired ones (kept on disk, with the reason) only on request."""
        return [t for t in self._raw() if self._mine(t) and (retired or not t.get("retired"))]

    def retire(self, reasons):
        """Mark tools as retired: REASONS maps a tool name to why. Returns the names."""
        done = []
        with self._lock:
            tools = self._raw()
            for t in tools:
                if self._mine(t) and t.get("name") in reasons and not t.get("retired"):
                    t["retired"] = reasons[t["name"]]
                    done.append(t["name"])
            if done:
                self._write(tools)
        return done

    def add(self, tool):
        with self._lock:
            tool = dict(tool)
            if self.mode:
                tool["mode"] = self.mode
            tools = [t for t in self._raw()
                     if not (t.get("name") == tool["name"] and self._mine(t))]
            tool.pop("retired", None)
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
    effort = getattr(_TEMP, "effort", "low")
    if effort != "medium":
        return _with_retry(lambda: cerebras_client.generate(
            user, system=system, max_tokens=3500, temperature=temp,
            reasoning_effort="low", timeout=120.0))
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


# Forms whose arguments are clauses, never data: nothing inside them is quoted.
_CLAUSE_HEADS = frozenset({
    "cond", "case", "ccase", "ecase", "typecase", "etypecase", "handler-case",
    "handler-bind", "restart-case", "do", "do*"})
# Forms whose FIRST argument is a binding or lambda list (not data).
_BIND_HEADS = frozenset({
    "let", "let*", "flet", "labels", "macrolet", "symbol-macrolet",
    "destructuring-bind", "multiple-value-bind", "dolist", "dotimes",
    "with-slots", "defun", "defmacro", "lambda"})
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?(?![^\s()])")
_SYM_RE = re.compile(r"[^\s()\"';]+")
_SPACE_RE = re.compile(r"\s*")


def _head_of(text, i):
    """Classify the first element of the list whose ``(`` is just before I.

    Returns ``(kind, head, end)``: kind is ``str``, ``num``, ``list`` (a nested
    or quoted list), ``sym`` (HEAD is the lower-cased symbol, END is the index
    just past it) or None for an empty list.
    """
    j = _SPACE_RE.match(text, i).end()
    if text.startswith('"', j):
        return "str", None, j
    if text.startswith("(", j) or text.startswith("'(", j):
        return "list", None, j
    if _NUM_RE.match(text, j):
        return "num", None, j
    m = _SYM_RE.match(text, j)
    if m:
        return "sym", m.group(0).lower(), m.end()
    return None, None, j


def quote_literals(text):
    """Quote list data that the model wrote without a quote mark.

    ``(3 4 5)`` becomes ``'(3 4 5)``, since Lisp would read it as a call of 3.
    An argument list whose first element is a string, number or list, such as
    ``(("users" ()))`` or ``('(1 "a"))``, is data too and is quoted, with its
    inner quote marks stripped. Lists already quoted, ``nil``, and code such as
    ``(f (g x))`` stay as written. Binding and clause forms (``let``, ``cond``,
    lambda lists) are never touched, and strings and ``#\\x`` characters are
    copied verbatim.
    """
    out, i, n = [], 0, len(text)
    depth, qdepth = 0, None
    frames = []          # one (kind, head, end, start) per open "("
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
            continue
        if c == "#" and text[i + 1:i + 2] == "\\":
            out.append(text[i:i + 3])           # character literal, e.g. #\(
            i += 3
            continue
        if c == "'" and qdepth is not None and text[i + 1:i + 2] == "(":
            i += 1                 # nested quote inside quoted data: (a '(b)) -> (a (b))
            continue
        if c == "(":
            kind, head, end = _head_of(text, i + 1)
            enc_kind, enc_head, enc_end, enc_start = \
                frames[-1] if frames else (None, None, 0, 0)
            is_sym = enc_kind == "sym"
            if is_sym:
                # a direct argument of a clause or binding form is never data
                first_arg = text[enc_end:i].strip() == ""
                skip = enc_head in _CLAUSE_HEADS or \
                    (enc_head in _BIND_HEADS and first_arg)
            else:
                # a clause or binding list itself, e.g. ((1 2) "low") in a case
                gp_kind, gp_head, gp_end, _ = \
                    frames[-2] if len(frames) > 1 else (None, None, 0, 0)
                skip = gp_kind == "sym" and (
                    gp_head in _CLAUSE_HEADS or
                    (gp_head in _BIND_HEADS and
                     text[gp_end:enc_start].strip() == ""))
            if out and out[-1].endswith("'") and qdepth is None and \
                    not (len(out) > 1 and out[-2] == "#"):   # not #'(lambda ...)
                qdepth = depth
            elif qdepth is None and text.startswith("(quote ", i):
                qdepth = depth
            elif qdepth is None and not skip and (
                    kind == "num" or (is_sym and kind in ("str", "list"))):
                out.append("'")
                qdepth = depth
            depth += 1
            frames.append((kind, head, end, i))
        elif c == ")":
            depth -= 1
            if frames:
                frames.pop()
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


_EG_RE = re.compile(r"e\.g\.\s*\(")


def _quote_spec_examples(spec):
    """Quote the call in each ``e.g. <call> => <value>`` example of a spec.

    Only the call is touched, and only when it is a call (its head is a
    symbol). The rest of the spec text is left exactly as written.
    """
    out, pos = [], 0
    for m in _EG_RE.finditer(spec):
        start = m.end() - 1                     # index of the example's "("
        if start < pos:
            continue
        end = _form_end(spec, start)
        if end < 0 or not re.match(r"\s*=>", spec[end + 1:]):
            continue
        call = spec[start:end + 1]
        if _head_of(call, 1)[0] != "sym":
            continue
        fixed = quote_literals(call)
        if fixed != call:
            out.append(spec[pos:start])
            out.append(fixed)
            pos = end + 1
    out.append(spec[pos:])
    return "".join(out)


def balance_call(call, definition):
    """Put back the ``)`` a model dropped inside nested data in a test call.

    ``(db-insert '(("users" (("bob" "x"))) "users" '("a"))`` is one paren
    short, and the model repeats the slip when asked to recount. The missing
    paren is restored only when exactly one placement makes the call pass as
    many arguments as the tool's ``defun`` takes; otherwise CALL is returned
    unchanged and validation reports it. Tools with ``&optional``/``&rest``
    parameters are left alone, since their argument count proves nothing.
    """
    m = re.match(r"\s*\(defun\s+(\S+)\s+\(([^)]*)\)", definition or "")
    if not m or "&" in m.group(2):
        return call
    name, argc = m.group(1).lower(), len(m.group(2).split())
    closes, depth, i, n = [], 0, 0, len(call)
    while i < n:                                  # ")" positions outside strings
        c = call[i]
        if c == '"':
            i += 1
            while i < n and call[i] != '"':
                i += 2 if call[i] == "\\" else 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            closes.append(i)
        i += 1
    if depth not in (1, 2) or not closes:
        return call
    found = set()
    for p in closes:
        cand = call[:p + 1] + ")" * depth + call[p + 1:]
        try:
            form = s_expr.parse(cand)
        except s_expr.SExprError:
            continue
        if isinstance(form, list) and len(form) == argc + 1 and \
                str(form[0]).lower() == name:
            found.add(cand)
    return found.pop() if len(found) == 1 else call


_STR_LIT = re.compile(r'"(?:[^"\\]|\\.)*"')


def newline_escapes(text):
    """Turn ``\\n`` inside string literals into a real line break.

    Lisp has no ``\\n`` escape (it reads as the letter n), so a model that
    writes it always means a newline.
    """
    return _STR_LIT.sub(lambda m: m.group(0).replace("\\n", "\n"), text)


def quote_bare_string(expect):
    """Wrap an expected value in quotes when it can only be an unquoted string.

    A printed Lisp value is exactly one datum. ``[ ] 1. Buy milk`` or
    ``No tasks.`` is several bare words, so the model meant the string.
    """
    text = expect.strip()
    if not text or text[0] in "\"('#:" or text.upper() in ("T", "NIL"):
        return expect
    if " " not in text:
        return expect
    return '"%s"' % text.replace("\\", "\\\\").replace('"', '\\"')


def one_line(printed):
    """A printed value on one line: line breaks INSIDE strings become a visible
    ``\\n``; layout whitespace between elements becomes one space."""
    out, in_str, i, n = [], False, 0, len(printed)
    while i < n:
        c = printed[i]
        if in_str:
            if c == "\\" and i + 1 < n:
                out.append(printed[i:i + 2])
                i += 2
                continue
            if c == '"':
                in_str = False
            out.append("\\n" if c == "\n" else c)
        elif c == '"':
            in_str = True
            out.append(c)
        elif c in " \t\r\n":
            if out and out[-1] != " ":
                out.append(" ")
        else:
            out.append(c)
        i += 1
    return "".join(out).strip()


def trim_surplus_parens(text):
    """Drop extra ``)`` after the end of the one top-level form.

    ``(defun f (x) x))`` cannot be read at all, so the model only ever hears
    "unexpected ')'". Without the surplus the compiler reads the code and can
    name the real mistake. Anything other than stray ``)`` after the form is
    left for validation to report.
    """
    start = text.find("(")
    if start < 0:
        return text
    end = _form_end(text, start)
    rest = text[end + 1:] if end >= 0 else ""
    if end >= 0 and rest.strip() and not rest.replace(")", "").strip():
        return text[:end + 1]
    return text


def _usable_test(t, name):
    """A test the harness can run: one Lisp form that calls the tool NAME."""
    if not (isinstance(t, dict) and isinstance(t.get("call"), str)):
        return False
    try:
        form = s_expr.parse(t["call"])
    except s_expr.SExprError:
        return False
    return isinstance(form, list) and bool(form) and bool(re.search(
        r"(?<![^\s('])%s(?![^\s)])" % re.escape(name), t["call"], re.I))


def normalize_plan(plan):
    """Balance and quote every call in a build/use plan, in place.

    ``plan["auto_fixes"]`` lists the repairs that changed something (for the
    session log; it tells us which model slips are common enough to matter).
    """
    fixes = []

    def step(name, fn, text):
        out = fn(text)
        if out != text and name not in fixes:
            fixes.append(name)
        return out

    if isinstance(plan.get("definition"), str):
        d = step("trimmed-surplus-paren", trim_surplus_parens, plan["definition"])
        d = step("completed-missing-paren", complete_parens, d)
        d = step("flattened-setf", fix_setf, d)
        d = step("let-to-let-star", lispstyle.let_to_let_star, d)
        if plan.get("action") == "build":
            # every tool states its own interface, even when the model forgot
            d = step("added-docstring",
                     lambda x: lispstyle.ensure_docstring(x, plan.get("description")), d)
        plan["definition"] = d
    defn = plan.get("definition") if isinstance(plan.get("definition"), str) else ""

    parts = lispstyle.defun_parts(defn) if defn else None
    state_at = parts[1].index("state") if parts and "state" in parts[1] else None

    def fix_call(text):
        text = step("restored-paren-in-call", lambda x: balance_call(x, defn), text)
        text = step("quoted-data-list", quote_literals, text)
        if state_at is not None:
            # the STATE argument is always a list of (name rows) tables
            text = step("nested-state-data",
                        lambda x: webkit.fix_state_args(x, parts[0], state_at), text)
        return text

    for t in plan.get("tests") or []:
        if isinstance(t, dict) and isinstance(t.get("expect"), str):
            e = step("quoted-bare-expected-string", quote_bare_string, t["expect"])
            e = step("newline-escape-in-expected-string", newline_escapes, e)
            t["expect"] = step("completed-paren-in-expected-value", complete_parens, e)

    if isinstance(plan.get("call"), str):
        plan["call"] = fix_call(plan["call"])
    for t in plan.get("tests") or []:
        if isinstance(t, dict) and isinstance(t.get("call"), str):
            t["call"] = fix_call(t["call"])
    # Tests that are prose or call some other (often non-existent) function are
    # dropped when usable tests remain: one bad extra test must not cost a
    # whole repair round. With none left, validation reports the problem.
    if plan.get("action") == "build" and isinstance(plan.get("name"), str) and \
            isinstance(plan.get("tests"), list):
        usable = [t for t in plan["tests"] if _usable_test(t, plan["name"])]
        if usable and len(usable) < len(plan["tests"]):
            plan["dropped_tests"] = [t.get("call") if isinstance(t, dict) else str(t)
                                     for t in plan["tests"] if t not in usable]
            plan["tests"] = usable
            fixes.append("dropped-unusable-test")
    for item in plan.get("steps") or []:
        if isinstance(item, dict) and isinstance(item.get("spec"), str):
            item["spec"] = step("quoted-spec-example", _quote_spec_examples, item["spec"])
            sig = re.match(r"\s*\(\s*([^\s()]+)((?:\s+[^\s()]+)*)\s*\)", item["spec"])
            names = sig.group(2).split() if sig else []
            if "state" in names:
                item["spec"] = step(
                    "nested-state-data",
                    lambda x: webkit.fix_state_args(x, sig.group(1), names.index("state")),
                    item["spec"])
    if fixes:
        plan["auto_fixes"] = fixes
    return plan


class BudgetExhausted(RuntimeError):
    """The session reached its model-call limit."""


class BadReply(ValueError):
    """The model's reply was not usable JSON even after the retries."""


def _loads_tolerant(raw):
    """``json.loads`` plus repairs for the slips real models make.

    Tried in order: the text as is, then a closing quote the model forgot just
    before a ``}`` or ``]`` (it wrote ``"expect":"(1 2)}`` ). Only a result that
    parses to an object is accepted. A reply that was CUT OFF is not patched
    here: its content is incomplete, so it must be asked for again.
    """
    try:
        return json.loads(raw)
    except ValueError as exc:
        first = exc
    try:
        data = json.loads(raw, strict=False)     # literal newlines or tabs in a string
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    closers = [i for i, c in enumerate(raw) if c in "}]"]
    for p in reversed(closers[-80:]):
        try:
            data = json.loads(raw[:p] + '"' + raw[p:])
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    raise first


def extract_json(text):
    """Pull the first JSON object out of model text, repairing small slips."""
    if not isinstance(text, str):
        raise ValueError("empty model reply")
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in reply")
    try:
        return _extract_balanced(text, start)
    except ValueError as exc:
        end = text.rfind("}")
        raw = text[start:end + 1] if end > start else text[start:]
        try:
            data = _loads_tolerant(raw.rstrip().rstrip("`").rstrip())
        except ValueError:
            raise exc
        if not isinstance(data, dict):
            raise exc
        return data


def _extract_balanced(text, start):
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


JSON_RETRY_TEMPS = (0.4, 0.8)     # a retry at the same temperature repeats the slip


def validate_build(plan, frozen=None):
    """Return an error string for a malformed build plan, else None."""
    name = plan.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return "name must be lowercase kebab-case"
    definition = plan.get("definition")
    if not isinstance(definition, str) or not definition.strip():
        return "definition missing"
    for problem in lispstyle.purity_problems(definition):
        return "not a pure function: " + problem
    for problem in lispstyle.case_problems(definition):
        return problem
    for problem in lispstyle.security_problems(name, plan.get("description"), definition):
        return "unsafe secret comparison: " + problem
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
        if "#" in re.sub(r'"(?:[^"\\]|\\.)*"', '""', t["expect"]):
            return ("expected values cannot contain '#': write plain lists like "
                    "(0 0 -5), not #(0 0 -5) vectors or other reader syntax")
    for t in tests:
        bare = re.sub(r'"(?:[^"\\]|\\.)*"', '""', t["expect"])
        if bare.count("(") != bare.count(")"):
            return ("expected value %s has unbalanced parentheses (%d opening, %d "
                    "closing); write the whole printed result"
                    % (t["expect"], bare.count("("), bare.count(")")))
    for t in tests:
        if t["call"].count("(") != t["call"].count(")") and '"' not in \
                re.sub(r'"(?:[^"\\]|\\.)*"', "", t["call"]):
            bare = re.sub(r'"(?:[^"\\]|\\.)*"', "", t["call"])
            if bare.count("(") != bare.count(")"):
                return ("test call %s has unbalanced parentheses (%d opening, %d "
                        "closing); count them and resend every test call balanced"
                        % (t["call"], bare.count("("), bare.count(")")))
        try:
            call_form = s_expr.parse(t["call"])
        except s_expr.SExprError:
            call_form = None
        if not (isinstance(call_form, list) and call_form):
            return ("test call %s must be exactly one Lisp call such as (%s ...), "
                    "with no prose or second form" % (t["call"], name))
        if not re.search(r"(?<![^\s('])%s(?![^\s)])" % re.escape(name),
                         t["call"], re.I):
            return ("test call %s does not call the tool %s; every test must call "
                    "%s" % (t["call"], name, name))
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


# -- capability gaps: what the pure-Lisp sandbox cannot do, read from the goal --
# (need, instead, pattern). Patterns are whole-word and case-insensitive; the
# misspellings listed are the ones seen in real prompts.
_CAPABILITY_RULES = [
    ("database",
     "the app's STATE value (a Lisp list of tables); the harness saves it in "
     "SQLite between requests when the project is mounted",
     re.compile(r"\b(?:sqlite\d?|sql\s*-?\s*lite|sqllite|sqlight|postgres(?:ql)?|"
                r"mysql|mariadb|data ?bas\w*|databse|sql|persist\w*)\b", re.I)),
    ("command line",
     "a pure (handle-command args state now) function; the harness runs it for "
     "each command the user types (Run a command, or mount.py --shell)",
     re.compile(r"\b(?:cli|tui|command[- ]?line|terminal|console (?:app|tool|program)|"
                r"text (?:ui|interface|user interface)|shell (?:app|tool)|"
                r"interactive (?:prompt|menu))\b", re.I)),
    ("web server",
     "a pure (handle-request request state) function; the harness mounts it "
     "on a port (Run server) and calls it for every request",
     re.compile(r"\b(?:ssr|server[- ]side\w*|(?:web|http|api) ?servers?|http ?requests?|"
                r"(?:api|rest|http|web|server)\s+endpoints?|"
                r"endpoints?\s+(?:for|that|to)\s+(?:the\s+)?(?:api|web|site|server|browsers?|clients?|users?)|"
                r"(?:web|http|url|api)\s+routes?|websites?|web ?sites?|blogs?|"
                r"web ?apps?|web ?pages?|served\s+(?:to|in)\s+(?:a\s+|the\s+)?browsers?)\b", re.I)),
    ("login security",
     "login written in plain Lisp using the request's :nonce and :now; no "
     "vetted password hashing — not safe for real accounts",
     re.compile(r"\b(?:log-?\s?ins?|sign-?\s?ins?|auth|authent\w*|authori[sz]\w*|"
                r"passwords?|passwd|credentials?|security|"
                r"sucurity|secuirty|securty|sercurity|sessions?|encrypt\w*|"
                r"decrypt\w*|cryptograph\w*|bcrypt|argon2|scrypt|pbkdf2)\b", re.I)),
    ("file system",
     "no file access in the sandbox",
     re.compile(r"\b(?:(?:read|reads|write|writes|save|saves|load|loads|open|opens|store|stores)"
                r"\s+(?:\w+\s+){0,3}?(?:files?|disk|folders?|director(?:y|ies))|"
                r"(?:to|on|from)\s+(?:a\s+|the\s+)?(?:disk|file|filesystem|file system))\b", re.I)),
    ("network connection",
     "no network access in the sandbox",
     re.compile(r"\b(?:networks?|networking|internet|downloads?|downloading|downloaded|"
                r"scrap(?:e|es|ed|ing)|web ?requests?|"
                r"fetch(?:es|ing)?\s+(?:from|over|data|(?:a|the)\s+(?:url|page|web|site|remote|api))|"
                r"(?:call|calls|calling|query|queries|querying)\s+(?:an?\s+|the\s+|external\s+|remote\s+)?"
                r"(?:api|apis|web ?services?)|api\s+calls?)\b", re.I)),
]


# The request/response contract of a mounted app (kept in step with mount.py).
WEB_APP_CONTRACT = (
    "WEB APP CONTRACT - the harness mounts the app on a port; you write only "
    "pure functions. The top-level tool MUST be (handle-request request state). "
    "REQUEST is a plist: (:method \"GET\" :path \"/posts\" :query ((\"k\" \"v\")) "
    ":form ((\"title\" \"Hi\")) :cookies ((\"sid\" \"abc\")) :now 1791560000 "
    ":nonce \"9f2c41aa\"). Read it with (getf request :path) and "
    "(second (assoc \"title\" (getf request :form) :test #'string=)). "
    "STATE is the app's whole data, a list of tables such as "
    "'((\"posts\" ((\"Hi\" \"text\"))) (\"users\" ()) (\"sessions\" ())); it is "
    "NIL on the very first request unless you also build a zero-argument tool "
    "initial-state. The harness saves the state you return in SQLite and "
    "passes it back on the next request. "
    "RETURN a plist: (:status 200 :headers ((\"Content-Type\" \"text/html\")) "
    ":body \"<html>...</html>\" :state new-state). Leave :state out when "
    "nothing changed. Redirect with :status 303 and a (\"Location\" \"/\") "
    "header; set a cookie with a (\"Set-Cookie\" \"sid=VALUE; HttpOnly; Path=/\") "
    "header. Stay pure: take the time from :now and randomness (session ids, "
    "salts) from :nonce. Escape all user text before putting it in HTML. "
    "Keep handle-request small: it only routes on method and path to other "
    "tools, each of which is (request state) -> response plist or a helper. "
) + webkit.USAGE


CLI_APP_CONTRACT = (
    "COMMAND-LINE APP CONTRACT - the harness runs the app; you write only pure "
    "functions. The top-level tool MUST be (handle-command args state now). ARGS "
    "is the list of words the user typed, e.g. '(\"add\" \"Buy milk\") or "
    "'(\"list\") or '(\"done\" \"2\"); numbers arrive as strings, so use "
    "(parse-integer word :junk-allowed t). STATE is the app's whole data, a list "
    "of (name rows) tables such as '((\"tasks\" ((1 \"Buy milk\" \"pending\" "
    "1700000000)))); it is NIL on the first run unless you build a zero-argument "
    "tool initial-state. NOW is the current time in whole seconds, passed in by "
    "the harness: functions must never read the clock or random numbers "
    "themselves. RETURN a plist (:output \"text to show\" :state new-state); "
    "leave :state out when nothing changed. :output is everything the user "
    "sees, so format it as readable lines; an unknown command returns usage "
    "help. These tested tools are already in the REGISTRY, use them and do NOT "
    "rebuild them: (table-rows state \"tasks\"), (with-table-rows state \"tasks\" "
    "rows), (join-strings strings separator). Test data for STATE always starts "
    "with two opening parentheses. "
    "COMMANDS: handle-command dispatches on (first args) and passes (rest args) "
    "to ONE tool per command, named cmd-<word>, with signature (cmd-<word> args "
    "state now) where ARGS holds only the words AFTER the command word, e.g. "
    "(cmd-add '(\"Buy\" \"milk\") state now); cmd-add joins its title words with "
    "(join-strings args \" \"). Each cmd tool returns the same RETURN plist. "
    "Keep handle-command small: it only dispatches."
)


def stale_tools(tools):
    """``{name: reason}`` for saved tools that no longer fit the project.

    A project keeps what earlier, abandoned plans saved. Such a tool poisons
    later runs: the model sees it in the registry, calls it, and inherits its
    old data layout. A tool is stale when it breaks a purity rule, when its code looks
    the app data up by keyword instead of as (name rows) tables, or when it
    calls a stale tool. The rule is deliberately narrow: a working app must
    never lose a function to a guess.
    """
    reasons = {}
    for t in tools:
        if t.get("kit"):
            continue
        problems = lispstyle.purity_problems(t.get("definition") or "")
        if problems:
            reasons[t["name"]] = "breaks a rule for pure functions: " + problems[0]
            continue
        # the definition itself looks app data up by KEYWORD: the layout of an
        # abandoned plan, incompatible with the project's (name rows) tables
        code = lispstyle.code_only(t.get("definition") or "")
        parts = lispstyle.defun_parts(t.get("definition") or "")
        if parts and "state" in parts[1] and re.search(
                r"\(\s*assoc\s+:[\w-]+\s+state(?![^\s)])", code, re.I) and "keywordp" not in code:
            reasons[t["name"]] = ("reads app data by keyword, a different layout than the "
                                  "project's list of (name rows) tables")
    changed = True
    while changed:                                # whatever calls a stale tool is stale too
        changed = False
        for t in tools:
            if t["name"] in reasons or t.get("kit"):
                continue
            code = lispstyle.code_only(t.get("definition") or "")
            hit = next((n for n in reasons if re.search(
                r"(?<![^\s('#])%s(?![^\s)])" % re.escape(n), code, re.I)), None)
            if hit:
                reasons[t["name"]] = "calls %s, which was retired" % hit
                changed = True
    return reasons


def capability_gaps(prompt):
    """Return [{"need", "instead"}] for each thing the goal asks for that the
    sandbox cannot do. Pure keyword matching: no model call, no I/O."""
    text = str(prompt or "")
    return [{"need": need, "instead": instead}
            for need, instead, pat in _CAPABILITY_RULES if pat.search(text)]


def capability_note(gaps):
    """Planner-prompt paragraph telling the model what to substitute. Empty
    when there are no gaps, so ordinary prompts are unchanged."""
    if not gaps:
        return ""
    lines = "; ".join("%s: %s" % (g["need"], g["instead"]) for g in gaps)
    text = ("CAPABILITY LIMITS: your code cannot itself provide: " + lines + ". "
            "Build what is named after each colon and never claim more than that.")
    if any(g["need"] == "web server" for g in gaps):
        text += " " + WEB_APP_CONTRACT
    elif any(g["need"] == "command line" for g in gaps):
        text += " " + CLI_APP_CONTRACT
    return text


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
        self._systems = set()             # system prompt ids already logged
        self._prerun = {}                 # test results computed side by side
        self._slips = []                  # known slips made in this run, in order
        self._warned = set()              # lessons already shown to the model in this run
        self._recurred = set()            # ...that happened again anyway (counted once)
        self._eval_ms = []                # timings of the latest rehearsal's tests
        self.project = projects.BUILTIN   # which program this session works on
        self.project_note = ""            # one line of project context for the model
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
    def _ask(self, user_text, label, system=None, temperature=None, effort="low",
             deep=False):
        """One model call. TEMPERATURE varies the answer at no extra cost; DEEP
        turns reasoning on (slow and expensive) and is rationed per session."""
        if self.model_calls >= self.max_calls:
            raise BudgetExhausted("model call budget (%d) exhausted"
                                  % self.max_calls)
        self.model_calls += 1
        if deep:
            if self._deep_calls >= MAX_DEEP_CALLS:
                deep = False
            else:
                self._deep_calls += 1
        self.emit("model_call", label=label, prompt=user_text,
                  system=self._system_id(system or SYSTEM_PROMPT),
                  temperature=BASE_TEMPERATURE if temperature is None else temperature,
                  deep=deep, effort=effort if deep else "none")
        _TEMP.effort = effort
        _TEMP.value = temperature
        _TEMP.deep = deep
        _TEMP.notify = lambda msg: self.emit("model_wait", message=msg)
        try:
            res = self.generate(system or SYSTEM_PROMPT, user_text)
        finally:
            _TEMP.notify = None
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
            return self._parsed(res.get("text"), label)
        except ValueError as exc:
            err = exc
        # Cut off or malformed JSON. Retry with a bigger budget and a HIGHER
        # temperature each time: at the same temperature the model repeats the
        # same broken reply word for word.
        for temp in JSON_RETRY_TEMPS:
            if self.model_calls >= self.max_calls:
                break
            self.emit("json_retry", reason=str(err))
            if self.lessons is not None:
                self.lessons.record_harness("invalid-json-reply")
            self.model_calls += 1
            retry_text = (
                user_text + "\nYOUR PREVIOUS REPLY WAS CUT OFF OR NOT VALID "
                "JSON (%s). Reply with ONE complete JSON object only. Close "
                "every string with a double quote before the next } or ], "
                "and keep the Lisp definition compact." % str(err)[:120])
            self.emit("model_call", label=label + " (retry: invalid JSON)",
                      prompt=retry_text,
                      system=self._system_id(system or SYSTEM_PROMPT),
                      temperature=temp, deep=False)
            _TEMP.max_tokens = 5000
            _TEMP.value = temp
            try:
                res = self.generate(system or SYSTEM_PROMPT, retry_text)
            finally:
                _TEMP.max_tokens = 2200
                _TEMP.value = None
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
                return self._parsed(res.get("text"), label)
            except ValueError as exc:
                err = exc
        raise BadReply("the model's reply was not valid JSON: %s" % err)

    def _system_id(self, system):
        """Short id of a system prompt; its full text is logged once per session."""
        import hashlib
        sid = hashlib.sha1(system.encode("utf-8")).hexdigest()[:8]
        if sid not in self._systems:
            self._systems.add(sid)
            self.emit("system_prompt", id=sid, chars=len(system), text=system)
        return sid

    def _parsed(self, text, label):
        """The reply as a normalized plan; logs every automatic fix applied to it."""
        strict = True
        try:
            _extract_balanced(text, text.find("{")) if isinstance(text, str) and "{" in text \
                else None
        except ValueError:
            strict = False
        plan = normalize_plan(extract_json(text))
        fixes = plan.pop("auto_fixes", [])
        if not strict:
            fixes = ["repaired-json"] + fixes
        if fixes:
            self.emit("auto_fix", label=label, fixes=fixes,
                      dropped=plan.get("dropped_tests") or [])
            if self.lessons is not None:
                self.lessons.record_fixes(fixes)
        return plan

    def _user_prompt(self, extra="", goal=None, full=False):
        """The user message of a model call.

        ``full`` (the planner) gets every tool with its example. Every other
        call gets a registry focused on what the call is about, the short web
        reminder, and only the two most frequent lessons.
        """
        tools = self.registry.load()
        target = goal or self.prompt
        focus = None if full else "%s\n%s" % (target, extra)
        text = "REGISTRY:\n%s\nGOAL: %s\n%s" % (registry_text(tools, focus), target, extra)
        if self.project_note:
            text = self.project_note + "\n" + text
        if not full and any(t["name"] == "handle-request" or t.get("kit") for t in tools):
            text += "\n" + WEB_REMINDER
        if self.lessons is not None and not full:
            keys = self.lessons.select(self.project, self._slips, limit=2)
            advice = self.lessons.advice(limit=2, brief=True, project=self.project,
                                         session_keys=self._slips)
            if advice:
                text += "\n" + advice
                fresh = [k for k in keys if k not in self._warned]
                self.lessons.mark_shown(fresh)
                self._warned.update(fresh)
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
        except BudgetExhausted:
            # not a crash: the work so far is saved and the next prompt builds on it
            names = [n for n, _ in self._built]
            if self.lessons is not None:
                self.lessons.record_harness("call-limit-reached")
            self.emit("gave_up", attempts=self.model_calls, detail=(
                "Stopped at the limit of %d model calls. %s"
                % (self.max_calls,
                   "Saved so far: %s." % ", ".join(names) if names
                   else "No tool passed its tests yet.")),
                hint=("Send the same prompt again to continue from the saved tools."
                      if names else "Try a smaller, more specific prompt."))
            self.state = "failed"
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
            "capability_gaps": next((e.get("gaps") for e in ev
                                     if e["kind"] == "capability_notice"), None) or [],
        }

    def _run(self):
        tools = self.registry.load()
        self.emit("goal", prompt=self.prompt, mode=self.mode, arm=self.arm,
                  pair=self.pair, expected=self.expected,
                  oracle=self.oracle, project=self.project)
        gaps = capability_gaps(self.prompt)
        if gaps:
            self.emit("capability_notice", gaps=gaps)
        needs = {g["need"] for g in gaps}
        names = {t["name"] for t in tools}
        if "web server" in needs or "handle-request" in names:
            added = webkit.seed(self.registry)
        elif needs & {"command line", "database"} or "handle-command" in names:
            added = webkit.seed(self.registry, webkit.STATE_NAMES)
        else:
            added = []
        if added:
            self.emit("kit_seeded", tools=added)
            tools = self.registry.load()
        if needs or names & {"handle-request", "handle-command"}:
            # leftovers of abandoned plans must not steer this one
            reasons = stale_tools(tools)
            retired = self.registry.retire(reasons)
            if retired:
                self.emit("retired", tools=[{"name": n, "reason": reasons[n]} for n in retired])
                tools = self.registry.load()
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
        # A goal that asks for a whole app is never answered by one saved tool:
        # skip the reuse check and go straight to planning.
        hits = [] if gaps else retrieve(self.prompt, tools)
        if hits:
            lines = [tool_line(t) for t in hits]
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
        note = capability_note(gaps)
        if "WEB APP CONTRACT" not in note and \
                any(t["name"] == "handle-request" for t in tools):
            note = (note + " " + WEB_APP_CONTRACT).strip()   # follow-up on a web app
        elif "COMMAND-LINE APP CONTRACT" not in note and \
                any(t["name"] == "handle-command" for t in tools):
            note = (note + " " + CLI_APP_CONTRACT).strip()
        plan = self._ask(self._user_prompt(note, full=True), "plan")
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
        want = (step.get("name") or "").lower()
        if want and any((x.get("name") or "").lower() == want for x in subs) and \
                any(t["name"].lower() == want for t in self.registry.load()):
            return True          # the split's last tool IS this step, and it passed
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
        keys = [k for k, _ in orc.lisp_hints(self._failure_text(verdict))]
        if self.lessons is not None:
            self.lessons.record(keys, signature=sig, detail=detail, project=self.project,
                                warned=self._warned - self._recurred)
            self._recurred.update(k for k in keys if k in self._warned)
            if verdict.get("class") in ("REPEATED_CANDIDATE", "REGRESSION",
                                        "SPEC_INCONSISTENT", "TEST_CALL_INVALID"):
                self.lessons.record_harness(verdict["class"])
        for k in keys:                      # this run's own slips outrank old statistics
            if k not in self._slips:
                self._slips.append(k)

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
                broken = self._regressions(plan)
                if not broken:
                    return self._promote(plan)
                verdict = {"ok": False, "stage": "regression", "got_map": {},
                           "class": "REGRESSION",
                           "reason": "the new %s breaks %d existing test(s)"
                                     % (plan["name"], len(broken)),
                           "detail": "Changing %s broke tools that call it; keep "
                                     "their behaviour or give the new behaviour a "
                                     "NEW tool name. %s"
                                     % (plan["name"], "; ".join(broken[:3]))}
                self.emit("verdict", **verdict)
            self._note_failure(verdict, attempt)
            # identical code AND identical tests after a value mismatch: the
            # model is stuck defending an expectation, so blame the test
            repeat_stuck = verdict.get("class") == "REPEATED_CANDIDATE" and \
                bool(prev_got)
            crashed = any(i.get("got") is None for i in verdict.get("infos") or [])
            test_wrong = (verdict.get("class") == "TEST_WRONG" or
                          bool(verdict.get("drift")) or repeat_stuck) and not crashed
            if test_wrong:
                self._last_stuck = True
            if not rescued and not crashed and (test_wrong or (
                    attempt >= MAX_REPAIRS and self._last_stuck)):
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
            kept = plan                 # the rehearsed plan; its definition is reused
            if verdict.get("class") == "TEST_CALL_INVALID":
                # the definition is right: only the test calls are not valid Lisp
                self.emit("test_call_repair", reason=(
                    verdict.get("detail") or verdict.get("reason") or "")[:160])
                frozen = ["%s = %s" % (t["call"], self._frozen[t["call"]])
                          for t in kept["tests"]
                          if isinstance(t, dict) and t.get("call") in self._frozen]
                body = ("A TEST CALL is not valid Lisp. The definition is correct "
                        "and is kept EXACTLY as it is. Fix ONLY the test calls, "
                        "and keep their expected values.\n"
                        "Rules: a data list passed as an argument must be quoted "
                        "with a single leading quote, e.g. '(1 \"alice\" \"pw\"), "
                        "never a bare (1 \"alice\") list; no quote marks nested "
                        "inside quoted data; every call is exactly ONE Lisp form; "
                        "only call functions that exist (the definition's function "
                        "or a saved tool in the REGISTRY).\n"
                        "The worker said: %s\n"
                        "%sCurrent definition (keep it exactly):\n%s\n"
                        "Return build JSON with the same name and definition and "
                        "corrected tests."
                        % (" ".join((verdict.get("error_text") or "n/a").split())[:400],
                           ("Verified expected values that must NOT change: %s\n"
                            % "; ".join(frozen)) if frozen else "",
                           kept["definition"]))
            elif verdict.get("class") == "COMPILER_ERROR":
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
            test_call = verdict.get("class") == "TEST_CALL_INVALID"
            try:
                plan = self._ask(self._user_prompt(head + body, goal=goal),
                                 "test-calls" if test_call else
                                 ("repair" if attempt == 0 else "rewrite"),
                                 system=TEST_SYSTEM if test_call else None,
                                 temperature=temp, effort="low",
                                 deep=temp is not None and attempt >= MAX_REPAIRS - 1)
            except BadReply as exc:
                # unusable reply: this attempt is lost, the step is not
                self.emit("bad_reply", reason=str(exc)[:200])
                continue
            if test_call and plan.get("action") == "build":
                # keep the definition the tests were run against; take only the tests
                plan = dict(kept, tests=plan.get("tests"),
                            call=plan.get("call") or kept.get("call"))
            self.emit("decision", action=plan.get("action"), plan=plan)
            if plan.get("action") != "build":
                self._last_failure = {"detail": "the model stopped building",
                                      "hint": "", "attempts": attempt + 1}
                if not quiet:
                    self.state = "failed"
                return False
        return False

    def _regressions(self, plan):
        """Failing tests of OTHER saved tools if PLAN replaces a tool they call.

        Empty when the name is new, the code is unchanged, or nothing depends
        on it. This is what makes a follow-up prompt safe: a refinement may
        change a tool only if everything built on it still passes.
        """
        tools = self.registry.load()
        old = next((t for t in tools if t["name"] == plan["name"]), None)
        if not old or orc.norm_definition(old["definition"]) == \
                orc.norm_definition(plan["definition"]):
            return []
        uses = re.compile(r"(?<![^\s('])%s(?![^\s)])" % re.escape(plan["name"]), re.I)
        prelude = "%s\n%s" % (self.registry.prelude(), plan["definition"])
        broken = []
        for dep in tools:
            if dep["name"] == plan["name"] or not uses.search(dep["definition"]):
                continue
            for t in dep.get("tests") or []:
                env = self._repl("%s\n%s\n(gg-check %s '%s)" % (
                    GG_CHECK, prelude, t["call"], t["expect"]), "regression")
                if not (env.get("ok") and
                        (env.get("return_value") or "").strip().upper() == "T"):
                    broken.append("%s no longer gives %s" % (t["call"], t["expect"]))
        return broken

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
            "eval_ms": (round(sorted(self._eval_ms)[len(self._eval_ms) // 2], 2)
                        if self._eval_ms else None),
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
        self._eval_ms = []
        self._prerun = self._run_side_by_side([d["code"] for d in direct])
        res = pipeline.run_candidate(
            candidate_text(plan), tests={"direct": direct},
            worker_fn=self._recording_worker(plan, prelude),
            risk_fn=self.risk_fn)
        self._prerun = {}
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
                # passed: a FACT (number, boolean, list) is verified and frozen; a
                # string literal is the model's own formatting, which may change
                if ok_idx and '"' not in plan["tests"][idx]["expect"]:
                    self._frozen.setdefault(plan["tests"][idx]["call"],
                                            plan["tests"][idx]["expect"])
                continue
            t = plan["tests"][idx] if ok_idx else {"call": "?", "expect": ""}
            rv = one_line(item.get("return_value") or "")
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
                "class": None if ok else orc.failure_class(
                    infos, drift, definition=plan["definition"],
                    calls=[t["call"] for t in plan["tests"]] + [
                        x["call"] for tool in self.registry.load()
                        for x in (tool.get("tests") or [])
                        if isinstance(x, dict) and x.get("call")])}

    def _run_side_by_side(self, codes):
        """``{code: result}`` for CODES, evaluated concurrently.

        Each evaluation is its own sandboxed SBCL process, so they are
        independent. Only the real worker is parallelised; injected test
        workers keep their strict one-at-a-time order.
        """
        codes = list(dict.fromkeys(codes))
        if self.worker_fn is not _worker_fn or len(codes) < 2:
            return {}
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(4, len(codes))) as pool:
            return dict(zip(codes, pool.map(self.worker_fn, codes)))

    def _recording_worker(self, plan, prelude):
        """Worker wrapper that logs each rehearsal eval as a REPL line."""
        def run(code):
            env = self._prerun.pop(code, None) or self.worker_fn(code)
            if env.get("candidate_ms") is not None or env.get("elapsed_ms") is not None:
                self._eval_ms.append(env.get("candidate_ms")
                                     if env.get("candidate_ms") is not None
                                     else env.get("elapsed_ms"))
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
            head = re.match(r"\s*\(\s*(\S+)", call)
            shown = ""
            if head:
                tool = next((t for t in self.registry.load()
                             if t["name"].lower() == head.group(1).lower()), None)
                if tool:
                    passed = [x["call"] for x in (tool.get("tests") or [])
                              if isinstance(x, dict) and x.get("call")][:3]
                    if passed:
                        shown = ("Calls of %s that PASSED their tests (imitate "
                                 "their argument shapes exactly): %s\n"
                                 % (tool["name"], " | ".join(c[:120] for c in passed)))
            plan = self._ask(self._user_prompt(
                "YOUR CALL FAILED: %s\nCall was: %s\n%sReturn JSON "
                '{"action":"use","call":"..."} with a corrected call '
                "(literal data from the goal, quoted lists)."
                % (reason[:200], call, shown)), "repair-call")
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
           "pair": None, "state": "running", "result": None, "repairs": 0,
           "model_ms": 0.0, "wall_s": None}
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
            row["project"] = ev.get("project") or projects.BUILTIN
        elif k == "model_call":
            row["model_calls"] += 1
        elif k == "model_reply":
            row["cost_usd"] += ev.get("cost_usd") or 0.0
            row["model_ms"] += ev.get("latency_ms") or 0.0
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
            if row.get("t") and ev.get("t"):
                row["wall_s"] = round(ev["t"] - row["t"], 2)   # prompt sent -> answer shown
    row["model_ms"] = round(row["model_ms"], 1)
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
        self.projects = projects.ProjectStore(self.registry.path.parent)
        self._registries = {}

    def busy(self):
        """True while a session is running."""
        return self._busy

    def registry_for(self, project=None):
        """The tool registry of PROJECT (the built-in one when unknown)."""
        pid = self.projects.resolve(project)
        if pid == projects.BUILTIN:
            return self.registry
        if pid not in self._registries:
            self._registries[pid] = ToolRegistry(self.projects.tools_path(pid))
        return self._registries[pid]

    def project_note(self, project=None):
        """One line telling the model which program it is extending, and how."""
        meta = self.projects.get(self.projects.resolve(project))
        if not meta or meta["builtin"]:
            return ""
        return ("PROJECT: %s%s. Its saved tools are in the REGISTRY below. To ADD a "
                "feature, build new tools that call the existing ones. To CHANGE a "
                "tool, build it again under the SAME name: it replaces the old "
                "version only if the tools that call it still pass their tests. "
                "Keep every argument shape used by the existing tools."
                % (meta["name"], " - " + meta["description"] if meta["description"] else ""))

    def start(self, prompt, mode="demo", compare=False, expected=None,
              oracle=None, project=None):
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
                           registry=self.registry_for(project), mode=mode,
                           expected=expected, oracle=oracle,
                           lessons=self.lessons,
                           postmortem_dir=AGENT_DIR / "postmortems")
            sess.project = self.projects.resolve(project)
            sess.project_note = self.project_note(project)
            sess.compare = "running" if compare else None
            self._sessions[sess.id] = sess
        threading.Thread(target=self._chain, args=(sess, compare),
                         daemon=True).start()
        return sess.id, None

    def _chain(self, sess, compare):
        try:
            sess.run()
            self.projects.touch(sess.project)
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
                    twin.project = sess.project
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

    def call_tool(self, text, mode=None, project=None):
        """Run one allow-listed call of a saved tool. Zero model tokens."""
        base = self.registry_for(project)
        reg = base.for_mode(mode) if mode else base
        names = {t["name"] for t in reg.load()}
        text = quote_literals(text) if isinstance(text, str) else text
        problem = safe_expr_check(text, names)
        if problem:
            out = {"ok": False, "error": problem, "tokens": 0}
            self._log_call(str(text)[:600], mode, project, dict(out, value="", error="refused: " + problem))
            return out
        prelude = reg.prelude()
        text = re.sub(r"#'([^\s()'#]+)", r"(function \1)", text)
        env = _worker_fn("%s\n%s" % (prelude, text))
        out = {"ok": bool(env.get("ok")), "value": env.get("return_value"),
               "error": (env.get("error") or "")[:300],
               "elapsed_ms": env.get("elapsed_ms"), "tokens": 0}
        self._log_call(text, mode, project, out)
        return out

    def _log_call(self, text, mode, project, out):
        """Append one typed-REPL call to ``calls.jsonl`` beside the registry."""
        try:
            path = self.registry.path.parent / "calls.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "t": round(time.time(), 3), "project": self.projects.resolve(project),
                    "mode": mode, "call": text, "ok": out["ok"],
                    "value": (out.get("value") or "")[:2000], "error": out.get("error"),
                    "elapsed_ms": out.get("elapsed_ms")}) + "\n")
        except OSError:
            pass

    def history(self, limit=40, project=None):
        """Per-session summaries from the JSONL logs, oldest first.

        With PROJECT given, only that project's sessions (newest LIMIT of them).
        """
        out = []
        pid = self.projects.resolve(project) if project else None
        epoch = self.registry_for(pid).epoch()
        files = sorted((AGENT_DIR / "sessions").glob("*.jsonl"),
                       key=lambda p: p.stat().st_mtime)
        for path in (files if pid else files[-limit:]):
            row = row_from_log(path)
            if row and row.get("t", 0) >= epoch and \
                    (pid is None or row.get("project", projects.BUILTIN) == pid):
                out.append(row)
        return out[-limit:]

    def tools(self, mode=None, project=None):
        base = self.registry_for(project)
        reg = base.for_mode(mode) if mode else base
        loaded = reg.load()
        meta = toolmeta.describe_all(loaded)
        return [{"name": t.get("name"), "description": t.get("description"),
                 "definition": t.get("definition"), "session": t.get("session"),
                 "created": t.get("created"), "uses": t.get("uses", 0),
                 "tests": t.get("tests", []),
                 "prompts": len(t.get("prompts", [])),
                 "mode": t.get("mode") or "demo",
                 "meta": meta.get(t.get("name"))}
                for t in loaded]

    def active(self):
        """The run in progress, if any: ``{session_id, prompt, project, mode}``."""
        with self._lock:
            running = [s for s in self._sessions.values()
                       if s.state == "running" and s.arm == "main"]
        if not (self._busy and running):
            return {"session_id": None}
        sess = running[-1]
        return {"session_id": sess.id, "prompt": sess.prompt,
                "project": sess.project, "mode": sess.mode}
