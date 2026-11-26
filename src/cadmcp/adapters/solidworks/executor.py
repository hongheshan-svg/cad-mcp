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
                "set_material", "leader_note", "overall_dimension", "sketch_fillet_all_corners"}


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
        x0, x1, y0, y1, z0, z1 = ctx["bbox"]
        u, v = uv
        return {"top": [u, v, z1], "bottom": [u, v, z0], "front": [u, y0, v], "back": [u, y1, v],
                "left": [x0, u, v], "right": [x1, u, v]}[ctx["face"]]

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

    def op_sketch_on_face(self, sketch: str, face: str, ray_from, ray_dir, bbox):
        self._close_sketch()
        self.model.ClearSelection2(True)
        p = [v * MM for v in ray_from]
        ok = self.model.Extension.SelectByRay(p[0], p[1], p[2], ray_dir[0], ray_dir[1], ray_dir[2],
                                              0.0005, self.c.swSelFACES, False, 0, 0)
        if not ok:
            raise OpFailed(f"SelectByRay found no face for {face}")
        self.model.SketchManager.InsertSketch(True)
        self.sketch_ctx = {"name": sketch, "face": face, "bbox": bbox}
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

    def op_sketch_polyline_closed(self, sketch: str, points):
        sm = self.model.SketchManager
        pts = [self._sk(p) for p in points]
        for a, b in zip(pts, pts[1:] + pts[:1]):
            if sm.CreateLine(a[0], a[1], 0, b[0], b[1], 0) is None:
                raise OpFailed("CreateLine failed")
        return {"edges": len(pts)}

    def op_sketch_shaft_half_profile(self, sketch: str, steps, axis: str):
        # Right Plane（YZ）上画半剖面：草图坐标用模型 (0, y, z) 换算
        sm = self.model.SketchManager
        pts = [(0.0, 0.0, 0.0)]
        z = 0.0
        for L, d in steps:
            pts.append((0.0, d / 2, z))
            z += L
            pts.append((0.0, d / 2, z))
        pts.append((0.0, 0.0, z))
        sk_pts = [self._to_sketch(p) for p in pts]
        for a, b in zip(sk_pts, sk_pts[1:] + sk_pts[:1]):
            sm.CreateLine(a[0], a[1], 0, b[0], b[1], 0)
        a, b = self._to_sketch((0, 0, -5)), self._to_sketch((0, 0, z + 5))
        sm.CreateCenterLine(a[0], a[1], 0, b[0], b[1], 0)
        return {"points": len(pts)}

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

    def op_revolve(self, feature: str, sketch: str, angle: float):
        self._select_sketch(sketch)
        feat = self.model.FeatureManager.FeatureRevolve2(True, True, False, False, False, False,
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

    def op_bom_table(self, view: str, anchor: str, columns):
        v = self.views[view]
        self.model.ActivateView(v.Name)
        tpl = self.templates.get("bom", "")
        bom = v.InsertBomTable4(True, 0.4, 0.1, self.c.swBOMConfigurationAnchor_BottomRight,
                                self.c.swBomType_TopLevelOnly, "", tpl, False, self.c.swNumberingType_Detailed, False)
        if bom is None:
            raise OpFailed("InsertBomTable4 failed")
        return {}

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
