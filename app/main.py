"""FastAPI wiring: LLM -> guardrails -> MILP optimizer -> replay -> response."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import guardrails, llm_interpreter, optimizer, replay
from .schemas import OptimizeRequest, OptimizeResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("gridwise.api")

app = FastAPI(title="GridWise LLM-Assisted Energy Optimizer", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

TYPE_LABEL = {
    "solar_reduction": "reduced solar",
    "minimum_battery_reserve": "a raised battery reserve",
    "no_charge_window": "a no-charge window",
    "no_discharge_window": "a no-discharge window",
    "max_grid_window": "a grid import cap",
}


def _summary(plan, hours, totals, directives) -> str:
    hours = sorted(hours, key=lambda h: h.hour)
    peak_hour = max(plan, key=lambda p: p["grid_kwh"])["hour"]
    charge_hours = [p["hour"] for p in plan if p["battery_action"] == "charge"]
    discharge_hours = [p["hour"] for p in plan if p["battery_action"] == "discharge"]
    active = [TYPE_LABEL[d["directive_type"]] for d in directives if d["applies"]]
    parts = [
        f"Total grid import {totals['total_grid_kwh']:.1f} kWh costing "
        f"{totals['total_cost_bdt']:.2f} BDT, peaking at {totals['peak_grid_kwh']:.1f} kWh in hour {peak_hour}.",
        f"Battery charges in hour(s) {charge_hours or 'none'} and discharges in hour(s) {discharge_hours or 'none'}, "
        f"returning to its starting level by hour 23.",
    ]
    parts.append(
        "Applied operator directives: " + ", ".join(active) + "."
        if active
        else "No operator note changed the schedule."
    )
    return " ".join(parts)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/optimize-energy")
async def optimize_energy(request: Request):
    # 1. parse + validate
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": "malformed_json"})
    try:
        req = OptimizeRequest.model_validate(body)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first.get("loc", ())) or "body"
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_request", "detail": f"{loc}: {first.get('msg', 'invalid')}"[:200]},
        )

    try:
        hours = req.hours_sorted()
        # 2. LLM interpretation (untrusted)
        raw = llm_interpreter.interpret(req.operator_notes, hours, req.battery)
        # 3-4. deterministic guardrails -> exactly one entry per note, in order
        interpretations = guardrails.approve_all(raw, len(req.operator_notes), req.battery)
        approved = [d for d in interpretations if d["applies"]]
        # 5. MILP
        plan = optimizer.solve(hours, req.battery, approved)
        # 6. independent replay + recomputed totals
        totals = replay.validate(plan, hours, req.battery, approved)
    except optimizer.InfeasibleError:
        log.error("optimizer infeasible for scenario_id=%s", req.scenario_id)
        return JSONResponse(status_code=500, content={"error": "internal_error"})
    except replay.ReplayError as exc:
        log.error("replay failed for scenario_id=%s: %s", req.scenario_id, exc)
        return JSONResponse(status_code=500, content={"error": "internal_error"})
    except Exception:  # noqa: BLE001
        log.exception("unexpected failure for scenario_id=%s", req.scenario_id)
        return JSONResponse(status_code=500, content={"error": "internal_error"})

    # 7. response
    resp = OptimizeResponse(
        scenario_id=req.scenario_id,
        directive_interpretation=interpretations,
        hourly_plan=plan,
        total_grid_kwh=totals["total_grid_kwh"],
        total_cost_bdt=totals["total_cost_bdt"],
        peak_grid_kwh=totals["peak_grid_kwh"],
        plan_summary=_summary(plan, hours, totals, interpretations),
    )
    return JSONResponse(status_code=200, content=resp.model_dump())


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"error": "internal_error"})
