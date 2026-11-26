"""示例：方管焊接底座（4 根立柱 + 上下框 + 地脚板 + 顶部安装板），用于测试与演示。"""
W, D, H = 600, 400, 500
S = 40  # 方管边长
T = 10  # 地脚板/安装板厚


def _frame(z):
    """一层水平框：前后梁沿 X 夹在立柱之间，左右梁沿 Y 夹在立柱之间。"""
    cx, cy = W / 2 - S / 2, D / 2 - S / 2
    return [
        {"name": f"BX_F{z:g}", "section": "SHS40x3", "start": [-cx + S / 2, -cy, z], "end": [cx - S / 2, -cy, z]},
        {"name": f"BX_B{z:g}", "section": "SHS40x3", "start": [-cx + S / 2, cy, z], "end": [cx - S / 2, cy, z]},
        {"name": f"BY_L{z:g}", "section": "SHS40x3", "start": [-cx, -cy + S / 2, z], "end": [-cx, cy - S / 2, z]},
        {"name": f"BY_R{z:g}", "section": "SHS40x3", "start": [cx, -cy + S / 2, z], "end": [cx, cy - S / 2, z]},
    ]


def weld_base():
    cx, cy = W / 2 - S / 2, D / 2 - S / 2
    posts = [{"name": f"P{i + 1}", "section": "SHS40x3", "start": [sx * cx, sy * cy, T], "end": [sx * cx, sy * cy, H - T]}
             for i, (sx, sy) in enumerate([(-1, -1), (1, -1), (1, 1), (-1, 1)])]
    # 上下框梁中心在立柱上，夹在立柱侧面之间：上框贴顶板
    members = posts + _frame(T + 100) + _frame(H - T - S / 2)
    feet = [{"name": f"FOOT{i + 1}", "size": [80, 80], "thickness": T, "center": [sx * cx, sy * cy, T / 2],
             "holes": [{"name": "H", "type": "simple", "size": 14, "positions": [[sx * 22, sy * 22]]}]}
            for i, (sx, sy) in enumerate([(-1, -1), (1, -1), (1, 1), (-1, 1)])]
    top = {"name": "TOP", "size": [W, D], "thickness": T, "center": [0, 0, H - T / 2], "machined": True,
           "holes": [{"name": "M8", "type": "tapped", "size": "M8", "positions": [[x, y] for x in (-200, 200) for y in (-120, 120)]}]}
    return {"name": "WB1", "part_no": "WB-01", "title": "焊接底座", "material": "Q235", "category": "weldment",
            "surface_treatment": "喷砂后喷漆（RAL7035）",
            "base": {"type": "weldment", "members": members, "plates": feet + [top]}}
