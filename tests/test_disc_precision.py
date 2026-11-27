"""圆盘类（拉伸圆盘 + 同心配合孔）精度与齿轮端视图腹板孔标注。"""
import json

from cadmcp.detailing import plan_part_drawing
from cadmcp.ir import DrawingSpec, PartSpec
from cadmcp.ir.resolve import resolve_part
from cadmcp.render_svg import render

CAP = {"name": "D80", "part_no": "D-80", "title": "端盖", "material": "45#", "base": {"type": "extrude", "profile": {"shape": "circle", "diameter": 100}, "thickness": 20}, "features": [{"feature": "hole", "name": "Bore", "type": "simple", "size": 30, "positions": [[0, 0]], "tolerance": "H7"}, {"feature": "pocket", "name": "KW", "face": "top", "depth": "through", "profile": {"shape": "rect", "width": 8, "height": 5.3, "center": [0, 15.65]}}, {"feature": "hole", "name": "PC", "type": "tapped", "size": "M5", "pattern": {"kind": "circular", "radius": 33, "count": 4, "start_angle": 45}}, {"feature": "boss", "name": "Spigot", "height": 5, "profile": {"shape": "circle", "diameter": 50, "center": [0, 0]}}], "auto_precision": True}
GEAR = {"name": "D43", "part_no": "D-43", "title": "齿轮", "material": "45#", "base": {"type": "extrude", "profile": {"shape": "circle", "diameter": 186}, "thickness": 25}, "features": [{"feature": "hole", "name": "Bore", "type": "simple", "size": 25, "positions": [[0, 0]], "tolerance": "H7"}, {"feature": "pocket", "name": "KW", "face": "top", "depth": "through", "profile": {"shape": "rect", "width": 8, "height": 5.3, "center": [0, 13.15]}}, {"feature": "hole", "name": "PC", "type": "counterbore", "size": "M6", "pattern": {"kind": "circular", "radius": 39, "count": 3, "start_angle": 30}}], "gear": {"module": 3, "teeth": 60}}


def _plan(spec):
    p = resolve_part(PartSpec.model_validate(spec))
    return p, plan_part_drawing(DrawingSpec(model=p.name), p)


def test_disc_bore_is_datum_and_seat_face_runout():
    """端盖：中心配合孔为基准 A（不是板类的底面/侧面 A|B|C），带止口的一面注对 A 的端面圆跳动，
    跳动箭头指在止口外一圈的底座顶面上（不指凸台顶面高度的空处）。"""
    p, plan = _plan(CAP)
    assert [(d["name"], d["target"]) for d in p.datums] == [("A", "hole:Bore")]
    assert [(g["type"], g["target"], g["datums"]) for g in p.geotols] == [("circular_runout", "face:top", ["A"])]
    g = next(d for d in plan.dims if d.kind == "gtol")
    base_top = p.bosses[0]["z0"]
    assert g.at[1] == base_top and g.at[1] < p.bbox.zmax
    _, out = render(plan, p)
    assert out["crossings"] == [] and out["collisions"] == [] and out["dash_crossings"] == []


def test_gear_web_holes_called_out_with_pcd():
    """齿轮腹板沉孔：端视图有引出标注（n×孔 均布）与分布圆标注，引线不沿中心线；沉孔画两圆；小轴孔 Ra 标在孔壁延长线上。"""
    p, plan = _plan(GEAR)
    co = {d.ref: d for d in plan.dims if d.kind == "callout"}
    assert co["PC"].extra["label"] and co["PC.pcd"].extra["label"]["dot"]
    lb = co["PC"].extra["label"]
    dx, dy = lb["elbow"][0] - lb["anchor"][0], lb["elbow"][1] - lb["anchor"][1]
    assert abs(dx) > 1 and abs(dy) > 1  # 斜引出
    ra = next(d for d in plan.dims if d.ref == "ra:hole:Bore")
    assert ra.extra.get("ext_pts") and ra.extra["sheet_at"][0] > plan.views[0].origin_on_sheet[0]
    svg, out = render(plan, p)
    assert "3×Φ6.6" in svg and "Φ78 分布圆" in svg
    assert out["crossings"] == [] and out["collisions"] == [] and out["dash_crossings"] == []


def test_disc_hub_keyway_dims():
    """圆盘中心孔上的键槽：按轮毂键槽注 b（JS9）与 d + t2（GB/T 1095），不再注型腔 "宽×高" 与型腔角点坐标。"""
    p, plan = _plan(CAP)
    kw = {d.ref: d for d in plan.dims if (d.extra or {}).get("hubkw")}
    assert kw["KW_w"].text.startswith("8JS9") and kw["KW_t"].text.startswith("33.3(+0.2/0)")
    assert not any(d.kind == "callout" and d.ref == "KW" for d in plan.dims)
    assert not any(d.kind == "ordinate" and d.text in ("46", "63") for d in plan.dims)
    from cadmcp.adapters.solidworks.compile import compile_drawing
    ops = compile_drawing(plan, "D.SLDPRT")
    assert {o.args["ref"] for o in ops if o.op == "overall_dimension"} >= {"KW_w", "KW_t"}


