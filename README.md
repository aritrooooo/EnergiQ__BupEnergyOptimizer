# GridWise — LLM-Assisted Energy Optimizer

## 1. What this is

GridWise is a single HTTP service that turns a 24-hour energy scenario plus free-text operator
notes into a valid, minimum-cost grid/solar/battery schedule. The pipeline is strictly
one-directional: **LLM → guardrails → MILP optimizer → final replay validator → response.**
Google Gemini reads each operator note and emits a structured directive (JSON mode, so the output
is machine-readable JSON, never prose). That output is treated as untrusted and passes through
deterministic guardrails that accept only the six supported directive shapes and degrade anything
else to `no_op`. Only approved directives become constraints in a PuLP/CBC mixed-integer program
that minimizes grid cost subject to the base energy rules. The solved schedule is then replayed
hour-by-hour from the raw numbers about to be returned, and `total_grid_kwh`, `total_cost_bdt` and
`peak_grid_kwh` are recomputed during that replay rather than taken from the solver.

## 2. Model and provider

- Provider: **Google Gemini API (Google AI Studio)**, official `google-genai` Python SDK.
- Model: **`gemini-3.8-flash`** (the default value of `LLM_MODEL`; override the env var to use a
  different model id without changing code).
- Called once per request with `temperature=0`, a system instruction that lists the six allowed
  directive types, and `response_mime_type="application/json"` (JSON mode). The reply is parsed
  with `json.loads`; there is no regex scraping of prose. Because JSON mode does not enforce a
  schema, the deterministic guardrails (Section 11) are what guarantee the shape and ranges of
  every directive. Flash was chosen because note interpretation is a short, well-specified
  classification task where latency and free-tier availability matter more than raw reasoning depth.
- One retry on failure or malformed output; on a second failure the affected notes degrade to
  `no_op` and the request still returns a valid 200 schedule.
- If `GEMINI_API_KEY` is unset, the service falls back to the deterministic mock interpreter and
  logs a warning.

## 3. Environment variables (names only — never commit values)

| Name | Required | Default | Purpose |
|---|---|---|---|
| `GEMINI_API_KEY` | yes (for live mode) | — | Google AI Studio API key. Read from env only, never logged. |
| `LLM_MODEL` | no | `gemini-3.8-flash` | Model id used for interpretation. |
| `PORT` | no | `8000` | Port uvicorn binds on `0.0.0.0`. |
| `REQUEST_TIMEOUT_SECONDS` | no | `12` | Per-attempt LLM client timeout. Two attempts stay under the 30s hard limit. |
| `MOCK_LLM` | no | unset | `1` = use the deterministic canned interpreter (tests/CI, no API quota). |

See `.env.example`.

## 4. Install

```bash
python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
```

On Windows, activate with `.venv\Scripts\activate`.

## 5. Run

