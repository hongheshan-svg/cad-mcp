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
    # 铸造材料：cast_min_wall = 砂型铸造最小壁厚（mm，中小件经验值）
    "HT200": {"density": 7200, "desc": "灰铸铁 HT200", "cast_min_wall": 6},
    "HT250": {"density": 7250, "desc": "灰铸铁 HT250", "cast_min_wall": 8},
    "QT450-10": {"density": 7100, "desc": "球墨铸铁 QT450-10", "cast_min_wall": 6},
    "ZG270-500": {"density": 7850, "desc": "铸钢 ZG270-500", "cast_min_wall": 10},
    "ZL102": {"density": 2650, "desc": "铸造铝合金 ZAlSi12", "cast_min_wall": 4},
    "ZL104": {"density": 2630, "desc": "铸造铝合金 ZAlSi9Mg", "cast_min_wall": 4},
}


# 热处理经验硬度（中小截面；TODO(核对手册)）：process → 硬度；local_induction = 表面高频淬火 (硬度, 淬硬层深)
HEAT_TREATMENT = {
    "45#": {"quench_temper": "220~250HBW", "normalize": "170~217HBW", "local_induction": ("40~45HRC", "1~1.5")},
    "40Cr": {"quench_temper": "241~286HBW", "normalize": "179~229HBW", "local_induction": ("48~53HRC", "1~1.5")},
}
HEAT_PROCESS = {"quench_temper": "调质处理", "normalize": "正火处理"}


def density(name: str) -> float:
    if name not in MATERIALS:
        raise CadError(SPEC_INVALID, f"unknown material {name!r}", hint=f"available: {sorted(MATERIALS)}")
    return MATERIALS[name]["density"]
