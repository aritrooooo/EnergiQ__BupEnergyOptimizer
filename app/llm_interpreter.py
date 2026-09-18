"""The ONLY module that talks to an LLM.

Claude is called once, with forced tool use, and its structured output is what
produces `directive_interpretation`. Output is untrusted until guardrails pass it.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from . import config
from .schemas import Battery, HourInput

log = logging.getLogger("gridwise.llm")

TOOL_NAME = "emit_directive_interpretation"

_HOURS_SCHEMA = {
    "type": "array",
    "items": {"type": "integer", "minimum": 0, "maximum": 23},
    "description": "Unique ascending hours 0-23 the directive applies to (start hour included, end hour excluded).",
}

TOOL = {
    "name": TOOL_NAME,
    "description": "Return exactly one structured directive interpretation per operator note, in note_index order.",
    "input_schema": {
        "type": "object",
        "properties": {
            "interpretations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "note_index": {"type": "integer", "minimum": 0},
                        "applies": {"type": "boolean"},
                        "directive_type": {
                            "type": "string",
                            "enum": [
                                "solar_reduction",
                                "minimum_battery_reserve",
                                "no_charge_window",
                                "no_discharge_window",
                                "max_grid_window",
                                "no_op",
                            ],
                        },
                        "structured_adjustment": {
                            "type": ["object", "null"],
                            "properties": {
                                "hours": _HOURS_SCHEMA,
                                "factor": {"type": "number", "minimum": 0, "maximum": 1},
                                "minimum_energy_kwh": {"type": "number", "minimum": 0},
                                "max_grid_kwh": {"type": "number", "minimum": 0},
                            },
                            "additionalProperties": False,
                        },
                        "explanation": {"type": "string"},
                    },
                    "required": [
                        "note_index",
                        "applies",
                        "directive_type",
                        "structured_adjustment",
                        "explanation",
                    ],
                },
            }
        },
        "required": ["interpretations"],
    },
}

SYSTEM_PROMPT = """You interpret shift-handover notes from a microgrid operator into structured \
directives for a 24-hour energy scheduler. You never optimise anything yourself and you never \
invent data.

You may emit ONLY these six directive types:
1. solar_reduction - usable solar is reduced during hours.
   structured_adjustment: {"hours": [...], "factor": <0..1>}
   factor is the FRACTION OF SOLAR THAT REMAINS. "output drops to 20%" -> factor 0.2.
   "an 80% reduction" -> factor 0.2. "panels offline" -> factor 0.0.
2. minimum_battery_reserve - battery stored energy must stay at or above a level during hours.
   structured_adjustment: {"hours": [...], "minimum_energy_kwh": <number>}
3. no_charge_window - charging forbidden during hours.
   structured_adjustment: {"hours": [...]}
4. no_discharge_window - discharging forbidden during hours.
   structured_adjustment: {"hours": [...]}
5. max_grid_window - grid import capped during hours.
   structured_adjustment: {"hours": [...], "max_grid_kwh": <number>}
6. no_op - the note has no effect on today's schedule.
   structured_adjustment: null

Rules you must follow exactly:
- Return exactly one interpretation per note, with note_index matching the note's index, in \
ascending order, no duplicates and none missing.
- hours must be unique ascending integers 0-23, using a whole-hour convention where the START \
hour is INCLUDED and the END hour is EXCLUDED: "1 PM to 3 PM" -> [13, 14]; "between 2 PM and 4 PM" \
-> [14, 15]; "from 9 AM to noon" -> [9, 10, 11]. Midnight is hour 0.
- applies is true for the five real directive types (each with a fully shaped \
structured_adjustment) and false ONLY for no_op (whose structured_adjustment is null).
- If a note does not affect today's 24-hour energy schedule, or describes something outside \
solar/battery/grid behaviour, you must return no_op with applies=false. Never invent a directive \
type outside the six listed. Never invent numeric values not stated or clearly implied by the note.
- Never modify demand, tariffs, or battery specification values. Those are given facts.
- explanation: one short sentence, plain language.