```bash
export GEMINI_API_KEY=...             # your key; never commit it
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Run without using any API quota (deterministic interpreter):

```bash
MOCK_LLM=1 uvicorn app.main:app --host 0.0.0.0 --port 8000
```

On Windows PowerShell: `$env:MOCK_LLM="1"; uvicorn app.main:app --port 8000`.

## 6. `/health`

```bash
curl localhost:8000/health
```

```json
{"status": "ok"}
```

## 7. `/optimize-energy`

```bash
curl -s -X POST localhost:8000/optimize-energy \
  -H 'Content-Type: application/json' \
  -d @- <<'JSON'
{
  "scenario_id": "GRID-101",
  "operator_notes": [
    "Solar output will drop to about 20% from 1 PM to 3 PM.",
    "Do not charge the battery between 2 PM and 4 PM.",
    "The cafeteria menu changes tomorrow."
  ],
  "hours": [
    {"hour": 0, "demand_kwh": 180, "solar_kwh": 0, "tariff_bdt_per_kwh": 7}
  ],
  "battery": {
    "capacity_kwh": 500, "initial_energy_kwh": 200, "minimum_energy_kwh": 50,
    "max_charge_kwh_per_hour": 100, "max_discharge_kwh_per_hour": 100
  }
}
JSON
```

(The `hours` array above is truncated for readability. The full 24-hour body for case `GRID-101` is
in `BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json`. `hours` must contain exactly 24 unique
entries for hours 0–23 or the request is rejected with `400`.)

Response shape:

```json
{
  "scenario_id": "GRID-101",
  "directive_interpretation": [
    {"note_index": 0, "applies": true, "directive_type": "solar_reduction",
     "structured_adjustment": {"hours": [13, 14], "factor": 0.2},
     "explanation": "Usable solar is limited to 20% of forecast during these hours."},
    {"note_index": 1, "applies": true, "directive_type": "no_charge_window",
     "structured_adjustment": {"hours": [14, 15]},
     "explanation": "Battery charging is forbidden during this window."},
    {"note_index": 2, "applies": false, "directive_type": "no_op",
     "structured_adjustment": null,
     "explanation": "This note does not affect today's energy schedule."}
  ],
  "hourly_plan": [
    {"hour": 0, "grid_kwh": 180.0, "solar_used_kwh": 0.0,
     "battery_action": "idle", "battery_kwh": 0.0, "battery_energy_after_kwh": 200.0}
  ],
  "total_grid_kwh": 4432.0,
  "total_cost_bdt": 47441.0,
  "peak_grid_kwh": 295.0,
  "plan_summary": "Total grid import 4432.0 kWh costing 47441.00 BDT, peaking at 295.0 kWh in hour 23. ..."
}
```

`hourly_plan` always has 24 entries; `directive_interpretation` always has exactly one entry per
operator note, in `note_index` order.

Error codes: `400` malformed JSON or structurally invalid request (safe message, no stack trace);
`500` controlled internal error with body `{"error": "internal_error"}` only.

## 8. Docker

```bash
docker build -t gridwise:latest .
docker run --rm -p 8000:8000 -e GEMINI_API_KEY=$GEMINI_API_KEY gridwise:latest
curl localhost:8000/health
```

No secrets are baked into the image — only variable names via `ENV`. The container binds
`0.0.0.0:$PORT` (default 8000). Registry reference to submit: `<registry>/<user>/gridwise:latest`
(fill in after `docker push`).

## 9. Dependencies

| Package | Used for |
|---|---|
| `fastapi` | HTTP routing, request/response handling, error handlers |
| `uvicorn[standard]` | ASGI server |
| `pydantic` (v2) | Request/response schema validation → clean `400`s |
| `google-genai` | Official Google Gen AI SDK; the single JSON-mode interpretation call |
| `pulp` | MILP modelling, solved with the bundled CBC solver (no external binary) |
| `httpx`, `pytest` | TestClient transport and test runner |

## 10. Known limitations

- Single LLM call per request with one retry; on a second failure notes degrade to `no_op` rather
  than failing the request. A note that the model misreads is therefore silently dropped instead of
  surfacing an error.
- Gemini JSON mode guarantees valid JSON but not the exact schema. Malformed or out-of-range
  directives are caught by the guardrails and degraded to `no_op`.
- The Google AI Studio free tier has rate limits. Under heavy load, calls may fail and notes will
  degrade to `no_op` (the request still returns a valid schedule).
- Guardrails are conservative by design: an almost-correct directive (e.g. `factor` = 1.2) is
  discarded rather than clamped, because inventing a corrected value would be inventing data.
- CBC solve time is fast for this 24-hour, ~150-variable problem, but MILP solve time is not
  formally bounded; pathological inputs could in principle be slow.
- The natural-language time parser in `MOCK_LLM=1` mode is regex-based and only covers common
  phrasings — it exists for quota-free testing, not as a replacement for the LLM.
- No battery round-trip efficiency, degradation cost, or grid export/feed-in — the spec defines a
  lossless battery and forbids export, and the model matches the spec exactly.
- Conflicting directives (e.g. a reserve floor incompatible with end-of-day neutrality) make the
  MILP infeasible, which returns a controlled `500` rather than a partially-satisfied schedule.

## 11. What the LLM does and does not do

**The LLM does exactly one thing:** read the operator notes and emit structured directives. That
call is `app/llm_interpreter.py::interpret` → `_call_gemini`, and its output is what produces
`directive_interpretation`. Nothing else in the system talks to a model.

**Everything else is deterministic:**
- `app/guardrails.py` — validates the untrusted LLM output (allowed types only, one entry per note,
  hour format, `0 ≤ factor ≤ 1`, finite non-negative reserve/grid caps, `applies` semantics, no
  invented fields). Anything failing any check becomes `no_op`, logged server-side only.
- `app/optimizer.py` — a MILP in PuLP/CBC. Binary `is_charge`/`is_discharge`/`is_idle` per hour sum
  to 1, which is what enforces a single battery action per hour (a pure LP could pick simultaneous
  charge and discharge). Minimizes `sum(grid[h] * tariff[h])` subject to hourly energy balance,
  solar cap, charge/discharge rate limits, battery bounds, approved directive constraints, and
  `energy[23] == initial_energy_kwh`.
- `app/replay.py` — independently re-walks the finished plan and recomputes the three totals that
  are returned. If replay fails, the service returns `500` rather than a schedule it cannot verify.
- `plan_summary` is built from a deterministic template over the solved plan, not by the LLM.

## 12. Testing

Quick smoke test with no API key:

```bash
MOCK_LLM=1 uvicorn app.main:app --port 8000
curl localhost:8000/health
```

Then POST each case from `BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json` to `/optimize-energy`
and check that:

- the response has 24 `hourly_plan` entries and one `directive_interpretation` entry per note;
- `battery_energy_after_kwh` at hour 23 equals `initial_energy_kwh`;
- malformed requests (bad JSON, missing fields, fewer than 24 hours) return `400`.

Then repeat with `GEMINI_API_KEY` set (and `MOCK_LLM` unset) to exercise the live Gemini path.

Automated `pytest` tests are not included yet. The replay validator in `app/replay.py` already
re-checks every returned schedule at request time (energy balance, bounds, directive constraints,
end-of-day neutrality), so an invalid plan cannot be returned as a `200`.