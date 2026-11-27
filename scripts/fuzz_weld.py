"""随机方管焊接框架（4 立柱 + 1–2 层水平框 + 可选地脚板/顶板/筋板孔）出图回归。
用法：uv run python scripts/fuzz_weld.py <种子> <个数>。"""
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


def frame(z, W, D, S, sec, tag):
    cx, cy = W / 2 - S / 2, D / 2 - S / 2
    return [
        {"name": f"BXF{tag}", "section": sec, "start": [-cx + S / 2, -cy, z], "end": [cx - S / 2, -cy, z]},
        {"name": f"BXB{tag}", "section": sec, "start": [-cx + S / 2, cy, z], "end": [cx - S / 2, cy, z]},
        {"name": f"BYL{tag}", "section": sec, "start": [-cx, -cy + S / 2, z], "end": [-cx, cy - S / 2, z]},
        {"name": f"BYR{tag}", "section": sec, "start": [cx, -cy + S / 2, z], "end": [cx, cy - S / 2, z]},
    ]


for n in range(N):
    S, sec = rng.choice([(30, "SHS30x2.5"), (40, "SHS40x3"), (50, "SHS50x4"), (60, "SHS60x4")])
    W, D, H = rng.choice([300, 400, 600, 800, 1000]), rng.choice([200, 300, 400, 600]), rng.choice([200, 300, 500, 800])
    T = rng.choice([6, 8, 10, 12])
    feet, top = rng.random() < .6, rng.random() < .6
    z0, z1 = (T if feet else 0), (H - T if top else H)
    cx, cy = W / 2 - S / 2, D / 2 - S / 2
    members = [{"name": f"P{i + 1}", "section": sec, "start": [sx * cx, sy * cy, z0], "end": [sx * cx, sy * cy, z1]}
               for i, (sx, sy) in enumerate([(-1, -1), (1, -1), (1, 1), (-1, 1)])]
    members += frame(z1 - S / 2, W, D, S, sec, "T")
    if rng.random() < .6 and H >= 300:
        members += frame(z0 + rng.choice([80, 100, 150]), W, D, S, sec, "L")
    plates = []
    if feet:
        fs = S + 2 * rng.choice([20, 30])
        plates += [{"name": f"FOOT{i + 1}", "size": [fs, fs], "thickness": T, "center": [sx * cx, sy * cy, T / 2],
                    "holes": [{"name": "H", "type": "simple", "size": rng.choice([11, 14, 18]),
                               "positions": [[sx * (fs / 2 - 12), sy * (fs / 2 - 12)]]}]}
                   for i, (sx, sy) in enumerate([(-1, -1), (1, -1), (1, 1), (-1, 1)])]
    if top:
        hx, hy = round(W / 2 - rng.choice([40, 60, 100])), round(D / 2 - rng.choice([40, 60]))
        plates.append({"name": "TOP", "size": [W, D], "thickness": T, "center": [0, 0, H - T / 2],
                       "machined": rng.random() < .5,
                       "holes": [{"name": "M", "type": "tapped", "size": rng.choice(["M6", "M8", "M10"]),
                                  "positions": [[x, y] for x in (-hx, hx) for y in (-hy, hy)]}] if rng.random() < .7 else []})
    spec = {"name": f"WF{n}", "part_no": f"WF-{n}", "title": "测试焊接架", "material": "Q235", "category": "weldment",
            "base": {"type": "weldment", "members": members, "plates": plates, "end_caps": rng.random() < .5}}
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
