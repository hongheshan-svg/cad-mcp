"""示例：电机安装板（止口孔 H7、螺纹盲孔、沉孔、底面让位槽）——主视图自动阶梯剖。"""
MP = {"name": "MP-001_MotorPlate", "part_no": "MP-001", "title": "电机安装板", "material": "AL6061-T6",
      "surface_treatment": "阳极氧化黑",
      "base": {"type": "extrude", "profile": {"shape": "rect", "width": 250, "height": 150, "corner_radius": 5}, "thickness": 12},
      "features": [
        {"feature": "hole", "name": "Pilot", "type": "simple", "size": 50, "tolerance": "H7", "positions": [[0, 0]]},
        {"feature": "hole", "name": "Motor", "type": "tapped", "size": "M5", "depth": 10, "thread_depth": 8,
         "pattern": {"kind": "circular", "radius": 35, "count": 4, "start_angle": 45}},
        {"feature": "hole", "name": "Fix", "type": "counterbore", "size": "M6",
         "positions": [[-110, -60], [110, -60], [110, 60], [-110, 60]]},
        {"feature": "pocket", "name": "Relief", "face": "bottom",
         "profile": {"shape": "rect", "width": 40, "height": 20, "center": [80, 0]}, "depth": 4}]}
