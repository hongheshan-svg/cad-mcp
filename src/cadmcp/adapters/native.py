"""原生操作计划：IR → 一串与 CAD API 一一对应的操作（纯数据，可在任何平台测试）。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..expr import names_in


@dataclass
class NativeOp:
    op: str
    args: dict[str, Any] = field(default_factory=dict)
    why: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"op": self.op, "args": self.args, **({"why": self.why} if self.why else {})}


def to_sw_equation(expr: str | float) -> str:
    """'L - 2*e' → '"L" - 2*"e"'（SW 方程式中全局变量需加引号）。"""
    if not isinstance(expr, str):
        return f"{expr:g}"
    names = names_in(expr)
    return re.sub(r"\b([A-Za-z_]\w*)\b", lambda m: f'"{m.group(1)}"' if m.group(1) in names else m.group(1), expr)