SMALL_CAP = {'name': 'D18', 'part_no': 'D-18', 'title': '端盖', 'material': '45#', 'base': {'type': 'extrude', 'profile': {'shape': 'circle', 'diameter': 60}, 'thickness': 20}, 'features': [{'feature': 'hole', 'name': 'Bore', 'type': 'simple', 'size': 25, 'positions': [[0, 0]], 'tolerance': 'H7'}, {'feature': 'pocket', 'name': 'KW', 'face': 'top', 'depth': 'through', 'profile': {'shape': 'rect', 'width': 8, 'height': 5.3, 'center': [0, 13.15]}}, {'feature': 'boss', 'name': 'Spigot', 'height': 5, 'profile': {'shape': 'circle', 'diameter': 41, 'center': [0, 0]}}, {'feature': 'chamfer', 'name': 'C1', 'edges': 'bottom_outer', 'size': 0.5}]}
CROWDED_CAP = {'name': 'D51', 'part_no': 'D-51', 'title': '端盖', 'material': '45#', 'base': {'type': 'extrude', 'profile': {'shape': 'circle', 'diameter': 80}, 'thickness': 15}, 'features': [{'feature': 'hole', 'name': 'Bore', 'type': 'simple', 'size': 20, 'positions': [[0, 0]], 'tolerance': 'H7'}, {'feature': 'pocket', 'name': 'KW', 'face': 'top', 'depth': 'through', 'profile': {'shape': 'rect', 'width': 6, 'height': 4.8, 'center': [0, 10.4]}}, {'feature': 'hole', 'name': 'PC', 'type': 'tapped', 'size': 'M6', 'pattern': {'kind': 'circular', 'radius': 29, 'count': 6, 'start_angle': 45}, 'depth': 12}, {'feature': 'boss', 'name': 'Spigot', 'height': 5, 'profile': {'shape': 'circle', 'diameter': 40, 'center': [0, 0]}}], 'datums': [{'name': 'A', 'target': 'hole:Bore'}], 'tolerances': [{'type': 'circular_runout', 'value': 0.02, 'target': 'face:top', 'datums': ['A']}], 'roughness': [{'target': 'hole:Bore', 'ra': 1.6}, {'target': 'face:top', 'ra': 3.2}]}


def test_small_disc_keyway_callouts_retry_clean():
    """小端盖 / 挤的端盖：键槽尺寸占了视图上方，引出标注按出图检查换备选位置，不压凸台圆、孔圆。"""
    for spec in (SMALL_CAP, CROWDED_CAP):
        p, plan = _plan(spec)
        assert not any("_alts" in (d.extra or {}) for d in plan.dims)
        _, out = render(plan, p)
        assert out["crossings"] == [] and out["collisions"] == []


HOLEY_DISC = {'name': 'D89', 'part_no': 'D-89', 'title': '端盖', 'material': '45#', 'base': {'type': 'extrude', 'profile': {'shape': 'circle', 'diameter': 80}, 'thickness': 15}, 'features': [{'feature': 'hole', 'name': 'Bore', 'type': 'simple', 'size': 35, 'positions': [[0, 0]], 'tolerance': 'H7'}, {'feature': 'pocket', 'name': 'KW', 'face': 'top', 'depth': 'through', 'profile': {'shape': 'rect', 'width': 10, 'height': 5.3, 'center': [0, 18.15]}}, {'feature': 'hole', 'name': 'PC', 'type': 'simple', 'size': 16, 'pattern': {'kind': 'circular', 'radius': 32, 'count': 6, 'start_angle': 22.5}}], 'auto_precision': True}
TITLED_PLATE = {'name': 'F182', 'part_no': 'F-182', 'title': '测试板', 'material': 'Q235', 'base': {'type': 'extrude', 'profile': {'shape': 'rect', 'width': 60, 'height': 120}, 'thickness': 20}, 'features': [{'feature': 'hole', 'name': 'H0', 'type': 'countersink', 'size': 'M8', 'positions': [[-15, -45], [-15, 45], [15, -45], [15, 45]]}], 'datums': [{'name': 'A', 'target': 'face:bottom'}], 'tolerances': [{'type': 'flatness', 'value': 0.02, 'target': 'face:top', 'datums': []}], 'roughness': [{'target': 'face:top', 'ra': 1.6}]}


def test_face_symbols_off_corner_and_section_title():
    """端面符号：分布孔投影盖满整条边时只按剖切面切到的孔避让，不落在外角（总体尺寸界线）上；
    剖视图顶面的 Ra / 框格不放在标题 "A-A" 正下方，同一边上的两个符号不叠在一点。"""
    for spec in (HOLEY_DISC, TITLED_PLATE):
        p, plan = _plan(spec)
        _, out = render(plan, p)
        assert out["crossings"] == [] and out["collisions"] == []
        v = next(v_ for v_ in plan.views if v_.type == "front")
        x0, x1 = v.origin_on_sheet[0] - v.size_mm[0] / 2, v.origin_on_sheet[0] + v.size_mm[0] / 2
        sym = [d for d in plan.dims if d.view == "front" and d.kind in ("gtol", "roughness", "datum")]
        for d in sym:
            assert x0 + 1.5 < d.extra["sheet_at"][0] < x1 - 1.5, d
        for i, a_ in enumerate(sym):
            for b_ in sym[i + 1:]:
                if a_.extra["normal"] == b_.extra["normal"]:
                    assert abs(a_.extra["sheet_at"][0] - b_.extra["sheet_at"][0]) >= 6
    p, plan = _plan(TITLED_PLATE)
    cx = next(v_ for v_ in plan.views if v_.type == "front").origin_on_sheet[0]
    assert all(abs(d.extra["sheet_at"][0] - cx) > 8 for d in plan.dims
               if d.view == "front" and tuple(d.extra.get("normal", ())) == (0, 1))
