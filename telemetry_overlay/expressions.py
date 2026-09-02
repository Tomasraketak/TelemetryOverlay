"""Derived channels computed from the logged ones.

Overlays routinely want a quantity the logger never recorded - total impulse
is the integral of thrust, acceleration is thrust over mass. Rather than make
users pre-process their CSV, a config can declare derived channels as small
expressions evaluated once at load time.

Only a fixed vocabulary of names is exposed, and the compiled code object is
checked for attribute access and imports, so a config file cannot reach out
of the sandbox.
"""

from __future__ import annotations

import math
from typing import Any, Callable

import numpy as np

from .data import Channel, Telemetry

__all__ = ["evaluate", "add_derived", "FUNCTIONS"]


def _integral(y: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Cumulative trapezoidal integral of ``y`` over time."""
    y = np.nan_to_num(np.asarray(y, dtype=float), nan=0.0)
    if y.size < 2:
        return np.zeros_like(y)
    steps = np.diff(t)
    increments = steps * 0.5 * (y[:-1] + y[1:])
    return np.concatenate(([0.0], np.cumsum(increments)))


def _derivative(y: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Time derivative, using central differences where possible."""
    y = np.asarray(y, dtype=float)
    if y.size < 2:
        return np.zeros_like(y)
    return np.gradient(y, t)


def _smooth(y: np.ndarray, t: np.ndarray, window: float = 5) -> np.ndarray:
    """Moving average over a window given in samples."""
    y = np.asarray(y, dtype=float)
    n = max(1, int(round(float(window))))
    if n <= 1 or y.size < 2:
        return y
    kernel = np.ones(n) / n
    filled = np.nan_to_num(y, nan=float(np.nanmean(y)) if np.isfinite(y).any() else 0.0)
    padded = np.pad(filled, (n // 2, n - 1 - n // 2), mode="edge")
    return np.convolve(padded, kernel, mode="valid")


def _running_max(y: np.ndarray, t: np.ndarray) -> np.ndarray:
    y = np.asarray(y, dtype=float)
    return np.maximum.accumulate(np.nan_to_num(y, nan=-np.inf))


def _running_min(y: np.ndarray, t: np.ndarray) -> np.ndarray:
    y = np.asarray(y, dtype=float)
    return np.minimum.accumulate(np.nan_to_num(y, nan=np.inf))


def _clip(y: np.ndarray, lo: float, hi: float) -> np.ndarray:
    return np.clip(np.asarray(y, dtype=float), float(lo), float(hi))


# Functions callable from an expression. Those needing the time base are
# wrapped at evaluation time so the user writes ``integral(thrust)``.
_TIME_AWARE: dict[str, Callable[..., np.ndarray]] = {
    "integral": _integral,
    "derivative": _derivative,
    "smooth": _smooth,
    "running_max": _running_max,
    "running_min": _running_min,
}

FUNCTIONS: dict[str, Any] = {
    "abs": np.abs, "sqrt": np.sqrt, "exp": np.exp, "log": np.log,
    "log10": np.log10, "sin": np.sin, "cos": np.cos, "tan": np.tan,
    "atan": np.arctan, "atan2": np.arctan2, "floor": np.floor,
    "ceil": np.ceil, "round": np.round, "sign": np.sign,
    "minimum": np.minimum, "maximum": np.maximum, "clip": _clip,
    "where": np.where, "nan_to_num": np.nan_to_num,
    "pi": math.pi, "e": math.e, "g": 9.80665,
}

# Names that make an expression dangerous regardless of the namespace given.
_BANNED_SUBSTRINGS = ("__", "import", "eval", "exec", "compile", "open",
                      "globals", "locals", "getattr", "setattr", "lambda")


def _check(expression: str) -> None:
    lowered = expression.lower()
    for banned in _BANNED_SUBSTRINGS:
        if banned in lowered:
            raise ValueError(
                f"expression contains the forbidden token {banned!r}: {expression}"
            )
    if len(expression) > 2000:
        raise ValueError("expression is unreasonably long")


def evaluate(expression: str, telemetry: Telemetry) -> np.ndarray:
    """Evaluate an expression over the whole time base, returning an array."""
    _check(expression)
    t = telemetry.time
    namespace: dict[str, Any] = dict(FUNCTIONS)
    namespace["t"] = t
    namespace["time"] = t
    for key, channel in telemetry.channels.items():
        namespace[key] = channel.values
    for name, func in _TIME_AWARE.items():
        namespace[name] = (lambda f: lambda y, *a: f(np.asarray(y, dtype=float),
                                                     t, *a))(func)

    try:
        code = compile(expression, "<expression>", "eval")
    except SyntaxError as exc:
        raise ValueError(f"cannot parse expression {expression!r}: {exc.msg}") from exc
    for name in code.co_names:
        if name not in namespace:
            raise ValueError(
                f"expression uses unknown name {name!r}. Available channels: "
                f"{', '.join(sorted(telemetry.channels))}. "
                f"Available functions: {', '.join(sorted(FUNCTIONS))}, "
                f"{', '.join(sorted(_TIME_AWARE))}."
            )
    try:
        result = eval(code, {"__builtins__": {}}, namespace)  # noqa: S307
    except Exception as exc:  # noqa: BLE001 - report any maths error clearly
        raise ValueError(f"error evaluating {expression!r}: {exc}") from exc

    array = np.asarray(result, dtype=float)
    if array.ndim == 0:
        array = np.full(t.shape, float(array))
    if array.shape != t.shape:
        raise ValueError(
            f"expression {expression!r} produced {array.shape} values, "
            f"expected {t.shape}"
        )
    return array


def add_derived(telemetry: Telemetry, specs: list[dict[str, Any]] | None) -> None:
    """Add every derived channel from a config list into ``telemetry``.

    Definitions are applied in order, so a later expression can build on an
    earlier one.
    """
    for spec in specs or []:
        if not isinstance(spec, dict):
            raise ValueError(f"derived channel must be a mapping, got {spec!r}")
        key = spec.get("key") or spec.get("name")
        expression = spec.get("expression") or spec.get("expr")
        if not key or not expression:
            raise ValueError(
                f"derived channel needs 'key' and 'expression': {spec!r}")
        values = evaluate(str(expression), telemetry)
        telemetry.add(Channel(
            key=str(key),
            label=str(spec.get("label", key)),
            unit=str(spec.get("unit", "")),
            values=values,
            kind="numeric",
        ))
