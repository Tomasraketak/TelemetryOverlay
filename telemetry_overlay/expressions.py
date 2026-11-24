"""Derived channels computed from the logged ones.

Overays routinely want a quantity the logger never recorded - total impulse
is the integral of thrust, acceleration is thrust over mass, speed in km/h
is speed * 3.6. A config can declare derived channels as expressions evaluated
once at load time, or widgets can pass inline ``expression:`` strings.

Expressions are parsed and validated with Python's ``ast`` module against a
strict whitelist of safe mathematical nodes and function names (no attribute
access, no imports, no lambdas, no comprehensions), so channel names like
``valve_open`` work cleanly without false-positive substring blocks.
"""

from __future__ import annotations

import ast
import math
from typing import Any, Callable

import numpy as np

from .data import Channel, Telemetry
from .formats.enrichment import (
    _ema_filter,
    _lowpass_filter,
    _median_filter,
    haversine_meters,
)

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


def _ema(y: np.ndarray, t: np.ndarray, alpha: float = 0.2) -> np.ndarray:
    return _ema_filter(np.asarray(y, dtype=float), float(alpha))


def _median(y: np.ndarray, t: np.ndarray, window: float = 5) -> np.ndarray:
    return _median_filter(np.asarray(y, dtype=float), max(3, int(round(float(window)))))


def _despike(
    y: np.ndarray, t: np.ndarray, window: float = 7, sigma: float = 3.5
) -> np.ndarray:
    arr = np.asarray(y, dtype=float)
    med = _median_filter(arr, max(3, int(round(float(window)))))
    diff = np.abs(arr - med)
    mad = float(np.nanmedian(diff)) * 1.4826
    thresh = float(sigma) * max(mad, 1e-9)
    return np.where(diff > thresh, med, arr)


def _lowpass(y: np.ndarray, t: np.ndarray, cutoff_hz: float = 2.0) -> np.ndarray:
    return _lowpass_filter(np.asarray(y, dtype=float), t, float(cutoff_hz))


def _running_max(y: np.ndarray, t: np.ndarray) -> np.ndarray:
    y = np.asarray(y, dtype=float)
    return np.maximum.accumulate(np.nan_to_num(y, nan=-np.inf))


def _running_min(y: np.ndarray, t: np.ndarray) -> np.ndarray:
    y = np.asarray(y, dtype=float)
    return np.minimum.accumulate(np.nan_to_num(y, nan=np.inf))


def _cumsum(y: np.ndarray, t: np.ndarray) -> np.ndarray:
    return np.cumsum(np.nan_to_num(np.asarray(y, dtype=float), nan=0.0))


def _diff(y: np.ndarray, t: np.ndarray) -> np.ndarray:
    arr = np.asarray(y, dtype=float)
    if arr.size < 2:
        return np.zeros_like(arr)
    return np.concatenate(([0.0], np.diff(arr)))


def _clip(y: np.ndarray, lo: float, hi: float) -> np.ndarray:
    return np.clip(np.asarray(y, dtype=float), float(lo), float(hi))


def _norm(*args: Any) -> np.ndarray:
    total = np.zeros_like(np.asarray(args[0], dtype=float))
    for a in args:
        arr = np.asarray(a, dtype=float)
        total = total + arr * arr
    return np.sqrt(total)


def _normalize(y: np.ndarray) -> np.ndarray:
    arr = np.asarray(y, dtype=float)
    fin = arr[np.isfinite(arr)]
    if fin.size == 0:
        return np.zeros_like(arr)
    lo, hi = float(fin.min()), float(fin.max())
    if hi <= lo:
        return np.zeros_like(arr)
    return (arr - lo) / (hi - lo)


