"""SolidWorks 执行器：逐条执行 NativeOp（pywin32 COM）。

⚠ 本文件在 macOS 上编写，尚未在 SolidWorks 实机运行。设计成"每步可诊断"：
  - 每个 op 记录 ok/失败/耗时/异常，写入 <out_dir>/<name>.oplog.json
  - 关键 op 失败即停止并报告；非关键 op（装饰螺纹、倒角等）失败记为警告继续
  - 枚举常量按名字从 SW 常量类型库读取，不硬编码数值
  - 不依赖英文基准面名称（中文版为"前视基准面"），按特征树顺序取前三个基准面
单位：计划中为 mm/deg，这里换算为 SW API 的 m/rad。
"""
from __future__ import annotations

import json
import math
import time
import traceback
from pathlib import Path
from typing import Any

from ..native import NativeOp

MM = 0.001
NON_CRITICAL = {"cosmetic_thread", "fillet", "chamfer", "select_edges", "dimension_profile", "ref_plane_coincident",
                "ref_axis", "ref_point", "auto_balloon", "readback_annotations", "export", "interference_check",
                "set_material", "leader_note", "overall_dimension", "sketch_fillet_all_corners",
                "section_view", "shaft_dimension", "insert_weldment", "weldment_cut_list", "count_dangling",
                "bom_balloons"}


class OpFailed(Exception):
    pass


def load_constants(com_module_hint: str = "SOLIDWORKS") -> Any:
    """在注册表中找 'SOLIDWORKS <ver> Constant type library' 并生成 Python 绑定，返回常量命名空间。"""
    import winreg  # type: ignore[import-not-found]

    from win32com.client import gencache  # type: ignore[import-not-found]

    best = None
    with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "TypeLib") as root:
        for i in range(winreg.QueryInfoKey(root)[0]):
            guid = winreg.EnumKey(root, i)
            try:
                with winreg.OpenKey(root, guid) as k:
                    for j in range(winreg.QueryInfoKey(k)[0]):
                        ver = winreg.EnumKey(k, j)
                        desc = winreg.QueryValue(k, ver)
                        if com_module_hint.lower() in desc.lower() and "constant" in desc.lower():
                            major, minor = (int(x, 16) for x in ver.split("."))
                            if best is None or (major, minor) > best[1:]:
                                best = (guid, major, minor)
            except OSError:
                continue
    if best is None:
        raise OpFailed("SOLIDWORKS constant type library not found in registry")
    mod = gencache.EnsureModule(best[0], 0, best[1], best[2])
    return mod.constants


