"""Minimal model-facing context compiler (plan.md section 14).

:func:`compile_context` builds the smallest task-relevant context for one
model call — goal, relevant capabilities, contracts, current failure,
callers, allowed effects, constraints, task — under a hard token budget.
It NEVER includes full history: at most the single current/most-recent
failure is rendered.

Token accounting uses :func:`estimate_tokens`, a deliberately cheap
word-based estimator (whitespace-separated words). This is documented as
an approximation: real model tokenizers (BPE/WordPiece) split words into
sub-word pieces, so word count underestimates true token usage by a
rough, language-dependent factor. The compiler treats the budget as a
hard cap on *estimated* tokens and drops lowest-priority elements first,
so callers SHOULD set the budget conservatively (e.g. words ~= tokens/2
for English prose) when a downstream limit must hold exactly.

Section priority, most to least important (dropped from the tail):

1. ``GOAL`` — never dropped; the call is meaningless without it.
2. ``TASK`` — never dropped; the required output contract.
3. ``CURRENT FAILURE`` — the single most recent failure only.
4. ``CONTRACT`` — output/validity contracts the candidate must satisfy.
5. ``RELEVANT CAPABILITY`` — trimmed to fewer entries before being
   dropped outright.
6. ``ALLOWED EFFECTS``.
7. ``CONSTRAINT``.
8. ``CALLERS`` — dropped first.
"""

from __future__ import annotations

# (section name, priority rank); lower rank drops first. GOAL/TASK are
# pinned (rank None) and survive until the final truncation resort.
_DROP_ORDER = ("CALLERS", "CONSTRAINT", "ALLOWED EFFECTS",
               "RELEVANT CAPABILITY", "CONTRACT", "CURRENT FAILURE")
_PINNED = ("GOAL", "TASK")

# Render order follows the plan.md section 14 example.
_RENDER_ORDER = ("GOAL", "RELEVANT CAPABILITY", "CONTRACT", "CURRENT FAILURE",
                 "CALLERS", "ALLOWED EFFECTS", "CONSTRAINT", "TASK")

DEFAULT_MAX_TOKENS = 2000


def estimate_tokens(text: str) -> int:
    """Cheap word-based token estimate: ``len(text.split())``.

    Documented approximation — see the module docstring. Empty/blank
    text estimates to 0.
    """
    if not text or not text.strip():
        return 0
    return len(text.split())


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _capability_lines(cap) -> str:
    """Render one retrieved capability in a single compact line.

    Accepts Capability-like objects (``id``/``intent``/``version``...),
    ``(capability, score)`` tuples from retrieval, or plain dicts.
    """
    score = None
    if isinstance(cap, (list, tuple)) and len(cap) == 2:
        cap, score = cap
    if isinstance(cap, dict):
        cid = cap.get("id", "?")
        intent = cap.get("intent", "")
        version = cap.get("version", "?")
        in_t = list(cap.get("input_types") or [])
        out_t = list(cap.get("output_types") or [])
        effects = list(cap.get("effects") or [])
    else:
        cid = getattr(cap, "id", "?")
        intent = getattr(cap, "intent", "") or ""
        version = getattr(cap, "version", "?")
        in_t = list(getattr(cap, "input_types", None) or [])
        out_t = list(getattr(cap, "output_types", None) or [])
        effects = list(getattr(cap, "effects", None) or [])
    line = "%s@%s: %s" % (cid, version, intent.strip())
    sig = []
    if in_t or out_t:
        sig.append("%s -> %s" % (",".join(in_t) or "?",
                                 ",".join(out_t) or "?"))
    if effects:
        sig.append("effects=%s" % ",".join(effects))
    if score is not None:
        try:
            sig.append("score=%.3f" % float(score))
        except (TypeError, ValueError):
            pass
    if sig:
        line += " [%s]" % "; ".join(sig)
    return line


def _failure_text(failure) -> str:
    if failure is None:
        return ""
    if isinstance(failure, str):
        return failure.strip()
    if isinstance(failure, dict):
        for key in ("summary", "message", "text", "error"):
            if failure.get(key):
                return str(failure[key]).strip()
        return str(failure).strip()
    return str(failure).strip()


def _normalize_goal(goal) -> dict:
    if goal is None:
        return {}
    if isinstance(goal, str):
        return {"text": goal}
    if isinstance(goal, dict):
        return dict(goal)
    raise TypeError("goal must be a dict, str, or None")


def _normalize_budgets(budgets) -> dict:
    if budgets is None:
        return {}
    if isinstance(budgets, dict):
        return dict(budgets)
    raise TypeError("budgets must be a dict or None")


