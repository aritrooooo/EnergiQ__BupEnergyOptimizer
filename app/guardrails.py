"""Deterministic validation of untrusted LLM output (Section 2.4).

Nothing reaches the optimizer that has not passed through `approve_all`. Any
candidate that fails any check is degraded to `no_op` and logged server-side only.
"""
from __future__ import annotations

import logging
import math
from typing import Any

from .schemas import DIRECTIVE_TYPES, Battery

log = logging.getLogger("gridwise.guardrails")

NO_OP_EXPLANATION = "This note does not affect today's energy schedule."
ALLOWED = set(DIRECTIVE_TYPES)
SHAPES: dict[str, set[str]] = {
    "solar_reduction": {"hours", "factor"},
    "minimum_battery_reserve": {"hours", "minimum_energy_kwh"},
    "no_charge_window": {"hours"},
    "no_discharge_window": {"hours"},
    "max_grid_window": {"hours", "max_grid_kwh"},
}


def _no_op(note_index: int, explanation: str | None = None) -> dict[str, Any]:
    return {
        "note_index": note_index,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": explanation or NO_OP_EXPLANATION,
    }


def _clean_hours(raw: Any) -> list[int] | None:
    if not isinstance(raw, list) or not raw:
        return None
    out: list[int] = []
    for h in raw:
        if isinstance(h, bool) or not isinstance(h, (int, float)):
            return None
        if isinstance(h, float) and not float(h).is_integer():
            return None
        h = int(h)
        if not 0 <= h <= 23:
            return None
        out.append(h)
    if len(set(out)) != len(out):
        log.warning("guardrails: duplicate hours in candidate, deduping")
    if out != sorted(set(out)):
        log.warning("guardrails: hours not unique-ascending as emitted, normalising")
    return sorted(set(out))


def _finite(x: Any) -> bool:
    return (
        not isinstance(x, bool)
        and isinstance(x, (int, float))
        and math.isfinite(float(x))
    )


def approve(candidate: Any, note_index: int, battery: Battery) -> dict[str, Any]:
    """Validate one candidate directive. Returns a safe interpretation entry."""
    if not isinstance(candidate, dict):
        log.warning("guardrails[%s]: candidate is not an object", note_index)
        return _no_op(note_index)

    dtype = candidate.get("directive_type")
    if dtype not in ALLOWED:
        log.warning("guardrails[%s]: unsupported directive_type %r", note_index, dtype)
        return _no_op(note_index)

    applies = candidate.get("applies")
    adj = candidate.get("structured_adjustment")
    explanation = candidate.get("explanation")
    if not isinstance(explanation, str) or not explanation.strip():
        explanation = None
    else:
        explanation = explanation.strip()[:400]

    if dtype == "no_op":
        if applies is not False or adj not in (None, {}):
            log.warning("guardrails[%s]: no_op with bad applies/adjustment", note_index)
        return _no_op(note_index, explanation)

    if applies is not True:
        log.warning("guardrails[%s]: %s must have applies=true", note_index, dtype)
        return _no_op(note_index)
    if not isinstance(adj, dict):
        log.warning("guardrails[%s]: missing structured_adjustment", note_index)
        return _no_op(note_index)
    if set(adj.keys()) != SHAPES[dtype]:
        log.warning("guardrails[%s]: %s has wrong adjustment keys %s", note_index, dtype, sorted(adj))
        return _no_op(note_index)

    hours = _clean_hours(adj.get("hours"))
    if hours is None:
        log.warning("guardrails[%s]: bad hours array", note_index)
        return _no_op(note_index)

    clean: dict[str, Any] = {"hours": hours}

    if dtype == "solar_reduction":
        f = adj.get("factor")
        if not _finite(f) or not 0.0 <= float(f) <= 1.0:
            log.warning("guardrails[%s]: factor out of range", note_index)
            return _no_op(note_index)
        clean["factor"] = float(f)
    elif dtype == "minimum_battery_reserve":
        r = adj.get("minimum_energy_kwh")
        if not _finite(r) or float(r) < 0 or float(r) > battery.capacity_kwh:
            log.warning("guardrails[%s]: reserve out of range", note_index)
            return _no_op(note_index)
        clean["minimum_energy_kwh"] = float(r)
    elif dtype == "max_grid_window":
        g = adj.get("max_grid_kwh")
        if not _finite(g) or float(g) < 0:
            log.warning("guardrails[%s]: max_grid_kwh out of range", note_index)
            return _no_op(note_index)
        clean["max_grid_kwh"] = float(g)

    return {
        "note_index": note_index,
        "applies": True,
        "directive_type": dtype,
        "structured_adjustment": clean,
        "explanation": explanation or f"Applied {dtype} for hours {hours}.",
    }


def approve_all(candidates: Any, n_notes: int, battery: Battery) -> list[dict[str, Any]]:
    """Map arbitrary LLM output onto exactly one approved entry per note, in order."""
    by_index: dict[int, Any] = {}
    if isinstance(candidates, list):
        for c in candidates:
            if not isinstance(c, dict):
                continue
            idx = c.get("note_index")
            if isinstance(idx, bool) or not isinstance(idx, int):
                continue
            if not 0 <= idx < n_notes:
                log.warning("guardrails: note_index %r out of range", idx)
                continue
            if idx in by_index:
                log.warning("guardrails: duplicate note_index %s, keeping first", idx)
                continue
            by_index[idx] = c

    out = []
    for i in range(n_notes):
        if i not in by_index:
            log.warning("guardrails: no candidate for note_index %s, defaulting to no_op", i)
            out.append(_no_op(i))
        else:
            out.append(approve(by_index[i], i, battery))
    return out
