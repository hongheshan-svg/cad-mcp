"""随机装配（销座板 / 轴承座 + 螺钉）出图回归。用法：uv run python scripts/fuzz_asm.py <种子> <个数>。"""
import collections
import json
import random
import sys
import tempfile
import traceback

from cadmcp.detailing import plan_assembly_drawing
from cadmcp.errors import CadError
from cadmcp.gateway.service import CadService
from cadmcp.ir import AssemblySpec, DrawingSpec, PartSpec
from cadmcp.render_svg import render

rng = random.Random(int(sys.argv[1]))
N = int(sys.argv[2])
stats = collections.Counter()
bad = []
errs = collections.Counter()
BRG = {"BRG-6204": (20, 47, 14), "BRG-6205": (25, 52, 15), "BRG-6206": (30, 62, 16)}


def pin_family(n):
    W, H, T = rng.choice([80, 100, 150]), rng.choice([60, 80, 100]), rng.choice([15, 20, 25])
    k = rng.randint(1, 3)
    xs = [round(-W / 2 + W * (i + 1) / (k + 1)) for i in range(k)]
    parts, comps = [], []
    holes = []
    for i, x in enumerate(xs):
        d = rng.choice([10, 12, 16, 20])
        holes.append({"feature": "hole", "name": f"Pin{i}", "type": "simple", "size": d, "tolerance": "H7",
                      "positions": [[x, 0]]})
        pin = {"name": f"PN{n}_{i}", "part_no": f"PN-{n}-{i}", "title": "销", "material": "45#",
               "base": {"type": "shaft", "chamfer": 1, "steps": [{"length": T + rng.choice([10, 20]), "diameter": d,
                                                                 "tolerance": rng.choice(["k6", "m6", "h6"])},
                                                                {"length": rng.choice([5, 8]), "diameter": d + 6}]}}
        parts.append(pin)
        comps.append({"id": f"pin{i}", "source": f"part:{pin['name']}", "place": {"offset": [x, 0, -rng.choice([5, 10])]}})
    plate = {"name": f"PL{n}", "part_no": f"PL-{n}", "title": "座板", "material": "Q235",
             "base": {"type": "extrude", "profile": {"shape": "rect", "width": W, "height": H}, "thickness": T},
             "features": holes}
    return [plate] + parts, [{"id": "plate", "source": f"part:{plate['name']}"}] + comps


def brg_family(n):
    code = rng.choice(list(BRG))
    di, do, B = BRG[code]
    W, H, T = rng.choice([120, 150]), rng.choice([100, 120]), rng.choice([B, B + 5])
    m = rng.choice([15, 20])
    scr = rng.random() < .6
    feats = [{"feature": "hole", "name": "Seat", "type": "simple", "size": do, "tolerance": "H7", "positions": [[0, 0]]}]
    if scr:
        pos = [[sx * (W / 2 - m), sy * (H / 2 - m)] for sx in (-1, 1) for sy in (-1, 1)]
        feats.append({"feature": "hole", "name": "M6", "type": "tapped", "size": "M6", "depth": 12, "positions": pos})
    housing = {"name": f"BH{n}", "part_no": f"BH-{n}", "title": "轴承座板", "material": "HT200",
               "base": {"type": "extrude", "profile": {"shape": "rect", "width": W, "height": H}, "thickness": T},
               "features": feats}
    shaft = {"name": f"BS{n}", "part_no": f"BS-{n}", "title": "轴", "material": "45#",
             "base": {"type": "shaft", "chamfer": 1, "steps": [{"length": rng.choice([30, 40, 60]), "diameter": di, "tolerance": "k6"},
                                                             {"length": 10, "diameter": di + 7}]}}
    comps = [{"id": "shaft", "source": f"part:{shaft['name']}"},
             {"id": "brg", "source": f"lib:{code}", "place": {"offset": [0, 0, 5]}},
             {"id": "seat", "source": f"part:{housing['name']}", "place": {"offset": [0, 0, 5]}}]
    if scr:
        comps += [{"id": f"s{i}", "source": "lib:SHCS-M6x20", "place": {"offset": [p[0], p[1], 5 + T]}}
                  for i, p in enumerate(pos)]
    return [shaft, housing], comps


for n in range(N):
    parts, comps = (pin_family if rng.random() < .5 else brg_family)(n)
    asm = {"name": f"A{n}", "part_no": f"A-{n}", "title": "测试组件", "components": comps}
    try:
        svc = CadService.from_config(None, None, tempfile.mkdtemp())
        svc.change_begin("F")
        for d in parts:
            svc.build(PartSpec.model_validate(d))
        svc.build(AssemblySpec.model_validate(asm))
        a = svc._models()[asm["name"]].resolved
        plan = plan_assembly_drawing(DrawingSpec(model=asm["name"]), a)
        svg, out = render(plan, a)
        from cadmcp.detailing import view_overlaps
        vo = view_overlaps(plan)
        if vo:
            stats["viewclash"] += 1
            bad.append((n, "viewclash", vo[:3], None, locals().get("spec") or locals().get("asm")))
        elif out["crossings"] or out["collisions"]:
            stats["overlap"] += 1
            bad.append((n, "overlap", out["crossings"][:3], out["collisions"][:3], {"parts": parts, "asm": asm}))
        else:
            stats["ok"] += 1
    except CadError as e:
        stats["caderror"] += 1
        errs[str(e)[:80]] += 1
    except Exception:
        stats["crash"] += 1
        bad.append((n, "crash", traceback.format_exc().splitlines()[-3:], None, {"parts": parts, "asm": asm}))
print(stats)
print(errs.most_common(5))
for b in bad[:12]:
    print(b[0], b[1], b[2], b[3])
    print(json.dumps(b[4], ensure_ascii=False))
