"""Material library (density in kg/m^3). Names follow common Chinese shop-floor grades."""
from __future__ import annotations

from .errors import SPEC_INVALID, CadError

MATERIALS: dict[str, dict] = {
    "Q235": {"density": 7850, "desc": "碳素结构钢"},
    "45#": {"density": 7850, "desc": "优质碳素钢 (C45)"},
    "SUS304": {"density": 7930, "desc": "不锈钢 304"},
    "AL6061-T6": {"density": 2700, "desc": "铝合金 6061-T6"},
    "AL6063-T5": {"density": 2700, "desc": "铝型材 6063-T5"},
    "AL7075-T6": {"density": 2810, "desc": "铝合金 7075-T6"},
    "POM": {"density": 1410, "desc": "聚甲醛"},
    "SPCC": {"density": 7850, "desc": "冷轧钣金"},
    "40Cr": {"density": 7850, "desc": "合金结构钢"},
}


def density(name: str) -> float:
    if name not in MATERIALS:
        raise CadError(SPEC_INVALID, f"unknown material {name!r}", hint=f"available: {sorted(MATERIALS)}")
    return MATERIALS[name]["density"]
