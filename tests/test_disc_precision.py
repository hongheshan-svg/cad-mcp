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


PCD_CAP = {'name': 'D88', 'part_no': 'D-88', 'title': '端盖', 'material': '45#', 'base': {'type': 'extrude', 'profile': {'shape': 'circle', 'diameter': 80}, 'thickness': 20}, 'features': [{'feature': 'hole', 'name': 'Bore', 'type': 'simple', 'size': 30, 'positions': [[0, 0]], 'tolerance': 'H7'}, {'feature': 'pocket', 'name': 'KW', 'face': 'top', 'depth': 'through', 'profile': {'shape': 'rect', 'width': 8, 'height': 5.3, 'center': [0, 15.65]}}, {'feature': 'hole', 'name': 'PC', 'type': 'simple', 'size': 12, 'pattern': {'kind': 'circular', 'radius': 31, 'count': 3, 'start_angle': 22.5}}, {'feature': 'boss', 'name': 'Spigot', 'height': 3, 'profile': {'shape': 'circle', 'diameter': 46, 'center': [0, 0]}}], 'auto_precision': True}


def test_bore_ra_leader_in_section():
    """端视图挤（Ra 压分布圆）、剖视孔腔又放不下符号：孔壁 Ra 改在剖视图上用带箭头引线指孔壁，
    引线穿孔腔从上端面伸出，符号立在折线上，不压剖视标题；SW 用视图草图线画引线与箭头。"""
    p, plan = _plan(PCD_CAP)
    ra = next(d for d in plan.dims if d.ref == "ra:hole:Bore")
    assert ra.view == "front" and ra.extra.get("leader_pts") and ra.extra.get("leader_segs")
    (ax, ay), (bx, by), _ = ra.extra["leader_pts"]
    v = next(v_ for v_ in plan.views if v_.type == "front")
    assert by > v.origin_on_sheet[1] + v.size_mm[1] / 2 > ay  # 从上端面伸出
    _, out = render(plan, p)
    assert out["crossings"] == [] and out["collisions"] == [] and out["dash_crossings"] == []
    from cadmcp.adapters.solidworks.compile import compile_drawing
    ops = compile_drawing(plan, "D.SLDPRT")
    assert sum(o.op == "view_line" and o.args["ref"].startswith("ra:hole:Bore:leader") for o in ops) >= 2


NARROW_SECTION = {'name': 'D127', 'part_no': 'D-127', 'title': '端盖', 'material': '45#', 'base': {'type': 'extrude', 'profile': {'shape': 'circle', 'diameter': 60}, 'thickness': 15}, 'features': [{'feature': 'hole', 'name': 'Bore', 'type': 'simple', 'size': 20, 'positions': [[0, 0]], 'tolerance': 'H7'}, {'feature': 'hole', 'name': 'PC', 'type': 'simple', 'size': 10, 'pattern': {'kind': 'circular', 'radius': 21, 'count': 6, 'start_angle': 0}}], 'datums': [{'name': 'A', 'target': 'hole:Bore'}], 'tolerances': [{'type': 'circular_runout', 'value': 0.02, 'target': 'face:top', 'datums': ['A']}], 'roughness': [{'target': 'hole:Bore', 'ra': 1.6}, {'target': 'face:top', 'ra': 3.2}]}
STEP_CUT_KW = {'name': 'D87', 'part_no': 'D-87', 'title': '端盖', 'material': '45#', 'base': {'type': 'extrude', 'profile': {'shape': 'circle', 'diameter': 120}, 'thickness': 20}, 'features': [{'feature': 'hole', 'name': 'Bore', 'type': 'simple', 'size': 35, 'positions': [[0, 0]], 'tolerance': 'H7'}, {'feature': 'pocket', 'name': 'KW', 'face': 'top', 'depth': 'through', 'profile': {'shape': 'rect', 'width': 10, 'height': 5.3, 'center': [0, 18.15]}}, {'feature': 'hole', 'name': 'PC', 'type': 'counterbore', 'size': 'M6', 'pattern': {'kind': 'circular', 'radius': 51, 'count': 3, 'start_angle': 22.5}}, {'feature': 'chamfer', 'name': 'C1', 'edges': 'bottom_outer', 'size': 1}]}
PCD_ON_HOLE = {'name': 'D109', 'part_no': 'D-109', 'title': '端盖', 'material': '45#', 'base': {'type': 'extrude', 'profile': {'shape': 'circle', 'diameter': 80}, 'thickness': 30}, 'features': [{'feature': 'hole', 'name': 'Bore', 'type': 'simple', 'size': 25, 'positions': [[0, 0]], 'tolerance': 'H7'}, {'feature': 'pocket', 'name': 'KW', 'face': 'top', 'depth': 'through', 'profile': {'shape': 'rect', 'width': 8, 'height': 5.3, 'center': [0, 13.15]}}, {'feature': 'hole', 'name': 'PC', 'type': 'counterbore', 'size': 'M8', 'pattern': {'kind': 'circular', 'radius': 26, 'count': 6, 'start_angle': 22.5}}, {'feature': 'boss', 'name': 'Spigot', 'height': 3, 'profile': {'shape': 'circle', 'diameter': 41, 'center': [0, 0]}}]}
STEP_CUT_KW2 = {'name': 'D3', 'part_no': 'D-3', 'title': '端盖', 'material': '45#', 'base': {'type': 'extrude', 'profile': {'shape': 'circle', 'diameter': 250}, 'thickness': 15}, 'features': [{'feature': 'hole', 'name': 'Bore', 'type': 'simple', 'size': 35, 'positions': [[0, 0]], 'tolerance': 'H7'}, {'feature': 'pocket', 'name': 'KW', 'face': 'top', 'depth': 'through', 'profile': {'shape': 'rect', 'width': 10, 'height': 5.3, 'center': [0, 18.15]}}, {'feature': 'hole', 'name': 'PC', 'type': 'counterbore', 'size': 'M6', 'pattern': {'kind': 'circular', 'radius': 80, 'count': 3, 'start_angle': 30}}], 'auto_precision': True}


