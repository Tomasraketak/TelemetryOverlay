"""Overlay widgets.

Importing this package registers every built-in widget type in
:data:`telemetry_overlay.widgets.base.WIDGET_TYPES`.
"""

from .base import (  # noqa: F401
    RenderContext, Widget, WIDGET_TYPES, build_widget, format_value, register,
)
from . import text as _text        # noqa: F401
from . import graph as _graph      # noqa: F401
from . import indicators as _ind   # noqa: F401

__all__ = ["RenderContext", "Widget", "WIDGET_TYPES", "build_widget",
           "format_value", "register"]
