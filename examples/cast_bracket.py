"""示例：灰铸铁轴承座底座（砂型铸造）。底面、顶面加工；大孔铸出，螺栓孔铸后钻。"""


def cast_base():
    return {
        "name": "CB1", "part_no": "CB-01", "title": "轴承座底座", "material": "HT200", "category": "casting",
        "surface_treatment": "非加工面涂防锈底漆",
        "base": {"type": "extrude", "profile": {"shape": "rect", "width": 240, "height": 140}, "thickness": 30},
        "features": [
            {"feature": "hole", "name": "Bore", "type": "simple", "size": 60, "tolerance": "H7", "positions": [[0, 0]]},
            {"feature": "hole", "name": "Bolt", "type": "clearance", "size": "M12",
             "positions": [[-95, -45], [95, -45], [95, 45], [-95, 45]]},
            {"feature": "pocket", "name": "Relief", "face": "bottom",
             "profile": {"shape": "rect", "width": 120, "height": 60, "center": [0, 0]}, "depth": 12},
        ],
        "datums": [{"name": "A", "target": "face:bottom"}],
        "tolerances": [{"type": "flatness", "value": 0.05, "target": "face:bottom"},
                       {"type": "perpendicularity", "value": 0.03, "target": "hole:Bore", "datums": ["A"]}],
        "roughness": [{"target": "face:bottom", "ra": 3.2}, {"target": "hole:Bore", "ra": 1.6}],
        "casting": {"process": "sand", "machined_faces": ["bottom", "top"]},
    }
