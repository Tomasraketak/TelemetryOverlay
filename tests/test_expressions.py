"""Derived channels and the expression sandbox."""

from __future__ import annotations

import numpy as np
import pytest

from telemetry_overlay.data import load
from telemetry_overlay.expressions import add_derived, evaluate

from conftest import CSV_SAMPLE


def test_integral_of_thrust_matches_a_manual_trapezoid():
    telemetry = load(CSV_SAMPLE)
    impulse = evaluate("integral(thrust)", telemetry)
    expected = np.trapezoid(telemetry["thrust"].values, telemetry.time)
    assert impulse[-1] == pytest.approx(expected, rel=1e-9)


def test_arithmetic_across_channels():
    telemetry = load(CSV_SAMPLE)
    result = evaluate("thrust / g", telemetry)
    assert result.max() == pytest.approx(telemetry["thrust"].values.max() / 9.80665)


def test_derivative_and_smooth_keep_the_sample_count():
    telemetry = load(CSV_SAMPLE)
    for expression in ("derivative(thrust)", "smooth(thrust, 9)",
                       "running_max(thrust)", "clip(thrust, 0, 100)"):
        assert evaluate(expression, telemetry).shape == telemetry.time.shape


def test_running_max_never_decreases():
    telemetry = load(CSV_SAMPLE)
    values = evaluate("running_max(thrust)", telemetry)
    assert np.all(np.diff(values) >= 0)


def test_scalar_expression_broadcasts():
    telemetry = load(CSV_SAMPLE)
    assert np.all(evaluate("42", telemetry) == 42)


def test_add_derived_registers_a_channel():
    telemetry = load(CSV_SAMPLE)
    add_derived(telemetry, [{"key": "impulse", "unit": "N.s",
                             "expression": "integral(clip(thrust, 0, 1e9))"}])
    assert "impulse" in telemetry
    assert telemetry["impulse"].unit == "N.s"
    assert telemetry["impulse"].values[-1] > 0


def test_derived_channels_can_build_on_each_other():
    telemetry = load(CSV_SAMPLE)
    add_derived(telemetry, [
        {"key": "positive", "expression": "clip(thrust, 0, 1e9)"},
        {"key": "impulse", "expression": "integral(positive)"},
    ])
    assert telemetry["impulse"].values[-1] > 0


@pytest.mark.parametrize("expression", [
    "__import__('os').system('echo hi')",
    "open('/etc/passwd').read()",
    "eval('1+1')",
    "(lambda: 1)()",
    "thrust.__class__",
])
def test_dangerous_expressions_are_refused(expression):
    telemetry = load(CSV_SAMPLE)
    with pytest.raises(ValueError):
        evaluate(expression, telemetry)


def test_unknown_names_are_reported_with_the_alternatives():
    telemetry = load(CSV_SAMPLE)
    with pytest.raises(ValueError, match="unknown name"):
        evaluate("no_such_channel * 2", telemetry)


def test_syntax_errors_are_reported_clearly():
    telemetry = load(CSV_SAMPLE)
    with pytest.raises(ValueError, match="cannot parse"):
        evaluate("thrust +* 2", telemetry)


def test_wrong_length_result_is_rejected():
    telemetry = load(CSV_SAMPLE)
    with pytest.raises(ValueError, match="expected"):
        evaluate("thrust[:10]", telemetry)
