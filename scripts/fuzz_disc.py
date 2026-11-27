"""随机盘盖/法兰/齿轮（extrude + circle 底座）规格出图回归：统计线压字/字压字/崩溃。
用法：uv run python scripts/fuzz_disc.py <种子> <个数> [打印个数]；DUMP=n1,n2 打印指定样例规格。"""
import collections
import json
import os
import random
import sys
import traceback

from cadmcp.detailing import dim_line_overlaps, plan_part_drawing, view_overlaps
from cadmcp.errors import CadError
from cadmcp.ir import DrawingSpec, PartSpec
from cadmcp.ir.resolve import resolve_part
from cadmcp.render_svg import render

rng = random.Random(int(sys.argv[1]) if len(sys.argv) > 1 else 1)
stats = collections.Counter()
bad = []
KW = {20: (6, 2.8), 25: (8, 3.3), 30: (8, 3.3), 35: (10, 3.3), 40: (12, 3.3), 50: (14, 3.8)}  # 轮毂键槽 b、t2（GB/T 1095）
for n in range(int(sys.argv[2]) if len(sys.argv) > 2 else 200):
    gear = rng.random() < .35
    if gear:
        m, z = rng.choice([1.5, 2, 2.5, 3, 4]), rng.choice([20, 25, 30, 40, 50, 60])
        D = m * (z + 2)
        T = rng.choice([15, 20, 25, 30])
    else:
        D = rng.choice([60, 80, 100, 120, 160, 200, 250])
        T = rng.choice([8, 10, 15, 20, 30])
    feats = []
    bore = rng.choice([d for d in KW if d < 0.45 * D] or [20])
    feats.append({"feature": "hole", "name": "Bore", "type": "simple", "size": bore, "positions": [[0, 0]],
                  **({"tolerance": "H7"} if gear or rng.random() < .6 else {})})
    if gear or rng.random() < .4:
        b, t2 = KW[bore]
        feats.append({"feature": "pocket", "name": "KW", "face": "top", "depth": "through",
                      "profile": {"shape": "rect", "width": b, "height": round(bore / 2 + t2 - (bore / 2 - 2), 1),
                                  "center": [0, round((bore / 2 - 2 + bore / 2 + t2) / 2, 2)]}})
    pcd_max = D / 2 - 8
    pcd_min = bore / 2 + 10
    if pcd_max > pcd_min and rng.random() < .8:
        r = round(rng.uniform(pcd_min, pcd_max))
        t = rng.choice(["clearance", "counterbore", "tapped", "simple"])
        feats.append({"feature": "hole", "name": "PC", "type": t,
                      "size": rng.choice([10, 12, 16]) if t == "simple" else rng.choice(["M5", "M6", "M8"]),
                      "pattern": {"kind": "circular", "radius": r, "count": rng.choice([3, 4, 6, 8]),
                                  "start_angle": rng.choice([0, 22.5, 30, 45])},
                      **({"depth": min(T, 12)} if t == "tapped" and rng.random() < .5 else {})})
    if not gear and T >= 15 and rng.random() < .3:  # 止口凸台
        feats.append({"feature": "boss", "name": "Spigot", "height": rng.choice([3, 5]),
                      "profile": {"shape": "circle", "diameter": round(max(bore + 16, 0.5 * D)), "center": [0, 0]}})
    if rng.random() < .4:
        feats.append({"feature": "chamfer", "name": "C1", "edges": rng.choice(["top_outer", "bottom_outer"]),
                      "size": rng.choice([0.5, 1, 2])})
    spec = {"name": f"D{n}", "part_no": f"D-{n}", "title": "齿轮" if gear else "端盖", "material": "45#",
            "base": {"type": "extrude", "profile": {"shape": "circle", "diameter": D}, "thickness": T},
            "features": feats}
    if gear:
        spec["gear"] = {"module": m, "teeth": z}
    r2 = rng.random()
    if r2 < .3:
        spec["auto_precision"] = True
    elif r2 < .5:
        spec["datums"] = [{"name": "A", "target": "hole:Bore"}]
        spec["tolerances"] = [{"type": "circular_runout", "value": 0.02, "target": "face:top", "datums": ["A"]}]
        spec["roughness"] = [{"target": "hole:Bore", "ra": 1.6}, {"target": "face:top", "ra": 3.2}]
    if os.environ.get("DUMP") and str(n) in os.environ["DUMP"].split(","):
        print("SPEC", n, json.dumps(spec, ensure_ascii=False))
    try:
        p = resolve_part(PartSpec.model_validate(spec))
        plan = plan_part_drawing(DrawingSpec(model=p.name), p)
        svg, out = render(plan, p)
        vo = view_overlaps(plan) + [("collinear", a, b) for a, b in dim_line_overlaps(plan)]
        if vo:
            stats["viewclash"] += 1
            bad.append((n, "viewclash", vo[:3], None, spec))
        elif out["crossings"] or out["collisions"]:
            stats["overlap"] += 1
            bad.append((n, "overlap", out["crossings"][:2], out["collisions"][:2], spec))
        else:
            stats["ok"] += 1
    except CadError as e:
        stats["caderror"] += 1
        stats[str(e)[:60]] += 0
    except Exception:  # noqa: BLE001
        stats["crash"] += 1
        bad.append((n, "crash", traceback.format_exc().splitlines()[-3:], None, spec))
print(stats)
for b in bad[:int(sys.argv[3]) if len(sys.argv) > 3 else 6]:
    print(b[0], b[1], b[2], b[3])
    print(json.dumps(b[4], ensure_ascii=False))
