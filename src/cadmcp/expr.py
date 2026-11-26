"""Safe arithmetic expressions for parametric specs.

Spec values may be numbers or expression strings referencing parameters, e.g.
``"L - 2*edge"``. Expressions map to SolidWorks equations / Creo relations, so the
grammar is deliberately small: + - * / ** unary, parentheses, and a few functions.
"""
from __future__ import annotations

import ast
import math
from collections.abc import Mapping

from .errors import SPEC_INVALID, CadError

_FUNCS = {
    "min": min, "max": max, "abs": abs, "round": round,
    "ceil": math.ceil, "floor": math.floor, "sqrt": math.sqrt,
    "sin": lambda d: math.sin(math.radians(d)),
    "cos": lambda d: math.cos(math.radians(d)),
    "tan": lambda d: math.tan(math.radians(d)),
}
_CONSTS = {"pi": math.pi}
_BINOPS = {
    ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
    ast.Pow: lambda a, b: a ** b, ast.Mod: lambda a, b: a % b,
}


def names_in(expr: str) -> set[str]:
    tree = _parse(expr)
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} - set(_FUNCS) - set(_CONSTS)


def _parse(expr: str) -> ast.Expression:
    try:
        return ast.parse(expr.strip(), mode="eval")
    except SyntaxError as e:
        raise CadError(SPEC_INVALID, f"bad expression {expr!r}: {e.msg}") from None


def evaluate(value: float | int | str, env: Mapping[str, float]) -> float:
    if isinstance(value, bool):
        raise CadError(SPEC_INVALID, "boolean is not a numeric value")
    if isinstance(value, (int, float)):
        return float(value)
    tree = _parse(value)

    def ev(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id in env:
                return float(env[node.id])
            if node.id in _CONSTS:
                return _CONSTS[node.id]
            raise CadError(SPEC_INVALID, f"unknown parameter {node.id!r} in {value!r}",
                           hint=f"defined parameters: {sorted(env)}")
        if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
            return _BINOPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            v = ev(node.operand)
            return -v if isinstance(node.op, ast.USub) else v
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
            return float(_FUNCS[node.func.id](*[ev(a) for a in node.args]))
        raise CadError(SPEC_INVALID, f"unsupported syntax in expression {value!r}")

    try:
        return ev(tree)
    except ZeroDivisionError:
        raise CadError(SPEC_INVALID, f"division by zero in {value!r}") from None


def resolve_parameters(params: Mapping[str, float | int | str]) -> dict[str, float]:
    """Resolve parameters that may reference each other; detects cycles."""
    resolved: dict[str, float] = {}
    visiting: set[str] = set()

    def res(name: str) -> float:
        if name in resolved:
            return resolved[name]
        if name not in params:
            raise CadError(SPEC_INVALID, f"unknown parameter {name!r}")
        if name in visiting:
            raise CadError(SPEC_INVALID, f"circular parameter reference involving {name!r}")
        visiting.add(name)
        v = params[name]
        deps = names_in(v) if isinstance(v, str) else set()
        env = {d: res(d) for d in deps if d in params}
        resolved[name] = evaluate(v, env)
        visiting.discard(name)
        return resolved[name]

    for n in params:
        res(n)
    return resolved
