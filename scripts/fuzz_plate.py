"""随机板件规格出图回归：统计线压字/字压字/崩溃。用法：uv run python scripts/fuzz_plate.py <种子> <个数>；DUMP=n1,n2 打印指定样例规格。"""
import random, sys, traceback, collections
from cadmcp.detailing import plan_part_drawing
from cadmcp.ir import DrawingSpec, PartSpec
from cadmcp.ir.resolve import resolve_part
from cadmcp.render_svg import render
from cadmcp.errors import CadError
rng = random.Random(int(sys.argv[1]) if len(sys.argv) > 1 else 1)
stats = collections.Counter(); bad = []
for n in range(int(sys.argv[2]) if len(sys.argv) > 2 else 200):
    W, H, T = rng.choice([60, 80, 100, 150, 200, 300]), rng.choice([40, 60, 80, 120, 200]), rng.choice([5, 8, 10, 15, 20, 30])
    feats = []
    for k in range(rng.randint(0, 4)):
        t = rng.choice(["simple", "clearance", "tapped", "counterbore", "countersink"])
        size = rng.choice(["M4", "M5", "M6", "M8", "M10"]) if t != "simple" else rng.choice([5, 8, 10, 12, 20])
        m = 0.25 * min(W, H)
        pat = rng.choice(["one", "grid", "two"])
        if pat == "one":
            pos = [[round(rng.uniform(-W / 2 + m, W / 2 - m)), round(rng.uniform(-H / 2 + m, H / 2 - m))]]
        elif pat == "two":
            pos = [[-round(W / 2 - m), 0], [round(W / 2 - m), 0]]
        else:
            pos = [[sx * round(W / 2 - m), sy * round(H / 2 - m)] for sx in (-1, 1) for sy in (-1, 1)]
        f = {"feature": "hole", "name": f"H{k}", "type": t, "size": size, "positions": pos}
        if t == "tapped":
            f["depth"] = min(T, 12) if rng.random() < .5 else "through"
        if t == "simple" and rng.random() < .3:
            f["tolerance"] = "H7"
        feats.append(f)
    if rng.random() < .4 and T >= 8:
        feats.append({"feature": "pocket", "name": "P", "face": "top", "depth": round(T / 2),
                      "profile": {"shape": "rect", "width": round(W / 4), "height": round(H / 4), "center": [0, 0],
                                  **({"corner_radius": 3} if rng.random() < .5 else {})}})
    boss = None
    if not any(f["feature"] == "pocket" for f in feats) and rng.random() < .3:  # 顶面凸台（与型腔二选一）
        bw = round(min(W, H) * rng.choice([.3, .4, .5]))
        boss = {"feature": "boss", "name": "B", "height": rng.choice([5, 10, 15]),
                "profile": {"shape": "circle", "diameter": bw, "center": [0, 0]} if rng.random() < .5 else
                {"shape": "rect", "width": bw, "height": round(bw * .8), "center": [0, 0],
                 **({"corner_radius": 3} if rng.random() < .5 else {})}}
        feats.append(boss)
    spec = {"name": f"F{n}", "part_no": f"F-{n}", "title": "测试板", "material": "Q235",
            "base": {"type": "extrude", "profile": {"shape": "rect", "width": W, "height": H}, "thickness": T},
            "features": feats}
    import os, json as _j
    if os.environ.get("DUMP") and str(n) in os.environ["DUMP"].split(","):
        print("SPEC", n, _j.dumps(spec, ensure_ascii=False))
    try:
        p = resolve_part(PartSpec.model_validate(spec))
        cs = [(i.uv, h.max_dia / 2) for h in p.holes for i in h.instances]
        if any(((a[0][0]-b[0][0])**2 + (a[0][1]-b[0][1])**2) ** .5 < a[1] + b[1] + 3 for k, a in enumerate(cs) for b in cs[k+1:]):
            stats["invalid"] += 1; continue
        if p.pockets and any(abs(u) < W / 8 + r + 3 and abs(v) < H / 8 + r + 3 for (u, v), r in cs):
            stats["invalid"] += 1; continue
        if boss is not None:  # 孔骑在凸台边上、同组孔一部分在凸台上：规格不合理
            from cadmcp import rules
            if any(f["rule"] == "R014" for f in rules.check_part(p)):
                stats["invalid"] += 1; continue
        if boss is not None:
            stats["boss"] += 1
        plan = plan_part_drawing(DrawingSpec(model=p.name), p)
        svg, out = render(plan, p)
        from cadmcp.detailing import view_overlaps
        vo = view_overlaps(plan)
        if vo:
            stats["viewclash"] += 1
            bad.append((n, "viewclash", vo[:3], None, locals().get("spec") or locals().get("asm")))
        elif out["crossings"] or out["collisions"]:
            stats["overlap"] += 1; bad.append((n, "overlap", out["crossings"][:2], out["collisions"][:2], spec))
        else:
            stats["ok"] += 1
    except CadError as e:
        stats["caderror"] += 1
    except Exception as e:
        stats["crash"] += 1; bad.append((n, "crash", traceback.format_exc().splitlines()[-3:], None, spec))
print(stats)
for b in bad[:6]:
    import json; print(b[0], b[1], b[2], b[3]); print(json.dumps(b[4], ensure_ascii=False))
