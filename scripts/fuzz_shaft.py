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
    if os.environ.get("EXTRA2"):  # 第二组扩展（独立随机流）：齿轮段、轴肩圆角、法兰孔、螺孔/竖直/配作横孔、孔内键槽、右端孔、半段螺纹
        r3 = random.Random(int(sys.argv[1]) * 104729 + n)
        if r3.random() < .3:
            base["shoulder_fillet"] = r3.choice([0.5, 1, 1.5])
        free = [i for i in range(k) if not any(f["step"] == i for f in feats)]
        if free and r3.random() < .25:  # 齿轮段：齿顶圆 = m(z+2)，取接近该段直径的齿数
            i = r3.choice(free); m = r3.choice([1.5, 2, 2.5, 3])
            z = max(12, round(steps[i]["diameter"] / m) - 2)
            steps[i]["diameter"] = round(m * (z + 2), 3); steps[i].pop("tolerance", None)
            steps[i]["length"] = max(steps[i]["length"], 20)
            feats.append({"feature": "gear", "name": f"Z{i}", "step": i, "module": m, "teeth": z})
            free.remove(i)
        if r3.random() < .25:
            i = r3.randrange(k)
            h = {"step": i}
            kind = r3.choice(["tap", "vertical", "asm"])
            if kind == "tap":
                h["size"] = r3.choice(["M5", "M6", "M8"]); h["depth"] = 10
            else:
                h["diameter"] = r3.choice([4, 5, 6]); h["tolerance"] = "H7"
                if kind == "vertical":
                    h["direction"] = "vertical"
                else:
                    h["at_assembly"] = True
            if steps[i]["diameter"] >= 20 and not any(f["feature"] == "gear" and f["step"] == i for f in feats):
                base["cross_holes"] = [h]
        if not base.get("bores") and r3.random() < .25:
            dmin = min(st["diameter"] for st in steps)
            bd = round(dmin * r3.choice([.4, .5]))
            if bd >= 10:
                b = {"diameter": bd, "side": r3.choice(["start", "end"])}
                if r3.random() < .4:
                    b["keyway"] = True; b["tolerance"] = "H7"
                elif r3.random() < .5:
                    b["length"] = round(sum(st["length"] for st in steps) * .3)
                base["bores"] = [b]
                base.pop("center_holes", None)
                Dm = max(st["diameter"] for st in steps)
                imax = [st["diameter"] for st in steps].index(Dm)
                if r3.random() < .3 and Dm - bd > 24:
                    base["flange_holes"] = [{"step": imax, "count": r3.choice([4, 6]), "pcd": round((Dm + bd) / 2),
                                             "diameter": r3.choice([6.6, 9])}]
        for f in feats:
            if f["feature"] == "thread" and r3.random() < .4 and steps[f["step"]]["length"] >= 20:
                f["length"] = round(steps[f["step"]]["length"] * .6)
            if f["feature"] == "keyway" and r3.random() < .3:
                f["offset"] = 2
    try:
        p = resolve_part(PartSpec.model_validate(spec))
        from cadmcp import rules
        if any(f["rule"] == "S011" for f in rules.check_part(p)):  # 同径同公差相邻段：应合并，不算排布样例
            stats["invalid"] += 1; continue
        plan = plan_part_drawing(DrawingSpec(model=p.name), p)
        svg, out = render(plan, p)
        from cadmcp.detailing import view_overlaps, dim_line_overlaps
        vo = view_overlaps(plan) + [("collinear", a, b) for a, b in dim_line_overlaps(plan)]
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
