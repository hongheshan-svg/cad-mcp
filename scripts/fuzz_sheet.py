"""随机钣金件（矩形底板 + 0–4 边翻边 + 底板孔）出图回归。用法：uv run python scripts/fuzz_sheet.py <种子> <个数>。"""
import collections
import json
import random
import sys
import traceback

from cadmcp.detailing import plan_part_drawing
from cadmcp.errors import CadError
from cadmcp.ir import DrawingSpec, PartSpec
from cadmcp.ir.resolve import resolve_part
from cadmcp.render_svg import render

rng = random.Random(int(sys.argv[1]))
N = int(sys.argv[2])
stats = collections.Counter()
bad = []
errs = collections.Counter()
for n in range(N):
    W, H, T = rng.choice([60, 80, 100, 150, 200, 300]), rng.choice([40, 60, 80, 120, 200]), rng.choice([1, 1.5, 2, 3])
    edges = rng.sample(["front", "back", "left", "right"], rng.randint(0, 4))
    flanges = [{"edge": e, "length": rng.choice([10, 15, 20, 30, 40])} for e in edges]
    feats = []
    if rng.random() < .7:
        m = 0.25 * min(W, H)
        pos = rng.choice([[[0, 0]], [[-round(W / 2 - m), 0], [round(W / 2 - m), 0]],
                          [[sx * round(W / 2 - m), sy * round(H / 2 - m)] for sx in (-1, 1) for sy in (-1, 1)]])
        feats.append({"feature": "hole", "name": "H", "type": rng.choice(["clearance", "simple"]),
                      "size": rng.choice(["M4", "M5", "M6"]) if rng.random() < .6 else rng.choice([5, 8, 10]),
                      "positions": pos})
    spec = {"name": f"SM{n}", "part_no": f"SM-{n}", "title": "测试钣金", "material": "SPCC", "category": "sheet_metal",
            "base": {"type": "sheet_metal", "profile": {"shape": "rect", "width": W, "height": H}, "thickness": T,
                     "flanges": flanges}, "features": feats}
    for f in feats:  # simple 孔用数值直径，clearance 用螺纹规格
        if f["type"] == "simple" and isinstance(f["size"], str):
            f["size"] = 6
        if f["type"] == "clearance" and not isinstance(f["size"], str):
            f["size"] = "M5"
    try:
        p = resolve_part(PartSpec.model_validate(spec))
        plan = plan_part_drawing(DrawingSpec(model=p.name), p)
        svg, out = render(plan, p)
        if out["crossings"] or out["collisions"]:
            stats["overlap"] += 1
            bad.append((n, "overlap", out["crossings"][:3], out["collisions"][:3], spec))
        else:
            stats["ok"] += 1
    except CadError as e:
        stats["caderror"] += 1
        errs[str(e)[:80]] += 1
    except Exception:
        stats["crash"] += 1
        bad.append((n, "crash", traceback.format_exc().splitlines()[-3:], None, spec))
print(stats)
print(errs.most_common(5))
for b in bad[:12]:
    print(b[0], b[1], b[2], b[3])
    print(json.dumps(b[4], ensure_ascii=False))