def _build_sections(goal, retrieved, failures, effects):
    """Return {section name: body text} for non-empty sections."""
    sections = {}

    goal_text = goal.get("text", goal.get("goal", ""))
    goal_text = str(goal_text).strip() if goal_text is not None else ""
    if goal_text:
        sections["GOAL"] = goal_text

    caps = _as_list(retrieved)
    if caps:
        sections["RELEVANT CAPABILITY"] = "\n".join(
            _capability_lines(c) for c in caps)

    contracts = _as_list(goal.get("contracts"))
    contracts = [str(c).strip() for c in contracts if str(c).strip()]
    if contracts:
        sections["CONTRACT"] = "\n".join(contracts)

    # Never full history: only the single current (last) failure.
    failures = _as_list(failures)
    if failures:
        current = _failure_text(failures[-1])
        if current:
            sections["CURRENT FAILURE"] = current

    callers = _as_list(goal.get("callers"))
    callers = [str(c).strip() for c in callers if str(c).strip()]
    if callers:
        sections["CALLERS"] = "\n".join(callers)

    effects = _as_list(effects)
    effects = [str(e).strip() for e in effects if str(e).strip()]
    if effects:
        sections["ALLOWED EFFECTS"] = "\n".join(effects)

    constraints = _as_list(goal.get("constraints"))
    constraints = [str(c).strip() for c in constraints if str(c).strip()]
    if constraints:
        sections["CONSTRAINT"] = "\n".join(constraints)

    task = goal.get("task", "")
    task = str(task).strip() if task is not None else ""
    if task:
        sections["TASK"] = task
    return sections


def _render(sections) -> str:
    blocks = []
    for name in _RENDER_ORDER:
        if name in sections:
            blocks.append("%s\n%s" % (name, sections[name]))
    return "\n\n".join(blocks)


def _truncate_to_fit(text: str, max_tokens: int) -> str:
    words = text.split()
    if len(words) <= max_tokens:
        return text
    return " ".join(words[:max_tokens])


def compile_context(goal, retrieved, failures, effects, budgets=None):
    """Compile the minimal model-facing context for one synthesis call.

    Args:
        goal: intent string or dict with ``text``/``goal``, plus optional
            ``task``, ``contracts``, ``callers``, ``constraints`` lists.
        retrieved: capabilities from retrieval (objects, ``(cap, score)``
            tuples, or dicts).
        failures: recent failures; ONLY the last (current) one is ever
            rendered — full history is never included.
        effects: allowed effects (strings).
        budgets: optional dict; ``max_tokens`` is the hard cap on
            estimated tokens (default 2000), ``max_capabilities`` caps
            the capability lines kept even when budget allows more.

    Returns a dict ``{"text", "tokens", "budget", "dropped", "sections"}``:
    the rendered context, its estimated token count (always
    ``<= max_tokens``), the budget applied, the section names dropped to
    fit (lowest priority first), and the section names retained.
    """
    goal = _normalize_goal(goal)
    budgets = _normalize_budgets(budgets)
    max_tokens = budgets.get("max_tokens", DEFAULT_MAX_TOKENS)
    try:
        max_tokens = int(max_tokens)
    except (TypeError, ValueError):
        raise ValueError("budgets['max_tokens'] must be an integer")
    if max_tokens <= 0:
        raise ValueError("budgets['max_tokens'] must be positive")
    max_caps = budgets.get("max_capabilities")
    if max_caps is not None:
        max_caps = int(max_caps)
        if max_caps <= 0:
            raise ValueError("budgets['max_capabilities'] must be positive")

    sections = _build_sections(goal, _as_list(retrieved),
                               _as_list(failures), _as_list(effects))
    if max_caps is not None and "RELEVANT CAPABILITY" in sections:
        lines = sections["RELEVANT CAPABILITY"].split("\n")[:max_caps]
        if lines:
            sections["RELEVANT CAPABILITY"] = "\n".join(lines)
        else:
            del sections["RELEVANT CAPABILITY"]

    dropped = []

    def current_cost():
        return estimate_tokens(_render(sections))

    # 1. Trim capability lines one at a time before dropping whole sections.
    while (current_cost() > max_tokens
           and "RELEVANT CAPABILITY" in sections):
        lines = sections["RELEVANT CAPABILITY"].split("\n")
        if len(lines) > 1:
            sections["RELEVANT CAPABILITY"] = "\n".join(lines[:-1])
        else:
            break

    # 2. Drop whole sections, lowest priority first.
    for name in _DROP_ORDER:
        if current_cost() <= max_tokens:
            break
        if name in sections:
            del sections[name]
            dropped.append(name)

    # 3. Last resort: hard cap must hold, so truncate the lowest-priority
    # surviving droppable section's body word-wise (GOAL/TASK shrink only
    # if they alone exceed the budget).
    if current_cost() > max_tokens:
        order = [n for n in _DROP_ORDER if n in sections]
        order += [n for n in _PINNED if n in sections]
        for name in order:
            if current_cost() <= max_tokens:
                break
            others = {k: v for k, v in sections.items() if k != name}
            others_cost = estimate_tokens(_render(others))
            # Reserve 1 token for the section header itself.
            allowance = max(max_tokens - others_cost - 1, 0)
            body = sections[name]
            words = body.split()
            if len(words) > allowance:
                sections[name] = " ".join(words[:allowance])
                if name not in dropped:
                    dropped.append(name + " (truncated)")

    text = _render(sections)
    tokens = estimate_tokens(text)
    # Defensive: the hard cap holds by construction, but never lie about it.
    if tokens > max_tokens:
        text = _truncate_to_fit(text, max_tokens)
        tokens = estimate_tokens(text)
    return {
        "text": text,
        "tokens": tokens,
        "budget": max_tokens,
        "dropped": dropped,
        "sections": [n for n in _RENDER_ORDER if n in sections],
    }
