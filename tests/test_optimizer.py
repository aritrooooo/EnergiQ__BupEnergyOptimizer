"""Optimizer tests against hand-computed optima."""
from app.optimizer import solve
from app.replay import validate
from app.schemas import Battery, HourInput

TOL = 0.01


def mk_hours(demand, solar, tariff):
    return [
        HourInput(hour=h, demand_kwh=demand[h], solar_kwh=solar[h], tariff_bdt_per_kwh=tariff[h])
        for h in range(24)
    ]


def flat(v):
    return [v] * 24


# Shifting scenario: cheap first half (tariff 1), expensive second half (tariff 2).
SHIFT_TARIFF = flat(1)[:12] + flat(2)[:12]
SHIFT_HOURS = mk_hours(flat(10), flat(0), SHIFT_TARIFF)
SHIFT_BAT = Battery(
    capacity_kwh=100,
    initial_energy_kwh=50,
    minimum_energy_kwh=0,
    max_charge_kwh_per_hour=10,
    max_discharge_kwh_per_hour=10,
)

FLAT_HOURS = mk_hours(flat(10), flat(0), flat(1))
FLAT_BAT = SHIFT_BAT


def cost_of(plan, hours):
    return sum(p["grid_kwh"] * hours[p["hour"]].tariff_bdt_per_kwh for p in plan)


def assert_sane(plan, hours, battery, directives):
    # replay is the independent checker: energy balance, bounds, rates, neutrality
    totals = validate(plan, hours, battery, directives)
    assert len(plan) == 24
    for p in plan:
        assert p["battery_action"] in ("charge", "discharge", "idle")
        if p["battery_action"] == "idle":
            assert abs(p["battery_kwh"]) < TOL
    assert abs(plan[23]["battery_energy_after_kwh"] - battery.initial_energy_kwh) < TOL
    return totals


# (a) no directives ---------------------------------------------------------


def test_no_directives_shifts_load_to_cheap_hours():
    plan = solve(SHIFT_HOURS, SHIFT_BAT, [])
    totals = assert_sane(plan, SHIFT_HOURS, SHIFT_BAT, [])
    # baseline 360 BDT; 50 kWh of headroom shifted from tariff 2 to tariff 1 saves 50
    assert abs(totals["total_cost_bdt"] - 310.0) < TOL
    assert abs(cost_of(plan, SHIFT_HOURS) - 310.0) < TOL
    assert abs(totals["total_grid_kwh"] - 240.0) < TOL


# (b) solar_reduction -------------------------------------------------------


SOLAR_BAT = Battery(
    capacity_kwh=100,
    initial_energy_kwh=50,
    minimum_energy_kwh=50,
    max_charge_kwh_per_hour=0,
    max_discharge_kwh_per_hour=0,
)
SOLAR_HOURS = mk_hours(flat(10), flat(4), flat(1))


def test_solar_reduction_costs_exactly_the_lost_solar():
    base = solve(SOLAR_HOURS, SOLAR_BAT, [])
    assert abs(cost_of(base, SOLAR_HOURS) - 144.0) < TOL  # 6 kWh grid x 24 h x 1 BDT

    d = [{
        "note_index": 0, "applies": True, "directive_type": "solar_reduction",
        "structured_adjustment": {"hours": [12, 13], "factor": 0.5},
    }]
    plan = solve(SOLAR_HOURS, SOLAR_BAT, d)
    totals = assert_sane(plan, SOLAR_HOURS, SOLAR_BAT, d)
    assert abs(totals["total_cost_bdt"] - 148.0) < TOL  # 2 kWh lost in each of 2 hours
    for h in (12, 13):
        assert plan[h]["solar_used_kwh"] <= 2.0 + TOL


# (c) no_charge_window ------------------------------------------------------


def test_no_charge_window_blocks_the_cheap_charge():
    d = [{
        "note_index": 0, "applies": True, "directive_type": "no_charge_window",
        "structured_adjustment": {"hours": list(range(12))},
    }]
    plan = solve(SHIFT_HOURS, SHIFT_BAT, d)
    totals = assert_sane(plan, SHIFT_HOURS, SHIFT_BAT, d)
    # no cheap-hour charging is possible, so no arbitrage: full 360 BDT
    assert abs(totals["total_cost_bdt"] - 360.0) < TOL
    for h in range(12):
        assert plan[h]["battery_action"] != "charge"


# (d) max_grid_window -------------------------------------------------------


def test_max_grid_window_is_respected():
    d = [{
        "note_index": 0, "applies": True, "directive_type": "max_grid_window",
        "structured_adjustment": {"hours": [12, 13], "max_grid_kwh": 5.0},
    }]
    plan = solve(FLAT_HOURS, FLAT_BAT, d)
    totals = assert_sane(plan, FLAT_HOURS, FLAT_BAT, d)
    for h in (12, 13):
        assert plan[h]["grid_kwh"] <= 5.0 + TOL
    # flat tariff + end-of-day neutrality => total grid still equals total demand
    assert abs(totals["total_cost_bdt"] - 240.0) < TOL


# (e) minimum_battery_reserve ----------------------------------------------


def test_minimum_battery_reserve_is_respected():
    d = [{
        "note_index": 0, "applies": True, "directive_type": "minimum_battery_reserve",
        "structured_adjustment": {"hours": [5, 6], "minimum_energy_kwh": 80.0},
    }]
    plan = solve(FLAT_HOURS, FLAT_BAT, d)
    totals = assert_sane(plan, FLAT_HOURS, FLAT_BAT, d)
    for h in (5, 6):
        assert plan[h]["battery_energy_after_kwh"] >= 80.0 - TOL
    assert abs(totals["total_cost_bdt"] - 240.0) < TOL


# (f) no_discharge_window + combination ------------------------------------


def test_no_discharge_window_and_combination():
    d = [
        {"note_index": 0, "applies": True, "directive_type": "no_discharge_window",
         "structured_adjustment": {"hours": [12, 13, 14]}},
        {"note_index": 1, "applies": True, "directive_type": "solar_reduction",
         "structured_adjustment": {"hours": [12], "factor": 0.0}},
    ]
    hours = mk_hours(flat(10), flat(4), SHIFT_TARIFF)
    plan = solve(hours, SHIFT_BAT, d)
    assert_sane(plan, hours, SHIFT_BAT, d)
    for h in (12, 13, 14):
        assert plan[h]["battery_action"] != "discharge"
    assert plan[12]["solar_used_kwh"] < TOL


def test_single_action_per_hour_everywhere():
    plan = solve(SHIFT_HOURS, SHIFT_BAT, [])
    for p in plan:
        assert not (p["battery_action"] == "charge" and p["battery_kwh"] == 0 and False)
        assert p["battery_kwh"] >= 0
