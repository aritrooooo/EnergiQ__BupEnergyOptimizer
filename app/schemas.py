"""Pydantic v2 models for the GridWise API (Sections 1.3 and 1.7)."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DIRECTIVE_TYPES = (
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
)


class HourInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hour: int = Field(ge=0, le=23)
    demand_kwh: float = Field(ge=0)
    solar_kwh: float = Field(ge=0)
    tariff_bdt_per_kwh: float = Field(ge=0)


class Battery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capacity_kwh: float = Field(gt=0)
    initial_energy_kwh: float = Field(ge=0)
    minimum_energy_kwh: float = Field(ge=0)
    max_charge_kwh_per_hour: float = Field(ge=0)
    max_discharge_kwh_per_hour: float = Field(ge=0)

    @model_validator(mode="after")
    def _coherent(self) -> "Battery":
        if self.minimum_energy_kwh > self.capacity_kwh:
            raise ValueError("minimum_energy_kwh exceeds capacity_kwh")
        if not (self.minimum_energy_kwh <= self.initial_energy_kwh <= self.capacity_kwh):
            raise ValueError("initial_energy_kwh outside [minimum_energy_kwh, capacity_kwh]")
        return self


class OptimizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario_id: str = Field(min_length=1)
    operator_notes: list[str] = Field(min_length=1, max_length=3)
    hours: list[HourInput]
    battery: Battery

    @field_validator("operator_notes")
    @classmethod
    def _notes_non_empty(cls, v: list[str]) -> list[str]:
        if any(not s or not s.strip() for s in v):
            raise ValueError("operator_notes entries must be non-empty strings")
        return v

    @field_validator("hours")
    @classmethod
    def _hours_complete(cls, v: list[HourInput]) -> list[HourInput]:
        seen = sorted(h.hour for h in v)
        if len(v) != 24 or seen != list(range(24)):
            raise ValueError("hours must contain exactly 24 unique entries for hours 0..23")
        return v

    def hours_sorted(self) -> list[HourInput]:
        return sorted(self.hours, key=lambda h: h.hour)


class DirectiveInterpretation(BaseModel):
    note_index: int
    applies: bool
    directive_type: Literal[DIRECTIVE_TYPES]  # type: ignore[valid-type]
    structured_adjustment: dict[str, Any] | None
    explanation: str


class HourPlan(BaseModel):
    hour: int
    grid_kwh: float
    solar_used_kwh: float
    battery_action: Literal["charge", "discharge", "idle"]
    battery_kwh: float
    battery_energy_after_kwh: float


class OptimizeResponse(BaseModel):
    scenario_id: str
    directive_interpretation: list[DirectiveInterpretation]
    hourly_plan: list[HourPlan]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str