class SwExecutor:
    def __init__(self, sw, constants=None, material_map: dict[str, tuple[str, str]] | None = None,
                 templates: dict[str, str] | None = None):
        import pythoncom  # type: ignore[import-not-found]
        import win32com.client  # type: ignore[import-not-found]

        self.pythoncom = pythoncom
        self.win32 = win32com.client
        self.sw = sw
        self.c = constants or load_constants()
        self.math = sw.GetMathUtility()
        self.material_map = material_map or {}
        self.templates = templates or {}
        self.model = None
        self.sketch_ctx: dict[str, Any] = {}
        self.features: dict[str, Any] = {}
        self.log: list[dict[str, Any]] = []
        self.out_dir: Path | None = None
        self.saved_path: Path | None = None
        self.readback: dict[str, Any] = {}

    # ---------- 运行 ----------
    def run(self, ops: list[NativeOp], out_dir: Path, name: str) -> dict[str, Any]:
        self.out_dir = out_dir
        status = "ok"
        for i, op in enumerate(ops):
            t0 = time.perf_counter()
            rec: dict[str, Any] = {"i": i, "op": op.op, "args": _short(op.args)}
            try:
                handler = getattr(self, "op_" + op.op, None)
                if handler is None:
                    raise OpFailed(f"no handler for op {op.op!r}")
                res = handler(**op.args)
                rec.update(ok=True, result=_short(res))
            except Exception as e:  # noqa: BLE001
                rec.update(ok=False, error=f"{type(e).__name__}: {e}", trace=traceback.format_exc(limit=4))
                if op.op not in NON_CRITICAL:
                    status = "errors"
                    rec["ms"] = round((time.perf_counter() - t0) * 1000)
                    self.log.append(rec)
                    break
                status = "warnings" if status == "ok" else status
            rec["ms"] = round((time.perf_counter() - t0) * 1000)
            self.log.append(rec)
        (out_dir / f"{name}.oplog.json").write_text(json.dumps(self.log, ensure_ascii=False, indent=1, default=str),
                                                     encoding="utf-8")
        return {"status": status, "log": self.log}

    # ---------- 工具 ----------
    def _ref_planes(self):
        planes = []
        f = self.model.FirstFeature()
        while f is not None and len(planes) < 3:
            if f.GetTypeName2() == "RefPlane":
                planes.append(f)
            f = f.GetNextFeature()
        if len(planes) < 3:
            raise OpFailed("could not find the three default reference planes")
        return {"Front Plane": planes[0], "Top Plane": planes[1], "Right Plane": planes[2]}

    def _to_sketch(self, xyz_mm) -> tuple[float, float]:
        sk = self.model.SketchManager.ActiveSketch
        xf = sk.ModelToSketchTransform
        pt = self.math.CreatePoint([v * MM for v in xyz_mm])
        sp = pt.MultiplyTransform(xf).ArrayData
        return sp[0], sp[1]

    def _uv_to_xyz(self, uv) -> list[float]:
        ctx = self.sketch_ctx
        if ctx.get("plane"):
            return [uv[0], uv[1], 0.0]
        c = ctx["plane_coord"]
        u, v = uv
        return {"top": [u, v, c], "bottom": [u, v, c], "front": [u, c, v], "back": [u, c, v],
                "left": [c, u, v], "right": [c, u, v]}[ctx["face"]]

    def _sk(self, uv) -> tuple[float, float]:
        if self.sketch_ctx.get("plane") == "Front Plane":
            return uv[0] * MM, uv[1] * MM
        return self._to_sketch(self._uv_to_xyz(uv))

    def _name_last_feature(self, name: str):
        feat = self.model.FeatureByPositionReverse(0)
        if feat is None:
            raise OpFailed(f"feature {name} was not created")
        feat.Name = name
        self.features[name] = feat
        return feat

    def _close_sketch(self):
        """退出当前草图，并把刚生成的草图特征改成计划中的名字（如 SK_Base）。"""
        if self.model.SketchManager.ActiveSketch is not None:
            self.model.SketchManager.InsertSketch(True)
            pending = self.sketch_ctx.pop("pending_name", None)
            if pending:
                feat = self.model.FeatureByPositionReverse(0)
                if feat is not None:
                    try:
                        feat.Name = pending
                    except Exception:  # noqa: BLE001 — 改名失败不影响建模
                        pass
                    self.features[pending] = feat

    def _select_sketch(self, name: str):
        self._close_sketch()
        feat = self.features.get(name)
        if feat is None:
            raise OpFailed(f"sketch {name} not found")
        self.model.ClearSelection2(True)
        if not feat.Select2(False, 0):
            raise OpFailed(f"could not select sketch {name}")

    # ---------- 文档 ----------
    def op_new_document(self, kind: str, template: str = ""):
        pref = {"part": self.c.swDefaultTemplatePart, "assembly": self.c.swDefaultTemplateAssembly}[kind]
        tpl = template or self.templates.get(kind) or self.sw.GetUserPreferenceStringValue(pref)
        self.model = self.sw.NewDocument(tpl, 0, 0, 0)
        if self.model is None:
            raise OpFailed(f"NewDocument failed for template {tpl!r}")
        return {"template": tpl}

    def op_set_units(self, system: str):
        ok = self.model.Extension.SetUserPreferenceInteger(self.c.swUnitSystem, 0, self.c.swUnitSystem_MMGS)
        return {"ok": bool(ok)}

    def op_add_global_variable(self, name: str, equation: str):
        idx = self.model.GetEquationMgr().Add2(-1, equation, True)
        if idx < 0:
            raise OpFailed(f"equation rejected: {equation}")
        return {"index": idx}

    def op_set_material(self, database: str, name: str):
        db, mat = self.material_map.get(name, ("SOLIDWORKS Materials", name))
        self.model.SetMaterialPropertyName2("", db, mat)
        got = self.model.GetMaterialPropertyName2("", "")
        if not got or not got[0]:
            raise OpFailed(f"material {mat!r} not found in {db!r}；请在节点配置 material_map 中映射")
        return {"material": got[0]}

    def op_set_custom_properties(self, properties: dict[str, str]):
        mgr = self.model.Extension.CustomPropertyManager("")
        for k, v in properties.items():
            mgr.Add3(k, self.c.swCustomInfoText, str(v), self.c.swCustomPropertyReplaceValue)
        return {"count": len(properties)}

    # ---------- 草图 ----------
    def op_sketch_on_plane(self, sketch: str, plane: str):
        self._close_sketch()
        self.model.ClearSelection2(True)
        if not self._ref_planes()[plane].Select2(False, 0):
            raise OpFailed(f"could not select {plane}")
        self.model.SketchManager.InsertSketch(True)
        self.sketch_ctx = {"name": sketch, "plane": plane}
        return self._name_active_sketch(sketch)

    def op_sketch_on_face(self, sketch: str, face: str, ray_from, ray_dir, bbox, plane_coord: float):
        self._close_sketch()
        self.model.ClearSelection2(True)
        p = [v * MM for v in ray_from]
        ok = self.model.Extension.SelectByRay(p[0], p[1], p[2], ray_dir[0], ray_dir[1], ray_dir[2],
                                              0.0005, self.c.swSelFACES, False, 0, 0)
        if not ok:
            raise OpFailed(f"SelectByRay found no face for {face}")
        self.model.SketchManager.InsertSketch(True)
        self.sketch_ctx = {"name": sketch, "face": face, "bbox": bbox, "plane_coord": plane_coord}
        return self._name_active_sketch(sketch)

    def _name_active_sketch(self, sketch: str):
        if self.model.SketchManager.ActiveSketch is None:
            raise OpFailed("no active sketch")
        self.sketch_ctx["pending_name"] = sketch
        return {"sketch": sketch}

    def op_sketch_center_rectangle(self, sketch: str, center, width: float, height: float):
        sm = self.model.SketchManager
        cx, cy = self._sk(center)
        corner = self._sk((center[0] + width / 2, center[1] + height / 2))
        segs = sm.CreateCenterRectangle(cx, cy, 0, corner[0], corner[1], 0)
        if segs is None:
            raise OpFailed("CreateCenterRectangle returned nothing")
        self.sketch_ctx["rect"] = (center, width, height)
        return {"segments": len(segs)}

    def op_sketch_fillet_all_corners(self, sketch: str, radius: float):
        (cx, cy), w, h = self.sketch_ctx["rect"]
        self.model.ClearSelection2(True)
        for sx in (-1, 1):
            for sy in (-1, 1):
                x, y = self._sk((cx + sx * w / 2, cy + sy * h / 2))
                self.model.Extension.SelectByID2("", "SKETCHPOINT", x, y, 0, True, 0, None, 0)
        seg = self.model.SketchManager.CreateFillet(radius * MM, self.c.swConstrainedCornerDeleteGeometry)
        if seg is None:
            raise OpFailed("CreateFillet failed")
        return {}

    def op_sketch_circle(self, sketch: str, center, diameter: float):
        x, y = self._sk(center)
        if self.model.SketchManager.CreateCircleByRadius(x, y, 0, diameter / 2 * MM) is None:
            raise OpFailed("CreateCircleByRadius failed")
        return {}

    def op_sketch_circles(self, sketch: str, points, diameter: float):
        sm = self.model.SketchManager
        sm.AddToDB = True  # 关闭自动推理/捕捉，避免点被吸附
        try:
            for p in points:
                x, y = self._to_sketch(p)
                if sm.CreateCircleByRadius(x, y, 0, diameter / 2 * MM) is None:
                    raise OpFailed(f"circle at {p} failed")
        finally:
            sm.AddToDB = False
        return {"count": len(points)}

    def op_sketch_points(self, sketch: str, points):
        sm = self.model.SketchManager
        for p in points:
            x, y = self._to_sketch(p)
            sm.CreatePoint(x, y, 0)
        return {"count": len(points)}

    def op_sketch_slot(self, sketch: str, length: float, width: float, center, angle: float):
        a = math.radians(angle)
        dx, dy = (length - width) / 2 * math.cos(a), (length - width) / 2 * math.sin(a)
        p1 = self._sk((center[0] - dx, center[1] - dy))
        p2 = self._sk((center[0] + dx, center[1] + dy))
        res = self.model.SketchManager.CreateSketchSlot(self.c.swSketchSlotCreationType_line,
                                                        self.c.swSketchSlotLengthType_CenterCenter, width * MM,
                                                        p1[0], p1[1], 0, p2[0], p2[1], 0, 0, 0, 0, 1, False)
        if res is None:
            raise OpFailed("CreateSketchSlot failed")
        return {}

    def op_sketch_slot_uv(self, sketch: str, end1, end2, width: float):
        p1, p2 = self._sk(end1), self._sk(end2)
        res = self.model.SketchManager.CreateSketchSlot(self.c.swSketchSlotCreationType_line,
                                                        self.c.swSketchSlotLengthType_CenterCenter, width * MM,
                                                        p1[0], p1[1], 0, p2[0], p2[1], 0, 0, 0, 0, 1, False)
        if res is None:
            raise OpFailed("CreateSketchSlot failed")
        return {}

    def op_sketch_polyline_closed(self, sketch: str, points):
        sm = self.model.SketchManager
        pts = [self._sk(p) for p in points]
        for a, b in zip(pts, pts[1:] + pts[:1]):
            if sm.CreateLine(a[0], a[1], 0, b[0], b[1], 0) is None:
                raise OpFailed("CreateLine failed")
        return {"edges": len(pts)}

    def op_sketch_shaft_half_profile(self, sketch: str, steps, axis: str, chamfer: float = 0.0):
        # Right Plane（YZ）上画半剖面：草图坐标用模型 (0, y, z) 换算；两端倒角直接画进轮廓（不依赖选边）
        sm = self.model.SketchManager
        c = chamfer
        total = sum(L for L, _ in steps)
        pts = [(0.0, 0.0, 0.0)]
        if c:
            pts += [(0.0, steps[0][1] / 2 - c, 0.0), (0.0, steps[0][1] / 2, c)]
        z = 0.0
        for i, (L, d) in enumerate(steps):
            if not (c and i == 0):
                pts.append((0.0, d / 2, z))
            z += L
            if c and i == len(steps) - 1:
                pts += [(0.0, d / 2, z - c), (0.0, d / 2 - c, z)]
            else:
                pts.append((0.0, d / 2, z))
        pts.append((0.0, 0.0, total))
        sk_pts = [self._to_sketch(p) for p in pts]
        for a, b in zip(sk_pts, sk_pts[1:] + sk_pts[:1]):
            sm.CreateLine(a[0], a[1], 0, b[0], b[1], 0)
        a, b = self._to_sketch((0, 0, -5)), self._to_sketch((0, 0, z + 5))
        sm.CreateCenterLine(a[0], a[1], 0, b[0], b[1], 0)
        return {"points": len(pts)}

    def op_sketch_slot_3d(self, sketch: str, end1, end2, width: float):
        p1, p2 = self._to_sketch(end1), self._to_sketch(end2)
        res = self.model.SketchManager.CreateSketchSlot(self.c.swSketchSlotCreationType_line,
                                                        self.c.swSketchSlotLengthType_CenterCenter, width * MM,
                                                        p1[0], p1[1], 0, p2[0], p2[1], 0, 0, 0, 0, 1, False)
        if res is None:
            raise OpFailed("CreateSketchSlot failed")
        return {}

    def op_sketch_polyline_3d(self, sketch: str, points, centerline=None):
        sm = self.model.SketchManager
        sk = [self._to_sketch(p) for p in points]
        for a, b in zip(sk, sk[1:] + sk[:1]):
            if sm.CreateLine(a[0], a[1], 0, b[0], b[1], 0) is None:
                raise OpFailed("CreateLine failed")
        if centerline:
            a, b = self._to_sketch(centerline[0]), self._to_sketch(centerline[1])
            sm.CreateCenterLine(a[0], a[1], 0, b[0], b[1], 0)
        return {"edges": len(sk)}

    def op_sketch_circle_3d(self, sketch: str, center, diameter: float):
        c = self._to_sketch(center)
        if self.model.SketchManager.CreateCircleByRadius(c[0], c[1], 0, diameter / 2 * MM) is None:
            raise OpFailed("CreateCircleByRadius failed")
        return {}

    def op_insert_weldment(self):
        if self.model.FeatureManager.InsertWeldmentFeature() is None:
            raise OpFailed("InsertWeldmentFeature failed")
        return {}

    def op_extrude_body(self, feature: str, sketch: str, start_offset: float, depth: float):
        """独立实体（Merge=False）：从草图平面偏移 start_offset（可为负）开始拉伸 depth。"""
        self._select_sketch(sketch)
        fm = self.model.FeatureManager
        t0 = self.c.swStartOffset if abs(start_offset) > 1e-9 else 0
        feat = fm.FeatureExtrusion3(True, False, False, self.c.swEndCondBlind, 0, depth * MM, 0,
                                    False, False, False, False, 0, 0, False, False, False, False,
                                    False, True, True, t0, abs(start_offset) * MM, start_offset < 0)
        if feat is None:
            raise OpFailed(f"FeatureExtrusion3 (body {feature}) failed")
        feat.Name = feature
        self.features[feature] = feat
        return {"feature": feature}

    def op_weldment_cut_list(self, view: str, anchor: str, rows):
        v = self.views.get(view) if hasattr(self, "views") else None
        if v is None or v.InsertWeldmentTable(False, 0.0, 0.0, 1, "", "") is None:
            raise OpFailed("InsertWeldmentTable failed (cut list in notes)")
        return {"rows": len(rows)}

    def op_keyway_cut(self, feature: str, sketch: str, start_offset: float, depth: float, flip: bool = False):
        """从草图平面偏移 start_offset 处开始、沿法向切 depth（键槽底面 = 轴心 + d/2 - t1）。"""
        self._select_sketch(sketch)
        fm = self.model.FeatureManager
        feat = None
        for fl in (flip, not flip):
            feat = fm.FeatureCut4(True, fl, False, self.c.swEndCondBlind, 0, depth * MM, 0, False, False, False, False,
                                  0, 0, False, False, False, False, False, True, True, True,
                                  True, False, self.c.swStartOffset, start_offset * MM, fl, False)
            if feat is not None:
                break
        if feat is None:
            raise OpFailed("keyway FeatureCut4 failed")
        feat.Name = feature
        self.features[feature] = feat
        return {"feature": feature}

    def op_dimension_profile(self, sketch: str, links: dict[str, str]):
        # P1：草图尺寸标注与方程式链接待实机完成；当前仅链接拉伸深度（见 extrude_boss）
        raise OpFailed("dimension_profile not implemented yet (non-critical)")

    # ---------- 特征 ----------
    def op_extrude_boss(self, feature: str, sketch: str, depth: float, depth_equation: str | None = None):
        self._select_sketch(sketch)
        fm = self.model.FeatureManager
        feat = fm.FeatureExtrusion3(True, False, False, self.c.swEndCondBlind, 0, depth * MM, 0,
                                    False, False, False, False, 0, 0, False, False, False, False,
                                    True, True, True, 0, 0, False)
        if feat is None:
            raise OpFailed("FeatureExtrusion3 failed")
        feat.Name = feature
        self.features[feature] = feat
        if depth_equation:
            self.model.GetEquationMgr().Add2(-1, f'"D1@{feature}" = {depth_equation}', True)
        return {"feature": feature}

    def op_extrude_cut(self, feature: str, sketch: str, end: str, depth: float, draft_deg: float = 0):
        self._select_sketch(sketch)
        fm = self.model.FeatureManager
        t1 = self.c.swEndCondThroughAll if end == "through_all" else self.c.swEndCondBlind
        feat = fm.FeatureCut4(True, False, False, t1, 0, depth * MM, 0, draft_deg > 0, False, False, False,
                              math.radians(draft_deg), 0, False, False, False, False, False, True, True, True,
                              True, False, 0, 0, False, False)
        if feat is None:
            # 方向可能反了：反向重试
            feat = fm.FeatureCut4(True, True, False, t1, 0, depth * MM, 0, draft_deg > 0, False, False, False,
                                  math.radians(draft_deg), 0, False, False, False, False, False, True, True, True,
                                  True, False, 0, 0, False, False)
        if feat is None:
            raise OpFailed("FeatureCut4 failed (both directions)")
        feat.Name = feature
        self.features[feature] = feat
        return {"feature": feature}

    def op_revolve(self, feature: str, sketch: str, angle: float, cut: bool = False):
        self._select_sketch(sketch)
        feat = self.model.FeatureManager.FeatureRevolve2(True, True, False, cut, False, False,
                                                         self.c.swEndCondBlind, 0, math.radians(angle), 0,
                                                         False, False, 0, 0, 0, 0, 0, True, True, True)
        if feat is None:
            raise OpFailed("FeatureRevolve2 failed")
        feat.Name = feature
        self.features[feature] = feat
        return {}

    def op_sheet_metal_base_flange(self, feature: str, sketch: str, thickness: float, bend_radius: float, k_factor: float):
        self._select_sketch(sketch)
        feat = self.model.FeatureManager.InsertSheetMetalBaseFlange2(
            thickness * MM, False, bend_radius * MM, 0, 0, False, 0, 0, 0, None, False, 0, 0, 0, 0,
            False, False, False, False)
        if feat is None:
            raise OpFailed("InsertSheetMetalBaseFlange2 failed")
        feat.Name = feature
        return {}

    def op_sheet_metal_edge_flange(self, **kw):
        raise OpFailed("edge flange: P1 实机实现（需按边选择规则 + InsertSheetMetalEdgeFlange2）")

    def op_hole_wizard(self, **kw):
        raise OpFailed("hole_wizard: 需实机核对 HoleWizard5 GB 标准参数表；请用 holes_via='cut'")

    def op_cosmetic_thread(self, feature: str, size: str, depth: float | None):
        raise OpFailed("cosmetic thread: P1 实现（InsertCosmeticThread3）")

    def op_select_edges(self, rule: str):
        raise OpFailed("edge selection rules: P1 实现")

    def op_fillet(self, feature: str, size: float):
        raise OpFailed("skipped (edges not selected)")

    def op_chamfer(self, feature: str, size: float):
        raise OpFailed("skipped (edges not selected)")

    def op_coordinate_system(self, feature: str, origin, rotate_deg):
        self._close_sketch()
        fm = self.model.FeatureManager
        o = [v * MM for v in origin]
        r = [math.radians(v) for v in rotate_deg]
        feat = fm.CreateCoordinateSystemUsingNumericalValues(True, o[0], o[1], o[2], True, r[0], r[1], r[2])
        if feat is None:
            raise OpFailed("CreateCoordinateSystemUsingNumericalValues failed")
        feat.Name = feature
        self.features[feature] = feat
        return {}

    def op_ref_plane_coincident(self, feature: str, point, normal):
        raise OpFailed("named planes: P1 实现（SelectByRay + InsertRefPlane）")

    def op_ref_axis(self, feature: str, point, direction):
        raise OpFailed("named axes: P1 实现")

    def op_ref_point(self, feature: str, point):
        raise OpFailed("named points: P1 实现")

    # ---------- 校验 ----------
    def op_rebuild(self, force: bool = True):
        self._close_sketch()
        ok = self.model.ForceRebuild3(False)
        return {"ok": bool(ok)}

    def op_collect_feature_errors(self):
        errors, warnings = [], []
        f = self.model.FirstFeature()
        while f is not None:
            try:
                code, is_warn = f.GetErrorCode2()
            except Exception:  # noqa: BLE001
                code, is_warn = 0, False
            if code:
                (warnings if is_warn else errors).append({"feature": f.Name, "code": int(code)})
            f = f.GetNextFeature()
        self.readback["feature_errors"] = errors
        self.readback["feature_warnings"] = warnings
        if errors:
            raise OpFailed(f"feature errors: {errors}")
        return {"warnings": warnings}

    def op_measure_mass(self, expect_kg: float | None = None):
        mp = self.model.Extension.CreateMassProperty()
        box = self.model.GetPartBox(True)
        mass = float(mp.Mass)
        bb = [round((box[i + 3] - box[i]) / MM, 3) for i in range(3)] if box else None
        self.readback["mass_kg"] = mass
        self.readback["bbox_mm"] = bb
        return {"mass_kg": mass, "bbox_mm": bb, "expect_kg": expect_kg}

    def op_interference_check(self):
        mgr = self.model.InterferenceDetectionManager
        mgr.TreatCoincidenceAsInterference = False
        mgr.IncludeMultibodyPartInterferences = False
        items = mgr.GetInterferences() or []
        res = []
        for it in items:
            comps = it.Components
            res.append({"components": [c.Name2 for c in comps], "volume_mm3": round(it.Volume / MM ** 3, 1)})
        mgr.Done()
        self.readback["interferences"] = res
        return {"count": len(res)}

    # ---------- 装配 ----------
    def op_add_component(self, instance: str, transform, file: str | None = None, library: str | None = None,
                         query: str = "", library_root: str = "", out_name: str = ""):
        if library:
            file = self._library_instance(library, query, library_root, out_name)
        doc = self.sw.OpenDoc6(file, self._doctype(file), self.c.swOpenDocOptions_Silent, "", 0, 0)
        if doc is None:
            raise OpFailed(f"cannot open component {file}")
        self.sw.ActivateDoc3(self.model.GetTitle(), False, 0, 0)
        t = transform
        comp = self.model.AddComponent5(file, self.c.swAddComponentConfigOptions_CurrentSelectedConfig, "", False, "",
                                        t[0][3] * MM, t[1][3] * MM, t[2][3] * MM)
        if comp is None:
            raise OpFailed(f"AddComponent5 failed for {file}")
        data = [t[0][0], t[1][0], t[2][0], t[0][1], t[1][1], t[2][1], t[0][2], t[1][2], t[2][2],
                t[0][3] * MM, t[1][3] * MM, t[2][3] * MM, 1.0, 0, 0, 0]
        comp.Transform2 = self.math.CreateTransform(data)
        self.features[instance] = comp
        return {"component": comp.Name2}

    def _doctype(self, file: str):
        ext = Path(file).suffix.upper()
        return self.c.swDocPART if ext == ".SLDPRT" else self.c.swDocASSEMBLY

    def _library_instance(self, code: str, query: str, root: str, out_name: str) -> str:
        from urllib.parse import parse_qsl

        src = Path(root) / f"{code}.SLDPRT"
        if not src.exists():
            raise OpFailed(f"library file {src} missing；标准件库需提供带 CS_ 基准的模型")
        params = dict(parse_qsl(query))
        if not params:
            return str(src)
        doc = self.sw.OpenDoc6(str(src), self.c.swDocPART, self.c.swOpenDocOptions_Silent, "", 0, 0)
        eq = doc.GetEquationMgr()
        for k, v in params.items():
            for i in range(eq.GetCount()):
                if eq.Equation(i).replace(" ", "").startswith(f'"{k}"='):
                    eq.Equation(i, f'"{k}" = {v}')
        doc.ForceRebuild3(False)
        out = self.out_dir / f"{out_name}.SLDPRT"
        doc.SaveAs3(str(out), 0, self.c.swSaveAsOptions_Copy)
        self.sw.CloseDoc(doc.GetTitle())
        return str(out)

    def op_fix_component(self, instance: str):
        self.model.ClearSelection2(True)
        self.features[instance].Select4(False, None, False)
        self.model.FixComponent()
        return {}

    def op_mate_coordinate(self, instance: str, ref: str, target_instance: str | None, target_ref: str,
                           offset, rotate, fallback: str):
        comp = self.features[instance]
        try:
            self.model.ClearSelection2(True)
            a = f"{ref}@{comp.Name2}@{self.model.GetTitle().rsplit('.', 1)[0]}"
            if not self.model.Extension.SelectByID2(a, "COORDSYS", 0, 0, 0, False, 1, None, 0):
                raise OpFailed(f"cannot select {a}")
            if target_instance:
                tc = self.features[target_instance]
                b = f"{target_ref}@{tc.Name2}@{self.model.GetTitle().rsplit('.', 1)[0]}"
                if not self.model.Extension.SelectByID2(b, "COORDSYS", 0, 0, 0, True, 1, None, 0):
                    raise OpFailed(f"cannot select {b}")
            if any(abs(v) > 1e-9 for v in list(offset) + list(rotate)):
                raise OpFailed("offset/rotate coordinate mates: P1（当前退回固定位姿）")
            mate = self.model.AddMate5(self.c.swMateCOORDINATE, self.c.swMateAlignALIGNED, False, 0, 0, 0, 0, 0,
                                       0, 0, 0, False, False, 0, 0)
            if mate is None:
                raise OpFailed("AddMate5 coordinate mate failed")
            return {"mate": "coordinate"}
        except Exception as e:  # noqa: BLE001
            self.model.ClearSelection2(True)
            comp.Select4(False, None, False)
            self.model.FixComponent()
            return {"mate": "fixed_at_transform", "reason": str(e)}

    # ---------- 工程图 ----------
    def op_new_drawing(self, sheet: str, template: str, width: float, height: float, scale: float):
        tpl = template or self.templates.get(f"drawing_{sheet}") or self.sw.GetUserPreferenceStringValue(
            self.c.swDefaultTemplateDrawing)
        self.model = self.sw.NewDocument(tpl, self.c.swDwgPapersUserDefined, width * MM, height * MM)
        if self.model is None:
            raise OpFailed(f"NewDocument(drawing) failed for {tpl!r}")
        sh = self.model.GetCurrentSheet()
        num, den = (scale, 1) if scale >= 1 else (1, round(1 / scale, 2))
        sh.SetScale(num, den, False, False)
        self.views: dict[str, Any] = {}
        return {"template": tpl}

    def op_model_view(self, view: str, sw_view: str, model: str, center, scale: float, rotate_deg: float, display: str):
        v = self.model.CreateDrawViewFromModelView3(model, sw_view, center[0] * MM, center[1] * MM, 0)
        if v is None:
            raise OpFailed(f"CreateDrawViewFromModelView3({sw_view}) failed；中文版可能需要本地化视图名")
        v.UseSheetScale = False
        num, den = (scale, 1) if scale >= 1 else (1, round(1 / scale, 2))
        v.ScaleRatio = (num, den)
        if rotate_deg:
            v.Angle = math.radians(rotate_deg)
        v.SetDisplayMode4(False, self.c.swHIDDEN_GREYED if display == "hidden_visible" else self.c.swHIDDEN, False, True, False)
        self.views[view] = v
        return {"name": v.Name}

    def op_flat_pattern_view(self, model: str, center, scale: float):
        v = self.model.CreateFlatPatternViewFromModelView3(model, "", center[0] * MM, center[1] * MM, 0, False, False)
        if v is None:
            raise OpFailed("flat pattern view failed")
        self.views["flat_pattern"] = v
        return {}

    def op_auto_dimension(self, view: str, scheme: str, origin: str):
        v = self.views[view]
        self.model.ActivateView(v.Name)
        self.model.ClearSelection2(True)
        self.model.Extension.SelectByID2(v.Name, "DRAWINGVIEW", 0, 0, 0, False, 0, None, 0)
        r = self.model.AutoDimension(self.c.swAutodimEntitiesAll, self.c.swAutodimSchemeOrdinate,
                                     self.c.swAutodimHorizontalPlacementBelow, self.c.swAutodimSchemeOrdinate,
                                     self.c.swAutodimVerticalPlacementLeft)
        if r != self.c.swAutodimStatusSuccess:
            raise OpFailed(f"AutoDimension status {r}")
        return {"status": r}

    def op_overall_dimension(self, view: str, axis: str, value: float):
        raise OpFailed("overall dimension on non-ordinate views: P1（需按视图轮廓选边）")

    def op_section_view(self, label: str, parent: str, z: float, center):
        raise OpFailed("section view: P1 实机实现（在主视图画垂直剖切线 + CreateSectionViewAt5，移出放置）")

    def op_shaft_dimension(self, view: str, kind: str, text: str, at, ref: str):
        raise OpFailed("shaft dimension: P1 实机实现（按轴肩边线选边标注 / 直径标注加公差带）")

    def op_leader_note(self, view: str, text: str, at_model, ref: str):
        v = self.views.get(view)
        if v is None:
            raise OpFailed(f"view {view} not on sheet")
        self.model.ActivateView(v.Name)
        note = self.model.InsertNote(text)
        if note is None:
            raise OpFailed("InsertNote failed")
        ann = note.GetAnnotation()
        pos = v.Position  # 视图中心（图纸坐标，m）
        ann.SetPosition2(pos[0] + 0.03, pos[1] + 0.02, 0)
        self.readback.setdefault("notes", []).append(ref)
        return {}

    def op_note(self, text: str, anchor: str):
        note = self.model.InsertNote(text)
        if note is None:
            raise OpFailed("InsertNote failed")
        sh = self.model.GetCurrentSheet()
        w, h = sh.GetSize()[1:3] if len(sh.GetSize()) > 2 else (0.42, 0.297)
        note.GetAnnotation().SetPosition2(w - 0.185, 0.07 + 0.005 * text.count("\n"), 0)
        self.readback.setdefault("notes", []).append("notes")
        return {}

    # swconst 中名称以实机为准；取不到时用 API 帮助里的枚举值
    def _k(self, name: str, default: int) -> int:
        try:
            return getattr(self.c, name)
        except AttributeError:
            return default

    @staticmethod
    def _qi(obj, iface: str):
        """BomTableAnnotation → TableAnnotation 等接口转换（晚绑定下需 CastTo；失败时用原对象）。"""
        try:
            import win32com.client as wc
            return wc.CastTo(obj, iface)
        except Exception:  # noqa: BLE001
            return obj

    def _comp_keys(self, comp) -> set[str]:
        out = set()
        try:
            out.add(Path(str(comp.GetPathName())).stem.lower())
        except Exception:  # noqa: BLE001
            pass
        try:
            out.add(str(comp.GetModelDoc2().GetCustomInfoValue("", "PartNo")).lower())
        except Exception:  # noqa: BLE001
            pass
        return {k for k in out if k}

    def op_bom_table(self, view: str, anchor: str, columns, rows=None, header_bottom: bool = True):
        v = self.views[view]
        self.model.ActivateView(v.Name)
        tpl = self.templates.get("bom", "")
        bom = v.InsertBomTable4(True, 0.4, 0.1, self.c.swBOMConfigurationAnchor_BottomRight,
                                self.c.swBomType_TopLevelOnly, "", tpl, False, self.c.swNumberingType_Detailed, False)
        if bom is None:
            raise OpFailed("InsertBomTable4 failed")
        if not rows:
            return {}
        tbl = self._qi(bom, "ITableAnnotation")
        notes = []
        if header_bottom:  # GB/T 10609.2：表头在下，序号自下而上
            try:
                tbl.SetHeader(self._k("swTableHeader_Bottom", 2), 1)
            except Exception as e:  # noqa: BLE001
                notes.append(f"SetHeader: {e}")
        ncol = int(tbl.ColumnCount or 0)
        for i, (title, prop) in enumerate(columns[:ncol]):
            try:
                tbl.SetColumnTitle2(i, title, True)
                if prop:
                    bom.SetColumnCustomProperty(i, prop)
            except Exception as e:  # noqa: BLE001
                notes.append(f"column {i}: {e}")
        cfg = str(getattr(v, "ReferencedConfiguration", "") or "")
        want = {r["item"]: r for r in rows}

        def data_rows():
            n = int(tbl.RowCount or 0)
            return list(range(0, n - 1)) if header_bottom else list(range(1, n))

        def item_of(row) -> int | None:
            comps = bom.GetComponents2(row, cfg) or []
            keys = self._comp_keys(comps[0]) if comps else set()
            return next((it for it, r in want.items() if keys & set(r["keys"])), None)

        # 按方案序号排行：表头在下时自上而下为 n…1
        target = sorted(want, reverse=header_bottom)
        for pos, it in enumerate(target):
            rs = data_rows()
            if pos >= len(rs):
                break
            cur = next((r for r in rs if item_of(r) == it), None)
            if cur is not None and cur != rs[pos]:
                tbl.MoveRow(cur, self._k("swTableItemInsertPosition_Before", 3), rs[pos])
        # 回读：序号 / 代号 / 数量（列号按 columns 顺序）
        ci = {t: i for i, (t, _) in enumerate(columns)}
        got = []
        for r in data_rows():
            it = item_of(r)
            try:
                if it is not None and str(tbl.Text(r, ci["序号"])).strip() != str(it):
                    tbl.Text2(r, ci["序号"], True)  # 触发重算
                got.append([str(tbl.Text(r, ci["序号"])).strip(), str(tbl.Text(r, ci["代号"])).strip(),
                            str(tbl.Text(r, ci["数量"])).strip()])
            except Exception as e:  # noqa: BLE001
                notes.append(f"readback row {r}: {e}")
        self.readback["bom_rows"] = got
        return {"rows": len(got), **({"notes": notes} if notes else {})}

    def op_bom_balloons(self, view: str, balloons):
        """逐个零件插 BOM 序号（链接明细表行），放到规划位置；任何一个找不到零件就整体退回自动序号。"""
        v = self.views[view]
        self.model.ActivateView(v.Name)
        comps = []
        try:
            comps = list(v.RootDrawingComponent.GetChildren() or [])
        except Exception:  # noqa: BLE001
            comps = []
        placed, missing = [], []
        for b in balloons:
            dc = next((d for d in comps if self._comp_keys(d.Component) & set(b["keys"])), None)
            if dc is None:
                missing.append(b["item"])
                continue
            self.model.ClearSelection2(True)
            dc.Select(False, None)
            note = self.model.Extension.InsertBOMBalloon2(
                self._k("swBS_Circular", 1), self._k("swBF_Tightest", 0), self._k("swBalloonTextItemNumber", 1), "",
                self._k("swBalloonTextCustom", 0), "")
            if note is None:
                missing.append(b["item"])
                continue
            note.GetAnnotation().SetPosition2(b["pos"][0], b["pos"][1], 0)
            placed.append(b["item"])
        if missing and not placed:
            self.op_auto_balloon(view)
        self.readback["balloon_items"] = self._read_balloons(v)
        return {"placed": placed, "missing": missing}

    def _read_balloons(self, v) -> list[str]:
        items = []
        try:
            n = v.GetFirstNote()
            for _ in range(5000):  # 上限：防止异常的注释链死循环
                if n is None:
                    break
                if n.IsBomBalloon() is True:
                    items.append(str(n.GetText()).strip())
                n = n.GetNext()
        except Exception:  # noqa: BLE001
            pass
        return items

    def op_auto_balloon(self, view: str):
        v = self.views[view]
        self.model.ClearSelection2(True)
        self.model.Extension.SelectByID2(v.Name, "DRAWINGVIEW", 0, 0, 0, False, 0, None, 0)
        opts = self.model.CreateAutoBalloonOptions()
        self.model.AutoBalloon5(opts)
        return {}

    def op_readback_annotations(self):
        dims = 0
        v = self.model.GetFirstView()
        while v is not None:
            dd = v.GetDisplayDimensions() or []
            dims += len(dd)
            v = v.GetNextView()
        self.readback["dim_count"] = dims
        return {"dim_count": dims}

    # ---------- 修改现有文档 ----------
    def op_open_document(self, path: str):
        ext = Path(path).suffix.upper()
        dt = {".SLDPRT": self.c.swDocPART, ".SLDASM": self.c.swDocASSEMBLY, ".SLDDRW": self.c.swDocDRAWING}[ext]
        doc = self.sw.OpenDoc6(path, dt, self.c.swOpenDocOptions_Silent, "", 0, 0)
        if doc is None:
            raise OpFailed(f"OpenDoc6 failed for {path}")
        self.model = doc
        self.saved_path = Path(path)
        return {"title": doc.GetTitle()}

    def _dim_value(self, dd, dim) -> tuple[float, str]:
        ang = getattr(self.c, "swAngularDimension", None)
        is_ang = ang is not None and dd.Type2 == ang
        v = float(dim.SystemValue)
        return (round(math.degrees(v), 6), "deg") if is_ang else (round(v / MM, 6), "mm")

    def op_inspect(self):
        """特征树（名称/类型/压缩/父特征）、尺寸（全名/值/是否从动）、方程式与全局变量、属性、材料、质量。"""
        m = self.model
        feats, dims = [], []
        f = m.FirstFeature()
        while f is not None:
            try:
                sup = bool(f.IsSuppressed())
            except Exception:  # noqa: BLE001
                sup = False
            try:
                parents = [x.Name for x in (f.GetParents() or [])]
            except Exception:  # noqa: BLE001
                parents = []
            feats.append({"name": f.Name, "type": f.GetTypeName2(), "suppressed": sup, "parents": parents})
            dd = f.GetFirstDisplayDimension()
            while dd is not None:
                dim = dd.GetDimension2(0)
                val, unit = self._dim_value(dd, dim)
                dims.append({"name": dim.FullName, "value": val, "unit": unit, "feature": f.Name,
                             "driven": bool(dim.DrivenState == getattr(self.c, "swDimensionDriven", -99))})
                dd = f.GetNextDisplayDimension(dd)
            f = f.GetNextFeature()
        eqm = m.GetEquationMgr()
        equations, variables = [], []
        for i in range(eqm.GetCount()):
            text = str(eqm.Equation(i))
            lhs, _, rhs = text.partition("=")
            lhs = lhs.strip().strip('"')
            if eqm.GlobalVariable(i):
                variables.append({"name": lhs, "value": float(eqm.Value(i)), "equation": text})
            else:
                equations.append({"lhs": lhs, "rhs": rhs.strip(), "index": i})
        mgr = m.Extension.CustomPropertyManager("")
        props = {}
        for n in (mgr.GetNames() or []):
            res = mgr.Get6(n, False)
            props[n] = str(res[2] if isinstance(res, tuple) and len(res) > 2 else res)
        info = {"kind": {self.c.swDocPART: "part", self.c.swDocASSEMBLY: "assembly",
                         self.c.swDocDRAWING: "drawing"}.get(m.GetType(), "?"),
                "features": feats, "dimensions": dims, "equations": equations, "variables": variables,
                "properties": [{"name": k, "value": v} for k, v in props.items()]}
        info["references"] = [x["name"] for x in feats if x["type"] in ("CoordSys", "RefPlane", "RefAxis", "RefPoint")]
        if info["kind"] == "assembly":
            comps = []
            for c in (m.GetComponents(True) or []):
                path = str(c.GetPathName())
                comps.append({"instance": c.Name2, "file": path, "code": Path(path).stem,
                              "suppressed": bool(c.IsSuppressed())})
            info["components"] = comps
            info["mass_kg"] = round(float(m.Extension.CreateMassProperty().Mass), 4)
        if info["kind"] == "part":
            got = m.GetMaterialPropertyName2("", "")
            info["material"] = got[0] if got else ""
            mp = m.Extension.CreateMassProperty()
            info["mass_kg"] = round(float(mp.Mass), 4)
            box = m.GetPartBox(True)
            info["bbox_mm"] = [round((box[i + 3] - box[i]) / MM, 3) for i in range(3)] if box else None
        self.readback.setdefault("inspect", []).append(info)
        return {"features": len(feats), "dimensions": len(dims)}

    def op_set_dimension(self, name: str, value: float, unit: str = "mm"):
        dim = self.model.Parameter(name)
        if dim is None:
            raise OpFailed(f"dimension {name} not found")
        sv = math.radians(value) if unit == "deg" else value * MM
        err = dim.SetSystemValue3(sv, self.c.swSetValue_InThisConfiguration, None)
        if err not in (0, None):
            raise OpFailed(f"SetSystemValue3({name}) returned {err}")
        return {}

    def op_set_variable(self, name: str, value: float):
        eqm = self.model.GetEquationMgr()
        for i in range(eqm.GetCount()):
            if eqm.GlobalVariable(i) and str(eqm.Equation(i)).split("=")[0].strip().strip('"') == name:
                eqm.SetEquation(i, f'"{name}" = {value:g}')
                return {"index": i}
        raise OpFailed(f"global variable {name} not found")

    def op_suppress(self, feature: str, suppressed: bool = True):
        self.model.ClearSelection2(True)
        if not self.model.Extension.SelectByID2(feature, "BODYFEATURE", 0, 0, 0, False, 0, None, 0):
            raise OpFailed(f"cannot select feature {feature}")
        ok = self.model.EditSuppress2() if suppressed else self.model.EditUnsuppress2()
        if not ok:
            raise OpFailed(f"{'EditSuppress2' if suppressed else 'EditUnsuppress2'} failed")
        return {}

    def op_replace_component(self, instance: str, file: str, all_instances: bool = False):
        """选中实例 → IAssemblyDoc.ReplaceComponents2（重新附加配合）；配合断开由随后的重建错误检查发现。"""
        self.model.ClearSelection2(True)
        title = self.model.GetTitle().rsplit(".", 1)[0]
        if not self.model.Extension.SelectByID2(f"{instance}@{title}", "COMPONENT", 0, 0, 0, False, 0, None, 0):
            raise OpFailed(f"cannot select component {instance}")
        if not self.model.ReplaceComponents2(file, "", all_instances, 0, True):
            raise OpFailed(f"ReplaceComponents2 failed for {instance} → {file}")
        return {}

    def op_count_dangling(self):
        n = 0
        v = self.model.GetFirstView()
        while v is not None:
            for a in (v.GetAnnotations() or []):
                try:
                    n += 1 if a.IsDangling() else 0
                except Exception:  # noqa: BLE001
                    pass
            v = v.GetNextView()
        self.readback["dangling"] = n
        return {"dangling": n}

    def op_save_document(self):
        res = self.model.Save3(self.c.swSaveAsOptions_Silent, 0, 0)
        if res is False:
            raise OpFailed("Save3 failed")
        return {"path": str(self.saved_path)}

    def op_close_document(self):
        self.sw.CloseDoc(self.model.GetTitle())
        return {}

    # ---------- 保存/导出 ----------
    def op_save_as(self, name: str, ext: str):
        path = self.out_dir / f"{name}{ext}"
        err = self.model.SaveAs3(str(path), 0, self.c.swSaveAsOptions_Silent)
        if err != 0 or not path.exists():
            raise OpFailed(f"SaveAs3 error {err} for {path}")
        self.saved_path = path
        return {"path": str(path)}

    def op_export(self, format: str):
        path = self.saved_path.with_suffix("." + format)
        err = self.model.SaveAs3(str(path), 0, self.c.swSaveAsOptions_Silent | self.c.swSaveAsOptions_Copy)
        if err != 0:
            raise OpFailed(f"export {format} error {err}")
        self.readback.setdefault("exports", []).append(str(path))
        return {"path": str(path)}


def _short(v: Any, n: int = 300) -> Any:
    s = json.dumps(v, ensure_ascii=False, default=str)
    return v if len(s) <= n else s[:n] + "…"
