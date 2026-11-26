"""示例：单工位气缸推料机构（底板 + 2 根 4040 立柱 + 气缸安装板 + SC32 气缸）。
也是测试夹具。AI 实际使用时会生成同样结构的 JSON spec。"""

BASE_PLATE = {
    "name": "CS01-001_BasePlate", "part_no": "CS01-001", "title": "底板", "material": "Q235",
    "surface_treatment": "发黑",
    "parameters": {"L": 300, "W": 200, "T": 15, "e": 20, "pitch": 160},
    "base": {"type": "extrude", "profile": {"shape": "rect", "width": "L", "height": "W", "corner_radius": 5},
             "thickness": "T"},
    "features": [
        {"feature": "hole", "name": "Fix", "type": "counterbore", "size": "M8",
         "pattern": {"kind": "grid", "origin": ["-L/2+e", "-W/2+e"], "nx": 2, "ny": 2, "dx": "L-2*e", "dy": "W-2*e"}},
        {"feature": "hole", "name": "Post", "type": "tapped", "size": "M8", "depth": "T", "thread_depth": 12,
         "positions": [["-pitch/2", 0], ["pitch/2", 0]]},
        {"feature": "chamfer", "name": "C1", "edges": "top_outer", "size": 1},
    ],
    "references": [
        {"name": "CS_PostL", "origin": ["-pitch/2", 0, "T"]},
        {"name": "CS_PostR", "origin": ["pitch/2", 0, "T"]},
    ],
}

# 注："depth": "T" 会触发 R004（盲孔深度=料厚），测试里用它验证规则；正式示例改为 through
BASE_PLATE["features"][1]["depth"] = "through"
BASE_PLATE["features"][1].pop("thread_depth")

MOUNT_PLATE = {
    "name": "CS01-002_CylMountPlate", "part_no": "CS01-002", "title": "气缸安装板", "material": "AL6061-T6",
    "surface_treatment": "阳极氧化黑",
    "parameters": {"L": 200, "W": 80, "T": 12, "pitch": 160},
    "base": {"type": "extrude", "profile": {"shape": "rect", "width": "L", "height": "W"}, "thickness": "T"},
    "features": [
        {"feature": "hole", "name": "Post", "type": "counterbore", "size": "M8",
         "positions": [["-pitch/2", 0], ["pitch/2", 0]]},
        # 气缸安装接口：按库登记的法兰孔位自动开 4×M6 过孔 + Φ26 活塞杆让位孔，并生成 CS_Cyl
        {"feature": "mount", "name": "Cyl", "item": "CYL-SC32x50"},
    ],
    "references": [
        {"name": "CS_PostL", "origin": ["-pitch/2", 0, 0]},
    ],
}

ASSEMBLY = {
    "name": "CS01-000_CylStation", "part_no": "CS01-000", "title": "气缸推料工位",
    "parameters": {"H": 250},
    "components": [
        {"id": "base", "source": "part:CS01-001_BasePlate"},
        {"id": "postL", "source": "lib:PRF-4040?L=250", "place": {"align": "CS_End0", "to": "base.CS_PostL"}},
        {"id": "postR", "source": "lib:PRF-4040?L=250", "place": {"align": "CS_End0", "to": "base.CS_PostR"}},
        {"id": "mount", "source": "part:CS01-002_CylMountPlate", "place": {"align": "CS_PostL", "to": "postL.CS_End1"}},
        {"id": "cyl", "source": "lib:CYL-SC32x50", "place": {"align": "CS_Mount", "to": "mount.CS_Cyl"}},
        {"id": "screw", "source": "lib:SHCS-M6x20", "place": {"align": "CS_Head", "to": "mount.CS_Cyl", "offset": [12, 12, 0]},
         "array": {"count": 2, "step": [-24, 0, 0]}},
    ],
}