def _haversine_dist(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    lat_a = np.asarray(lat, dtype=float)
    lon_a = np.asarray(lon, dtype=float)
    if lat_a.size < 2:
        return np.zeros_like(lat_a)
    seg = haversine_meters(lat_a[:-1], lon_a[:-1], lat_a[1:], lon_a[1:])
    return np.concatenate(([0.0], np.cumsum(np.nan_to_num(seg, nan=0.0))))


# Functions callable from an expression that implicitly receive the time base.
_TIME_AWARE: dict[str, Callable[..., np.ndarray]] = {
    "integral": _integral,
    "derivative": _derivative,
    "smooth": _smooth,
    "ema": _ema,
    "median_filter": _median,
    "despike": _despike,
    "lowpass": _lowpass,
    "running_max": _running_max,
    "running_min": _running_min,
    "cumsum": _cumsum,
    "diff": _diff,
}

FUNCTIONS: dict[str, Any] = {
    "abs": np.abs, "sqrt": np.sqrt, "exp": np.exp, "log": np.log,
    "log10": np.log10, "sin": np.sin, "cos": np.cos, "tan": np.tan,
    "asin": np.arcsin, "acos": np.arccos,
    "atan": np.arctan, "atan2": np.arctan2, "hypot": np.hypot,
    "degrees": np.degrees, "radians": np.radians,
    "floor": np.floor, "ceil": np.ceil, "round": np.round, "sign": np.sign,
    "minimum": np.minimum, "maximum": np.maximum, "clip": _clip,
    "where": np.where, "nan_to_num": np.nan_to_num,
    "norm": _norm, "normalize": _normalize, "haversine_dist": _haversine_dist,
    "mean": np.nanmean, "std": np.nanstd, "min_val": np.nanmin, "max_val": np.nanmax,
    # Handy unit conversion helpers
    "to_kmh": lambda x: np.asarray(x, dtype=float) * 3.6,
    "to_mph": lambda x: np.asarray(x, dtype=float) * 2.236936292,
    "to_knots": lambda x: np.asarray(x, dtype=float) * 1.943844492,
    "to_ft": lambda x: np.asarray(x, dtype=float) * 3.280839895,
    "to_psi": lambda x: np.asarray(x, dtype=float) * 14.50377377,
    "to_bar": lambda x: np.asarray(x, dtype=float) * 0.0689475729,
    "to_lbf": lambda x: np.asarray(x, dtype=float) * 0.2248089431,
    "to_kgf": lambda x: np.asarray(x, dtype=float) / 9.80665,
    "c_to_f": lambda x: np.asarray(x, dtype=float) * 1.8 + 32.0,
    "f_to_c": lambda x: (np.asarray(x, dtype=float) - 32.0) / 1.8,
    # Constants
    "pi": math.pi, "e": math.e, "g": 9.80665,
}

# Strict AST node whitelist for safe mathematical evaluation
_ALLOWED_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.Call,
    ast.Name,
    ast.Constant,
    ast.Tuple,
    ast.List,
    ast.Subscript,
    ast.Slice,
    ast.Load,
    # Operators
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.UAdd, ast.USub, ast.Not, ast.Invert,
    ast.BitAnd, ast.BitOr, ast.BitXor,
    ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
)


def _validate_ast(expression: str, allowed_names: set[str], available_channels: list[str]) -> ast.Expression:
    if len(expression) > 2000:
        raise ValueError("expression is unreasonably long")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"cannot parse expression {expression!r}: {exc.msg}") from exc

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(
                f"expression uses disallowed syntax ({type(node).__name__}): {expression}"
            )
        if isinstance(node, ast.Name):
            if node.id.startswith("__"):
                raise ValueError(f"expression uses forbidden name {node.id!r}: {expression}")
            if node.id not in allowed_names:
                raise ValueError(
                    f"expression uses unknown name {node.id!r}. Available channels: "
                    f"{', '.join(sorted(available_channels))}. "
                    f"Available functions: {', '.join(sorted(FUNCTIONS))}, "
                    f"{', '.join(sorted(_TIME_AWARE))}."
                )
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ValueError(f"only direct function calls are allowed: {expression}")
            if node.func.id not in FUNCTIONS and node.func.id not in _TIME_AWARE:
                raise ValueError(
                    f"cannot call {node.func.id!r} in expression: {expression}"
                )
    return tree


def evaluate(expression: str, telemetry: Telemetry) -> np.ndarray:
    """Evaluate an expression over the whole time base, returning an array."""
    t = telemetry.time
    namespace: dict[str, Any] = dict(FUNCTIONS)
    namespace["t"] = t
    namespace["time"] = t
    for key, channel in telemetry.channels.items():
        if channel.kind != "text":
            namespace[key] = channel.values
    for name, func in _TIME_AWARE.items():
        namespace[name] = (
            lambda f: lambda y, *a: f(np.asarray(y, dtype=float), t, *a)
        )(func)

    tree = _validate_ast(expression, set(namespace), list(telemetry.channels))
    code = compile(tree, "<expression>", "eval")
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
    earlier one. Entries with ``optional: true`` are silently skipped if a
    referenced channel is absent from the loaded log.
    """
    for spec in specs or []:
        if not isinstance(spec, dict):
            raise ValueError(f"derived channel must be a mapping, got {spec!r}")
        key = spec.get("key") or spec.get("name")
        expression = spec.get("expression") or spec.get("expr")
        if not key or not expression:
            raise ValueError(
                f"derived channel needs 'key' and 'expression': {spec!r}"
            )
        try:
            values = evaluate(str(expression), telemetry)
        except ValueError:
            if spec.get("optional"):
                continue
            raise
        telemetry.add(
            Channel(
                key=str(key),
                label=str(spec.get("label", key)),
                unit=str(spec.get("unit", "")),
                values=values,
                kind="numeric",
            )
        )
