"""MILP formulation of the GridWise scheduling problem (Section 2.5).

Binary indicators enforce exactly one battery action per hour, which is why this
is a MILP and not a plain LP.
"""
from __future__ import annotations

import logging
from typing import Any

import pulp

from .schemas import Battery, HourInput

log = logging.getLogger("gridwise.optimizer")

EPS = 1e-6


class InfeasibleError(RuntimeError):
    """Raised when the MILP has no feasible schedule."""


def effective_solar(hours: list[HourInput], directives: list[dict[str, Any]]) -> list[float]:
    solar = [float(h.solar_kwh) for h in hours]
    for d in directives:
        if d.get("directive_type") == "solar_reduction" and d.get("applies"):
            adj = d["structured_adjustment"]
            for h in adj["hours"]:
                solar[h] *= float(adj["factor"])
    return solar


def effective_min_energy(battery: Battery, directives: list[dict[str, Any]]) -> list[float]:
    floors = [float(battery.minimum_energy_kwh)] * 24
    for d in directives:
        if d.get("directive_type") == "minimum_battery_reserve" and d.get("applies"):
            adj = d["structured_adjustment"]
            for h in adj["hours"]:
                floors[h] = max(floors[h], float(adj["minimum_energy_kwh"]))
    return floors


def _hour_sets(directives: list[dict[str, Any]], dtype: str) -> set[int]:
    out: set[int] = set()
    for d in directives:
        if d.get("directive_type") == dtype and d.get("applies"):
            out.update(d["structured_adjustment"]["hours"])
    return out


def grid_caps(directives: list[dict[str, Any]]) -> dict[int, float]:
    caps: dict[int, float] = {}
    for d in directives:
        if d.get("directive_type") == "max_grid_window" and d.get("applies"):
            adj = d["structured_adjustment"]
            for h in adj["hours"]:
                cap = float(adj["max_grid_kwh"])
                caps[h] = min(caps[h], cap) if h in caps else cap
    return caps


def _r(x: float) -> float:
    x = float(x)
    if abs(x) < EPS:
        return 0.0
    return round(x, 6)


def solve(
    hours: list[HourInput], battery: Battery, directives: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return a cost-minimal 24-hour plan, or raise InfeasibleError."""
    hours = sorted(hours, key=lambda h: h.hour)
    solar = effective_solar(hours, directives)
    floors = effective_min_energy(battery, directives)
    no_charge = _hour_sets(directives, "no_charge_window")
    no_discharge = _hour_sets(directives, "no_discharge_window")
    caps = grid_caps(directives)

    H = range(24)
    prob = pulp.LpProblem("gridwise", pulp.LpMinimize)

    grid = {h: pulp.LpVariable(f"grid_{h}", lowBound=0) for h in H}
    sol_used = {h: pulp.LpVariable(f"sol_{h}", lowBound=0, upBound=max(solar[h], 0.0)) for h in H}
    chg = {h: pulp.LpVariable(f"chg_{h}", lowBound=0, upBound=battery.max_charge_kwh_per_hour) for h in H}
    dis = {h: pulp.LpVariable(f"dis_{h}", lowBound=0, upBound=battery.max_discharge_kwh_per_hour) for h in H}
    energy = {
        h: pulp.LpVariable(f"e_{h}", lowBound=floors[h], upBound=battery.capacity_kwh) for h in H
    }
    is_c = {h: pulp.LpVariable(f"ic_{h}", cat="Binary") for h in H}
    is_d = {h: pulp.LpVariable(f"id_{h}", cat="Binary") for h in H}
    is_i = {h: pulp.LpVariable(f"ii_{h}", cat="Binary") for h in H}

    prob += pulp.lpSum(grid[h] * float(hours[h].tariff_bdt_per_kwh) for h in H)

    for h in H:
        demand = float(hours[h].demand_kwh)
        prob += grid[h] + sol_used[h] + dis[h] == demand + chg[h], f"bal_{h}"
        prob += sol_used[h] <= solar[h], f"solcap_{h}"
        prob += is_c[h] + is_d[h] + is_i[h] == 1, f"one_action_{h}"
        prob += chg[h] <= battery.max_charge_kwh_per_hour * is_c[h], f"chgcap_{h}"
        prob += dis[h] <= battery.max_discharge_kwh_per_hour * is_d[h], f"discap_{h}"
        prev = battery.initial_energy_kwh if h == 0 else energy[h - 1]
        prob += energy[h] == prev + chg[h] - dis[h], f"transition_{h}"
        if h in no_charge:
            prob += is_c[h] == 0, f"nochg_{h}"
        if h in no_discharge:
            prob += is_d[h] == 0, f"nodis_{h}"
        if h in caps:
            prob += grid[h] <= caps[h], f"gridcap_{h}"

    prob += energy[23] == battery.initial_energy_kwh, "neutral"

    status = prob.solve(pulp.PULP_CBC_CMD(msg=False))
    if pulp.LpStatus[status] != "Optimal":
        log.error("MILP not optimal: status=%s", pulp.LpStatus[status])
        raise InfeasibleError(pulp.LpStatus[status])

    plan: list[dict[str, Any]] = []
    for h in H:
        c, d = _r(chg[h].value() or 0.0), _r(dis[h].value() or 0.0)
        if c > 0 and d > 0:  # defensive; binaries should prevent this
            net = c - d
            c, d = (max(net, 0.0), max(-net, 0.0))
        if c > 0:
            action, amount = "charge", c
        elif d > 0:
            action, amount = "discharge", d
        else:
            action, amount = "idle", 0.0
        plan.append(
            {
                "hour": h,
                "grid_kwh": max(_r(grid[h].value() or 0.0), 0.0),
                "solar_used_kwh": max(_r(sol_used[h].value() or 0.0), 0.0),
                "battery_action": action,
                "battery_kwh": round(amount, 6),
                "battery_energy_after_kwh": _r(energy[h].value() or 0.0),
            }
        )
    return plan