Call the emit_directive_interpretation tool. Do not reply with prose."""


def _user_message(notes: list[str], hours: list[HourInput], battery: Battery) -> str:
    ctx = {
        "battery": battery.model_dump(),
        "hours_available": [
            {"hour": h.hour, "solar_kwh": h.solar_kwh, "tariff_bdt_per_kwh": h.tariff_bdt_per_kwh}
            for h in hours
        ],
    }
    numbered = "\n".join(f"[{i}] {n}" for i, n in enumerate(notes))
    return (
        "Scenario context (read-only, never alter these numbers):\n"
        f"{json.dumps(ctx)}\n\n"
        f"Operator notes ({len(notes)} total):\n{numbered}\n\n"
        f"Emit exactly {len(notes)} interpretations, note_index 0..{len(notes) - 1}."
    )


# --------------------------------------------------------------------------- mock


_TIME_RE = re.compile(
    r"(\d{1,2})\s*(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\s*(?:to|-|–|until|till|and)\s*"
    r"(\d{1,2})\s*(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?",
    re.I,
)


def _to_24(val: int, mer: str | None, other_mer: str | None) -> int:
    mer = (mer or other_mer or "").lower().replace(".", "")
    if mer.startswith("p") and val != 12:
        return val + 12
    if mer.startswith("a") and val == 12:
        return 0
    return val % 24


def _parse_window(text: str) -> list[int] | None:
    m = _TIME_RE.search(text)
    if not m:
        if "noon" in text.lower() and "midnight" in text.lower():
            return list(range(12, 24))
        return None
    start = _to_24(int(m.group(1)), m.group(3), m.group(6))
    end = _to_24(int(m.group(4)), m.group(6), m.group(3))
    if end <= start:
        end = start + 1
    end = min(end, 24)
    return list(range(start, end)) or None


def _mock_interpret(notes: list[str], battery: Battery) -> list[dict[str, Any]]:
    """Deterministic canned interpreter used when MOCK_LLM=1 (tests / no credits)."""
    out: list[dict[str, Any]] = []
    for i, note in enumerate(notes):
        low = note.lower()
        hours = _parse_window(note)
        entry: dict[str, Any] | None = None

        if hours and "solar" in low:
            pct = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", low)
            if pct:
                p = float(pct.group(1)) / 100.0
                factor = max(0.0, min(1.0, 1.0 - p if "reduc" in low or "drop by" in low or "down by" in low else p))
            else:
                factor = 0.0
            entry = {
                "note_index": i,
                "applies": True,
                "directive_type": "solar_reduction",
                "structured_adjustment": {"hours": hours, "factor": factor},
                "explanation": f"Usable solar is limited to {factor:.0%} of forecast during these hours.",
            }
        elif hours and ("not charge" in low or "no charging" in low or "avoid charging" in low or "don't charge" in low):
            entry = {
                "note_index": i,
                "applies": True,
                "directive_type": "no_charge_window",
                "structured_adjustment": {"hours": hours},
                "explanation": "Battery charging is forbidden during this window.",
            }
        elif hours and ("not discharge" in low or "no discharging" in low or "avoid discharging" in low):
            entry = {
                "note_index": i,
                "applies": True,
                "directive_type": "no_discharge_window",
                "structured_adjustment": {"hours": hours},
                "explanation": "Battery discharging is forbidden during this window.",
            }
        elif hours and ("grid" in low and ("cap" in low or "limit" in low or "no more than" in low or "at most" in low)):
            num = re.search(r"(\d+(?:\.\d+)?)\s*kwh", low)
            entry = {
                "note_index": i,
                "applies": True,
                "directive_type": "max_grid_window",
                "structured_adjustment": {
                    "hours": hours,
                    "max_grid_kwh": float(num.group(1)) if num else 0.0,
                },
                "explanation": "Grid import is capped during this window.",
            }
        elif "battery" in low and ("reserve" in low or "above" in low or "at least" in low or "keep" in low):
            num = re.search(r"(\d+(?:\.\d+)?)\s*kwh", low)
            if num:
                entry = {
                    "note_index": i,
                    "applies": True,
                    "directive_type": "minimum_battery_reserve",
                    "structured_adjustment": {
                        "hours": hours or list(range(24)),
                        "minimum_energy_kwh": min(float(num.group(1)), battery.capacity_kwh),
                    },
                    "explanation": "A higher battery reserve floor is required during these hours.",
                }

        out.append(
            entry
            or {
                "note_index": i,
                "applies": False,
                "directive_type": "no_op",
                "structured_adjustment": None,
                "explanation": "This note does not affect today's energy schedule.",
            }
        )
    return out


# --------------------------------------------------------------------------- live


def _call_gemini(notes, hours, battery):
    from google import genai
    from google.genai import types

    client = genai.Client(
        api_key=config.GEMINI_API_KEY,
        http_options=types.HttpOptions(timeout=int(config.REQUEST_TIMEOUT_SECONDS * 1000)),
    )
    system = SYSTEM_PROMPT.replace(
        "Call the emit_directive_interpretation tool. Do not reply with prose.",
        'Respond with ONLY JSON: {"interpretations":[{"note_index":int,"applies":bool,'
        '"directive_type":str,"structured_adjustment":object|null,"explanation":str}]}',
    )
    resp = client.models.generate_content(
        model=config.LLM_MODEL,
        contents=_user_message(notes, hours, battery),
        config=types.GenerateContentConfig(
            system_instruction=system,
            temperature=0,
            response_mime_type="application/json",
        ),
    )
    data = json.loads(resp.text)
    if isinstance(data, dict) and isinstance(data.get("interpretations"), list):
        return data["interpretations"]
    if isinstance(data, list):
        return data
    raise ValueError("no usable JSON")


def interpret(notes, hours, battery):
    """Return RAW, UNTRUSTED candidate directives (one attempt + one retry)."""
    if config.MOCK_LLM or not config.GEMINI_API_KEY:
        if not config.MOCK_LLM:
            log.warning("GEMINI_API_KEY unset; using deterministic mock interpreter")
        return _mock_interpret(notes, battery)
    for attempt in (1, 2):
        try:
            return _call_gemini(notes, hours, battery)
        except Exception as exc:  # noqa: BLE001
            log.warning("llm attempt %s failed: %s: %s", attempt, type(exc).__name__, str(exc)[:300])
    log.error("llm failed twice; degrading all notes to no_op")
    return []
