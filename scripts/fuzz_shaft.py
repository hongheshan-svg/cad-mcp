"""随机阶梯轴规格出图回归（直径中间大两端小、键槽/环槽/螺纹/中心孔/横孔）。用法：uv run python scripts/fuzz_shaft.py <种子> <个数>。"""
import random, sys, traceback, collections, json
from cadmcp.detailing import plan_part_drawing
from cadmcp.ir import DrawingSpec, PartSpec
from cadmcp.ir.resolve import resolve_part
from cadmcp.render_svg import render
from cadmcp.errors import CadError
rng = random.Random(int(sys.argv[1])); N = int(sys.argv[2])
stats = collections.Counter(); bad = []; errs = collections.Counter()
DIAS = [12, 15, 17, 20, 22, 25, 28, 30, 32, 35, 40, 45, 50, 55, 60]
for n in range(N):
    k = rng.randint(2, 6)
    peak = rng.randrange(k)
    top = rng.choice([30, 35, 40, 45, 50, 55, 60])
    ds = [0] * k
    ds[peak] = top
    for i in range(peak - 1, -1, -1):
        ds[i] = max(12, ds[i + 1] - rng.choice([3, 5, 5, 8, 10]))
    for i in range(peak + 1, k):
        ds[i] = max(12, ds[i - 1] - rng.choice([3, 5, 5, 8, 10]))
    steps = [{"length": rng.choice([10, 15, 20, 25, 30, 40, 50, 60, 80]), "diameter": d,
              **({"tolerance": rng.choice(["k6", "h7", "f7", "js6", "m6"])} if rng.random() < .4 else {})} for d in ds]
    feats = []
    for i, st in enumerate(steps):
        r = rng.random()
        if r < .25 and st["length"] >= 25 and st["diameter"] >= 17:
            L = max(x for x in [10, 12, 14, 16, 18, 20, 22, 25, 28, 32, 36, 40, 45, 50, 56, 63] if x <= 0.8 * st["length"] - 2)
            feats.append({"feature": "keyway", "name": f"K{i}", "step": i, "length": L})
        elif r < .35:
            feats.append({"feature": "groove", "name": f"G{i}", "type": rng.choice(["circlip", "undercut", "grinding"]), "step": i})
        elif r < .42 and st["diameter"] in (12, 16, 20, 24, 30) and i in (0, k - 1):
            feats.append({"feature": "thread", "name": f"T{i}", "step": i, "size": f"M{st['diameter']}"})
    base = {"type": "shaft", "chamfer": rng.choice([0, 0.5, 1, 1.5]), "steps": steps}
    if rng.random() < .3:
        base["center_holes"] = {"type": "B"}
    if rng.random() < .25:
        i = rng.randrange(k)
        base["cross_holes"] = [{"step": i, "diameter": rng.choice([4, 5, 6, 8])}]
    spec = {"name": f"S{n}", "part_no": f"S-{n}", "title": "测试轴", "material": "45#", "base": base, "features": feats}
    import os
    if os.environ.get("EXTRA"):  # 扩展覆盖（独立随机流，不影响默认种子）：轴端螺孔、内孔（空心轴/套）、自动精度
        r2 = random.Random(int(sys.argv[1]) * 7919 + n)
        if r2.random() < .4:
            dend = ds[-1] if r2.random() < .5 else ds[0]
            side = "end" if dend == ds[-1] else "start"
            m = max((x for x in (5, 6, 8, 10, 12, 16) if x <= dend * 0.4), default=None)
            if m and not any(f["feature"] == "thread" and f["step"] in (0, k - 1) for f in feats):
                base["end_holes"] = [{"side": side, "size": f"M{m}", "depth": round(1.5 * m)}]
        elif r2.random() < .35:
            dmin = min(ds)
            bd = round(dmin * r2.choice([.3, .4, .5]))
            if bd >= 6:
                base["bores"] = [{"diameter": bd, **({} if r2.random() < .5 else {"length": round(sum(st["length"] for st in steps) * .4)}),
                                  **({"tolerance": "H7"} if r2.random() < .4 else {})}]
                base.pop("center_holes", None)
        if r2.random() < .3:
            spec["auto_precision"] = True
    try:
        p = resolve_part(PartSpec.model_validate(spec))
        from cadmcp import rules
        if any(f["rule"] == "S011" for f in rules.check_part(p)):  # 同径同公差相邻段：应合并，不算排布样例
            stats["invalid"] += 1; continue
        plan = plan_part_drawing(DrawingSpec(model=p.name), p)
        svg, out = render(plan, p)
        from cadmcp.detailing import view_overlaps
        vo = view_overlaps(plan)
        if vo:
            stats["viewclash"] += 1
            bad.append((n, "viewclash", vo[:3], None, locals().get("spec") or locals().get("asm")))
        elif out["crossings"] or out["collisions"]:
            stats["overlap"] += 1; bad.append((n, "overlap", out["crossings"][:3], out["collisions"][:3], spec))
        else:
            stats["ok"] += 1
    except CadError as e:
        stats["caderror"] += 1; errs[str(e)[:70]] += 1
    except Exception:
        stats["crash"] += 1; bad.append((n, "crash", traceback.format_exc().splitlines()[-3:], None, spec))
print(stats); print(errs.most_common(5))
for b in bad[:int(sys.argv[3]) if len(sys.argv) > 3 else 12]:
    print(b[0], b[1], b[2], b[3]); print(json.dumps(b[4], ensure_ascii=False))
