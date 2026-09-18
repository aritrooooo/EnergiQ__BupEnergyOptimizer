"""Independent hour-by-hour replay of the final plan (Section 2.6).

Recomputes everything from the raw numbers about to be returned; never trusts the
optimizer's internal solve state.
"""
from __future__ import annotations

from typing import Any

from .config import TOL
from .optimizer import effective_min_energy, effective_solar, grid_caps
from .schemas import Battery, HourInput


class ReplayError(RuntimeError):
    """Raised when the final plan fails independent validation."""


def validate(
    plan: list[dict[str, Any]],
    hours: list[HourInput],
    battery: Battery,
    directives: list[dict[str, Any]],
) -> dict[str, float]:
    hours = sorted(hours, key=lambda h: h.hour)
    if len(plan) != 24 or sorted(p["hour"] for p in plan) != list(range(24)):
        raise ReplayError("plan must contain exactly hours 0..23, no duplicates")

    plan = sorted(plan, key=lambda p: p["hour"])
    solar = effective_solar(hours, directives)
    floors = effective_min_energy(battery, directives)
    caps = grid_caps(directives)
    no_charge = {
        h
        for d in directives
        if d.get("directive_type") == "no_charge_window" and d.get("applies")
        for h in d["structured_adjustment"]["hours"]
    }
    no_discharge = {
        h
        for d in directives
        if d.get("directive_type") == "no_discharge_window" and d.get("applies")
        for h in d["structured_adjustment"]["hours"]
    }

    energy = float(battery.initial_energy_kwh)
    total_grid = 0.0
    total_cost = 0.0
    peak = 0.0

    for h, p in enumerate(plan):
        g = float(p["grid_kwh"])
        s = float(p["solar_used_kwh"])
        amt = float(p["battery_kwh"])
        action = p["battery_action"]

        if g < -TOL or s < -TOL or amt < -TOL:
            raise ReplayError(f"negative quantity at hour {h}")
        if action not in ("charge", "discharge", "idle"):
            raise ReplayError(f"bad battery_action at hour {h}")
        if action == "idle" and abs(amt) > TOL:
            raise ReplayError(f"idle hour {h} has non-zero battery_kwh")
        if s > solar[h] + TOL:
            raise ReplayError(f"solar_used exceeds effective solar at hour {h}")

        charge = amt if action == "charge" else 0.0
        discharge = amt if action == "discharge" else 0.0
        if charge > battery.max_charge_kwh_per_hour + TOL:
            raise ReplayError(f"charge rate exceeded at hour {h}")
        if discharge > battery.max_discharge_kwh_per_hour + TOL:
            raise ReplayError(f"discharge rate exceeded at hour {h}")
        if charge > TOL and h in no_charge:
            raise ReplayError(f"no_charge_window violated at hour {h}")
        if discharge > TOL and h in no_discharge:
            raise ReplayError(f"no_discharge_window violated at hour {h}")
        if h in caps and g > caps[h] + TOL:
            raise ReplayError(f"max_grid_window violated at hour {h}")

        if abs((g + s + discharge) - (float(hours[h].demand_kwh) + charge)) > TOL:
            raise ReplayError(f"energy balance violated at hour {h}")

        energy = energy + charge - discharge
        if abs(energy - float(p["battery_energy_after_kwh"])) > TOL:
            raise ReplayError(f"battery transition mismatch at hour {h}")
        if energy < floors[h] - TOL:
            raise ReplayError(f"battery below required floor at hour {h}")
        if energy > battery.capacity_kwh + TOL:
            raise ReplayError(f"battery above capacity at hour {h}")

        total_grid += g
        total_cost += g * float(hours[h].tariff_bdt_per_kwh)
        peak = max(peak, g)

    if abs(energy - float(battery.initial_energy_kwh)) > TOL:
        raise ReplayError("end-of-day battery neutrality violated")

    return {
        "total_grid_kwh": round(total_grid, 4),
        "total_cost_bdt": round(total_cost, 4),
        "peak_grid_kwh": round(peak, 4),
    }
