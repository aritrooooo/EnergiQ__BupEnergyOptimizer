import pytest

from app.guardrails import approve, approve_all
from app.schemas import Battery

BAT = Battery(
    capacity_kwh=500,
    initial_energy_kwh=200,
    minimum_energy_kwh=50,
    max_charge_kwh_per_hour=100,
    max_discharge_kwh_per_hour=100,
)


def ok(d):
    return d["applies"] and d["directive_type"] != "no_op"


def is_noop(d):
    return (
        d["applies"] is False
        and d["directive_type"] == "no_op"
        and d["structured_adjustment"] is None
    )


# ---------------------------------------------------------------- happy paths


@pytest.mark.parametrize(
    "dtype,adj",
    [
        ("solar_reduction", {"hours": [13, 14], "factor": 0.2}),
        ("minimum_battery_reserve", {"hours": [8, 9], "minimum_energy_kwh": 150}),
        ("no_charge_window", {"hours": [14, 15]}),
        ("no_discharge_window", {"hours": [10, 11, 12]}),
        ("max_grid_window", {"hours": [18, 19, 20], "max_grid_kwh": 100}),
    ],
)
def test_happy_path_each_type(dtype, adj):
    out = approve(
        {"note_index": 0, "applies": True, "directive_type": dtype,
         "structured_adjustment": adj, "explanation": "x"},
        0, BAT,
    )
    assert ok(out) and out["directive_type"] == dtype
    assert out["structured_adjustment"]["hours"] == adj["hours"]


def test_happy_path_no_op():
    out = approve(
        {"note_index": 0, "applies": False, "directive_type": "no_op",
         "structured_adjustment": None, "explanation": "irrelevant"},
        0, BAT,
    )
    assert is_noop(out)


# ---------------------------------------------------------------- rejections


def test_unsupported_type_degrades():
    assert is_noop(approve(
        {"note_index": 0, "applies": True, "directive_type": "shutdown_plant",
         "structured_adjustment": {"hours": [1]}, "explanation": "x"}, 0, BAT))


def test_non_object_candidate():
    assert is_noop(approve("charge more", 0, BAT))


def test_factor_out_of_range():
    for f in (-0.1, 1.5, "0.2", float("nan")):
        assert is_noop(approve(
            {"note_index": 0, "applies": True, "directive_type": "solar_reduction",
             "structured_adjustment": {"hours": [13], "factor": f}, "explanation": "x"}, 0, BAT))


def test_hours_out_of_range_or_wrong_type():
    for hrs in ([24], [-1], ["13"], [], "13", [1.5]):
        assert is_noop(approve(
            {"note_index": 0, "applies": True, "directive_type": "no_charge_window",
             "structured_adjustment": {"hours": hrs}, "explanation": "x"}, 0, BAT))


def test_hours_unsorted_are_normalised_not_rejected():
    out = approve(
        {"note_index": 0, "applies": True, "directive_type": "no_charge_window",
         "structured_adjustment": {"hours": [15, 14, 14]}, "explanation": "x"}, 0, BAT)
    assert ok(out) and out["structured_adjustment"]["hours"] == [14, 15]


def test_reserve_above_capacity():
    assert is_noop(approve(
        {"note_index": 0, "applies": True, "directive_type": "minimum_battery_reserve",
         "structured_adjustment": {"hours": [8], "minimum_energy_kwh": 5000}, "explanation": "x"}, 0, BAT))


def test_negative_grid_cap():
    assert is_noop(approve(
        {"note_index": 0, "applies": True, "directive_type": "max_grid_window",
         "structured_adjustment": {"hours": [18], "max_grid_kwh": -5}, "explanation": "x"}, 0, BAT))


def test_applies_mismatch_real_type():
    assert is_noop(approve(
        {"note_index": 0, "applies": False, "directive_type": "no_charge_window",
         "structured_adjustment": {"hours": [14]}, "explanation": "x"}, 0, BAT))


def test_no_op_with_adjustment_is_still_no_op():
    out = approve(
        {"note_index": 0, "applies": True, "directive_type": "no_op",
         "structured_adjustment": {"hours": [1]}, "explanation": "x"}, 0, BAT)
    assert is_noop(out)


def test_extra_or_missing_adjustment_keys():
    # invented field (attempt to change demand) -> rejected
    assert is_noop(approve(
        {"note_index": 0, "applies": True, "directive_type": "no_charge_window",
         "structured_adjustment": {"hours": [14], "demand_kwh": 999}, "explanation": "x"}, 0, BAT))
    # missing required field
    assert is_noop(approve(
        {"note_index": 0, "applies": True, "directive_type": "solar_reduction",
         "structured_adjustment": {"hours": [14]}, "explanation": "x"}, 0, BAT))


# ---------------------------------------------------------------- mapping


def test_missing_note_index_filled_with_no_op():
    out = approve_all([
        {"note_index": 0, "applies": True, "directive_type": "no_charge_window",
         "structured_adjustment": {"hours": [14]}, "explanation": "x"}], 3, BAT)
    assert len(out) == 3
    assert [d["note_index"] for d in out] == [0, 1, 2]
    assert ok(out[0]) and is_noop(out[1]) and is_noop(out[2])


def test_duplicate_note_index_keeps_first():
    out = approve_all([
        {"note_index": 0, "applies": True, "directive_type": "no_charge_window",
         "structured_adjustment": {"hours": [14]}, "explanation": "first"},
        {"note_index": 0, "applies": True, "directive_type": "no_discharge_window",
         "structured_adjustment": {"hours": [2]}, "explanation": "second"}], 1, BAT)
    assert len(out) == 1 and out[0]["directive_type"] == "no_charge_window"


def test_out_of_range_note_index_dropped():
    out = approve_all([
        {"note_index": 7, "applies": True, "directive_type": "no_charge_window",
         "structured_adjustment": {"hours": [14]}, "explanation": "x"}], 2, BAT)
    assert len(out) == 2 and all(is_noop(d) for d in out)


def test_garbage_llm_output_never_crashes():
    for junk in (None, "oops", {}, [1, 2, 3], [{"note_index": "a"}]):
        out = approve_all(junk, 2, BAT)
        assert len(out) == 2 and all(is_noop(d) for d in out)