def test_disc_regressions_seed4_5():
    """窄剖面顶面 Ra 改引线；阶梯剖折角旁的键槽深度尺寸外移；分布圆标注落在两孔之间、被引线穿过时换备选。"""
    for spec in (NARROW_SECTION, STEP_CUT_KW, PCD_ON_HOLE, STEP_CUT_KW2):
        p, plan = _plan(spec)
        _, out = render(plan, p)
        assert out["crossings"] == [] and out["collisions"] == [], spec["name"]
    p, plan = _plan(NARROW_SECTION)
    assert next(d for d in plan.dims if d.ref == "ra:face:top").extra.get("leader_segs")
    p, plan = _plan(PCD_ON_HOLE)
    import math
    pcd = next(d for d in plan.dims if d.ref.endswith(".pcd"))
    h = next(h for h in p.holes if h.circular)
    lab = pcd.extra["label"]["anchor"]
    v = next(v_ for v_ in plan.views if v_.type == "top")
    from cadmcp.detailing import VIEW_AXES, _uv_sheet, vc
    ha, va = VIEW_AXES["top"]
    for i in h.instances:
        cx, cy = _uv_sheet(v, p.bbox, (vc(i.xyz, ha), vc(i.xyz, va)))
        assert math.dist(lab, (cx, cy)) > h.max_dia / 2 * v.scale  # 圆点不落在孔里


CENTER_CAP = {'name': 'RD1', 'part_no': 'RD-1', 'title': '端盖', 'material': '45#', 'base': {'type': 'extrude', 'profile': {'shape': 'circle', 'diameter': 120}, 'thickness': 15}, 'features': [{'feature': 'hole', 'name': 'Bore', 'type': 'simple', 'size': 30, 'positions': [[0, 0]], 'tolerance': 'H7'}, {'feature': 'hole', 'name': 'Pin', 'type': 'simple', 'size': 8, 'positions': [[40, 0], [-40, 0]], 'tolerance': 'H7'}, {'feature': 'hole', 'name': 'T1', 'type': 'tapped', 'size': 'M6', 'positions': [[0, 45]], 'depth': 10}]}
CENTER_KW = {'name': 'D20', 'part_no': 'D-20', 'title': '端盖', 'material': '45#', 'base': {'type': 'extrude', 'profile': {'shape': 'circle', 'diameter': 60}, 'thickness': 10}, 'features': [{'feature': 'hole', 'name': 'Bore', 'type': 'simple', 'size': 25, 'positions': [[0, 0]], 'tolerance': 'H7'}, {'feature': 'pocket', 'name': 'KW', 'face': 'top', 'depth': 'through', 'profile': {'shape': 'rect', 'width': 8, 'height': 5.3, 'center': [0, 13.15]}}, {'feature': 'hole', 'name': 'T1', 'type': 'tapped', 'size': 'M6', 'positions': [[0, -22]], 'depth': 8}, {'feature': 'chamfer', 'name': 'C1', 'edges': 'top_outer', 'size': 0.5}]}


def test_disc_hole_positions_from_center():
    """圆盘：孔位从中心线标（对称销孔注中心距 80、单侧螺孔注到中心线 45），不用包围盒角点坐标；外径注 Φ120。"""
    p, plan = _plan(CENTER_CAP)
    assert not [d for d in plan.dims if d.view == "top" and d.kind == "ordinate"]
    cp = {d.text: d for d in plan.dims if (d.extra or {}).get("cpos")}
    assert set(cp) == {"80", "45"}
    assert cp["80"].extra["orient"] == "h" and cp["45"].extra["orient"] == "v"
    assert next(d for d in plan.dims if d.ref == "overall_x").text == "Φ120"
    assert not any(d.ref == "overall_y" for d in plan.dims)
    for spec in (CENTER_CAP, CENTER_KW):
        p, plan = _plan(spec)
        _, out = render(plan, p)
        assert out["crossings"] == [] and out["collisions"] == []
    # 有轮毂键槽：孔位尺寸列排在键槽深度尺寸线外侧
    p, plan = _plan(CENTER_KW)
    kx = next(d for d in plan.dims if d.ref == "KW_t").extra["sheet_pos"][0]
    assert next(d for d in plan.dims if (d.extra or {}).get("cpos")).extra["sheet_pos"][0] > kx
