# Self-Evolving Lisp Runtime

A persistent Common Lisp system that turns expensive model reasoning into
tested, reusable executable capabilities — so future tasks in the same domain
need progressively less inference.

> How much model reasoning can the system permanently replace with safe,
> reusable computation?

## Pipeline

```mermaid
flowchart TD
    Goal["Goal intake"] --> Norm["Intent normalizer"]
    Norm --> Search["Capability search"]
    Search -->|"capability exists"| Det["Deterministic path<br/>execute known capability, no model call"]
    Det --> Live["Live runtime"]
    Search -->|"capability missing"| Ctx["Context compiler<br/>minimal task context"]
    Ctx --> LLM["Cerebras / Qwen<br/>candidate Lisp AST"]
    LLM --> Risk["Mutation classifier<br/>risk R0-R6"]
    Risk --> Re["Rehearsal orchestrator<br/>pinned generation fingerprint"]
    Re --> W1["Worker: regression"]
    Re --> W2["Worker: fuzzing + adversarial"]
    Re --> W3["Worker: differential old vs new"]
    W1 --> Eval["Trusted evaluator<br/>hidden tests"]
    W2 --> Eval
    W3 --> Eval
    Eval -->|"fail"| Rep{"Repair budget left?"}
    Rep -->|"yes: one repair"| LLM
    Rep -->|"no"| Esc["Escalate failure"]
    Eval -->|"pass"| Reg["Registry<br/>ephemeral / patch"]
    Reg --> TG["Transfer gate<br/>reuse on related tasks"]
    TG --> Prom["Promotion authority<br/>new immutable version"]
    Prom --> Disp["Versioned dispatch<br/>publish new epoch"]
    Disp --> Live
```

Green-stop: the harness, not the model, declares success. No speculative
candidate touches canonical state; promotion requires independent evidence.

## Repo map

| Path | Contents |
|---|---|
| `plan.md` | Full specification (source of truth) |
| `memory.md` | Advisory learning-layer design |
| `todos.md` | Execution-ordered task list |
| `documents/` | Planning/research digests derived from the specs |
| `docs/` | GitHub Pages site (thesis + benchmarks), served from `/docs` |
| `cerebras_client.py` | Cerebras inference client (Python + OpenAI SDK) |

## Quickstart

```powershell
uv run python cerebras_client.py "Reply with exactly: OK"
```

Reads `CEREBRAS_API_KEY` (or `CEREBRAS`) from `.env`. See
`documents/roadmap.md` for build phases and current status.
