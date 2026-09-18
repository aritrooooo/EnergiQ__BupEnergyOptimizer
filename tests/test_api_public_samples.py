"""End-to-end API tests. Runs with MOCK_LLM=1 (see conftest) so no API credits are used.

Set MOCK_LLM=0 and ANTHROPIC_API_KEY=... to exercise the live Claude path instead.
"""
import copy
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
CASES = json.loads((Path(__file__).resolve().parents[1] / "public_sample_cases.json").read_text())
TOL = 0.01


def test_health():
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_sample_case(case):
    req = copy.deepcopy(case["request"])
    expected_types = req.pop("expected_directive_types", None)

    t0 = time.perf_counter()
    r = client.post("/optimize-energy", json=req)
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["scenario_id"] == req["scenario_id"]
    assert len(body["directive_interpretation"]) == len(req["operator_notes"])
    assert [d["note_index"] for d in body["directive_interpretation"]] == list(
        range(len(req["operator_notes"]))
    )
    for d in body["directive_interpretation"]:
        if d["directive_type"] == "no_op":
            assert d["applies"] is False and d["structured_adjustment"] is None
        else:
            assert d["applies"] is True and isinstance(d["structured_adjustment"], dict)
            hrs = d["structured_adjustment"]["hours"]
            assert hrs == sorted(set(hrs)) and all(0 <= h <= 23 for h in hrs)
    if expected_types:
        assert [d["directive_type"] for d in body["directive_interpretation"]] == expected_types

    plan = body["hourly_plan"]
    assert len(plan) == 24 and [p["hour"] for p in plan] == list(range(24))

    hours = {h["hour"]: h for h in req["hours"]}
    bat = req["battery"]
    energy = bat["initial_energy_kwh"]
    total_grid = total_cost = 0.0
    peak = 0.0
    for p in plan:
        chg = p["battery_kwh"] if p["battery_action"] == "charge" else 0.0
        dis = p["battery_kwh"] if p["battery_action"] == "discharge" else 0.0
        h = hours[p["hour"]]
        assert abs((p["grid_kwh"] + p["solar_used_kwh"] + dis) - (h["demand_kwh"] + chg)) < TOL
        assert p["solar_used_kwh"] <= h["solar_kwh"] + TOL
        assert chg <= bat["max_charge_kwh_per_hour"] + TOL
        assert dis <= bat["max_discharge_kwh_per_hour"] + TOL
        energy += chg - dis
        assert abs(energy - p["battery_energy_after_kwh"]) < TOL
        assert bat["minimum_energy_kwh"] - TOL <= energy <= bat["capacity_kwh"] + TOL
        total_grid += p["grid_kwh"]
        total_cost += p["grid_kwh"] * h["tariff_bdt_per_kwh"]
        peak = max(peak, p["grid_kwh"])

    assert abs(energy - bat["initial_energy_kwh"]) < TOL
    assert abs(total_grid - body["total_grid_kwh"]) < TOL
    assert abs(total_cost - body["total_cost_bdt"]) < TOL
    assert abs(peak - body["peak_grid_kwh"]) < TOL
    assert isinstance(body["plan_summary"], str) and body["plan_summary"]
    assert elapsed < 5.0, f"latency {elapsed:.2f}s exceeds the 5s p95 target"


def test_directive_actually_changes_the_plan():
    """The no_charge_window in GRID-101 must be visible in the schedule."""
    req = copy.deepcopy(CASES[0]["request"])
    req.pop("expected_directive_types", None)
    body = client.post("/optimize-energy", json=req).json()
    nc = next(d for d in body["directive_interpretation"] if d["directive_type"] == "no_charge_window")
    for h in nc["structured_adjustment"]["hours"]:
        assert body["hourly_plan"][h]["battery_action"] != "charge"


# ------------------------------------------------------------------ 400 paths


def _valid():
    req = copy.deepcopy(CASES[0]["request"])
    req.pop("expected_directive_types", None)
    return req


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["hours"].pop(),                       # only 23 hours
        lambda r: r["hours"].__setitem__(5, r["hours"][4]),  # duplicate hour
        lambda r: r.__setitem__("battery", {}),           # missing battery fields
        lambda r: r.__setitem__("operator_notes", []),    # no notes
        lambda r: r.__setitem__("operator_notes", ["a", "b", "c", "d"]),  # too many notes
        lambda r: r.__setitem__("operator_notes", [""]),  # empty note
        lambda r: r["hours"][0].__setitem__("demand_kwh", "lots"),  # wrong type
        lambda r: r.pop("scenario_id"),                   # missing field
    ],
)
def test_malformed_requests_return_clean_400(mutate):
    req = _valid()
    mutate(req)
    r = client.post("/optimize-energy", json=req)
    assert r.status_code == 400
    assert "Traceback" not in r.text and "File \"" not in r.text
    assert set(r.json()).issubset({"error", "detail"})


def test_malformed_json_returns_400():
    r = client.post(
        "/optimize-energy", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 400 and r.json()["error"] == "malformed_json"
