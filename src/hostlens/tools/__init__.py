"""Hostlens Tool Registry capability layer (M2).

Re-exports the five canonical names per spec §1.1:

    ToolSpec, ToolContext, ToolRegistry, register_default_tools, tool
"""

from hostlens.tools.base import ToolContext, ToolSpec
from hostlens.tools.decorators import tool
from hostlens.tools.default_tools import register_default_tools
from hostlens.tools.registry import ToolRegistry

__all__ = [
    "ToolContext",
    "ToolRegistry",
    "ToolSpec",
    "register_default_tools",
    "tool",
]
