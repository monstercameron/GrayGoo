# GrayGoo Dashboard

Local control + visualization dashboard for the GrayGoo agent system.
Stdlib-only backend, vanilla JS frontend, no CDN, no pip dependencies.

## Start

Double-click `start.cmd` in the repo root, or run from the repo root:

```sh
uv run python start.py
```

It starts the dashboard on port 8150 (8150-8169 are tried if that port is taken
by something else), waits until it answers, re-mounts the project apps that were
mounted last time, and opens **http://127.0.0.1:8150/** in your browser. If a
dashboard is already running it only opens the browser. Ctrl+C stops it.

- `--port N` preferred port; `--no-browser` skip the browser;
  `--status` report the running dashboard, its projects and mounted apps, then exit.
- Mounted apps are remembered in `artifacts/agent/apps/mounts.json`.

## Run the server directly

From the repo root:

```sh
python dashboard/server.py
```

Then open **http://127.0.0.1:8137/** in a browser.

Custom port:

```sh
python dashboard/server.py --port 8137
```

## Agent tab (prompt the agent, watch it build tools)

Start with `uv run python dashboard/server.py` (plain `python` lacks the
Cerebras client dependencies, so Live mode would be unavailable).

- Ask in plain English. The model writes a Lisp tool and tests; the tests run
  in sandboxed SBCL; only passing tools are saved to `artifacts/agent/tools.json`.
- Every prompt is also run against an empty registry (the no-memory arm), so
  token savings and answer agreement are measured, not estimated.
- Proof panel: net savings, cost once a tool exists, held-out correctness (answers
  from independent Python), regression on known answers; per-prompt paired bars.
- Tabs switch between your session, the shipped recorded live runs
  (`dashboard/evidence/*.json`) and runs you pin.
- Modes: **Demo** is a scripted offline model (free, estimated tokens, example chips
  only). **Live** calls Cerebras Qwen (real tokens); a hard `$1.00` spend cap per
  server process applies (`LIVE_SPEND_CAP_USD` in `agent_session.py`).
- `python evidence_run.py --runs 3 --cap 0.14` repeats the live guided and held-out
  runs and rewrites `dashboard/evidence/live-repeat-runs.json` (paid, capped).

Agent API: `POST /api/agent/prompt`, `GET /api/agent/sessions/{id}?since=N`,
`GET /api/agent/tools|history|config|snapshots|heldout`, `POST /api/agent/call|pin|reset`.

## Views

- **Overview** — todo progress, Cerebras spend vs $50 budget, git log, system checks.
- **Pipeline** — `run_candidate` stage graph (parse → risk → worker → repair →
  patch → transfer) plus the last demo summary.
- **Control** — run smoke / tests / baseline-stub jobs, watch live output, stop.

## API

- `GET /api/status`, `GET /api/pipeline`
- `POST /api/run` with `{"job": "smoke"|"tests"|"baseline-stub"}` → `{job_id}`
  (409 while another job runs; one job at a time)
- `GET /api/jobs/{id}` → `{state, exit_code, tail, output}`
- `POST /api/jobs/{id}/stop`
- `GET /api/events` (optional SSE; the UI polls `/api/jobs/{id}` instead)

Job logs go to `artifacts/dashboard/` (gitignored).

## Security notes

- Binds `127.0.0.1` (loopback) only; no auth — single-user local use.
- Secrets are never served: `CEREBRAS_API_KEY` values are redacted in every
  response and in every byte written to job logs; the key is reported as
  present/absent only.
