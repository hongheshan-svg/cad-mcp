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
import re
import time
import traceback
from pathlib import Path
from typing import Any

from ..native import NativeOp

MM = 0.001
NON_CRITICAL = {"check_body_extent", "probe_gdt_constants", "datum_tag", "gtol", "surface_finish", "hole_tags", "hole_table", "broken_out_section", "section_cut", "cosmetic_thread", "fillet", "chamfer", "select_edges", "dimension_profile", "ref_plane_coincident",
                "ref_axis", "ref_point", "auto_balloon", "readback_annotations", "export", "interference_check",
                "set_material", "leader_note", "overall_dimension", "sketch_fillet_all_corners",
                "section_view", "shaft_dimension", "insert_weldment", "weldment_cut_list", "count_dangling",
                "bom_balloons", "cut_list_properties", "weld_symbol", "revision_table", "center_marks", "detail_view", "view_line", "select_edges_at"}


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

    def op_cut_list_properties(self, rows):
        """更新切割清单，并按实体名把规格/长度写到对应切割清单项（文件夹）的属性。"""
        m = self.model
        m.ForceRebuild3(False)
        folders, f, done = [], m.FirstFeature(), set()
        for _ in range(20000):
            if f is None:
                break
            tn = str(f.GetTypeName2())
            if tn == "SolidBodyFolder":
                try:
                    f.GetSpecificFeature2().UpdateCutList()
                except Exception:  # noqa: BLE001
                    pass
            elif tn == "CutListFolder":
                folders.append(f)
            f = f.GetNextFeature()
        if not folders:  # 切割清单文件夹在 SolidBodyFolder 下：遍历子特征
            f = m.FirstFeature()
            for _ in range(20000):
                if f is None:
                    break
                if str(f.GetTypeName2()) == "SolidBodyFolder":
                    sub = f.GetFirstSubFeature()
                    for _ in range(5000):
                        if sub is None:
                            break
                        if str(sub.GetTypeName2()) == "CutListFolder":
                            folders.append(sub)
                        sub = sub.GetNextSubFeature()
                f = f.GetNextFeature()
        by_body = {b: r for r in rows for b in r["bodies"]}
        for fd in folders:
            try:
                bodies = fd.GetSpecificFeature2().GetBodies() or []
                names = [str(b.Name) for b in bodies]
            except Exception:  # noqa: BLE001
                continue
            r = next((by_body[n] for n in names if n in by_body), None)
            if r is None:
                continue
            mgr = fd.CustomPropertyManager
            for k, v in r["props"].items():
                mgr.Add3(k, self.c.swCustomInfoText, str(v), self.c.swCustomPropertyReplaceValue)
            done.add(id(r))
        self.readback["cut_list_folders"] = len(folders)
        if len(done) < len(rows):
            raise OpFailed(f"cut list: {len(done)}/{len(rows)} rows matched {len(folders)} folders "
                           "(检查实体名是否保留、切割清单是否自动更新)")
        return {"folders": len(folders), "rows": len(rows)}

    def op_weld_symbol(self, view: str, size: float, both_sides: bool, all_around: bool, count: int, process: str,
                       at, pos, at_model, fallback_text: str, ref: str):
        """原生焊接符号；任一步失败退回带引线注释（leader_note）。符号代码可在节点配置 templates.weld_fillet_symbol 覆盖。"""
        sym = self.templates.get("weld_fillet_symbol", "<WELD-FILLET>")
        k = f"{size:g}"
        try:
            self.model.ClearSelection2(True)
            if not self.model.Extension.SelectByID2("", "EDGE", at[0], at[1], 0, False, 0, None, 0):
                raise OpFailed("no edge at weld location")
            ws = self.model.InsertWeldSymbol3()
            if ws is None:
                raise OpFailed("InsertWeldSymbol3 returned None")
            right = f"{count}处" if count > 1 else ""
            if not ws.SetText(True, k, sym, right, "", 0):
                raise OpFailed("WeldSymbol.SetText (arrow side) failed")
            if both_sides:
                ws.SetText(False, k, sym, right, "", 0)
            if all_around:
                try:
                    ws.SetPeripheral(True)
                except Exception:  # noqa: BLE001
                    pass
            if process:
                try:
                    ws.SetProcess(True, process)
                except Exception:  # noqa: BLE001
                    pass
            ws.GetAnnotation().SetPosition2(pos[0], pos[1], 0)
            self.readback["weld_symbols"] = self.readback.get("weld_symbols", 0) + 1
            return {"native": True}
        except Exception as e:  # noqa: BLE001
            self.op_leader_note(view=view, text=fallback_text, at_model=at_model, ref=ref)
            self.readback["weld_symbols"] = self.readback.get("weld_symbols", 0) + 1
            self.readback.setdefault("weld_symbol_fallback", []).append(f"{ref}: {e}")
            return {"native": False, "fallback": str(e)}

    def op_weldment_cut_list(self, view: str, anchor: str, rows, header_bottom: bool = True):
        v = self.views.get(view) if hasattr(self, "views") else None
        tpl = self.templates.get("cut_list", "")
        tb = None if v is None else v.InsertWeldmentTable(False, 0.0, 0.0, 1, tpl, "")
        if tb is None:
            raise OpFailed("InsertWeldmentTable failed (cut list in notes)")
        tbl = self._qi(tb, "ITableAnnotation")
        if header_bottom:
            try:
                tbl.SetHeader(self._k("swTableHeader_Bottom", 2), 1)
            except Exception:  # noqa: BLE001
                pass
        # 回读：每行 [序号, 长度/说明, 数量]（列名按模板：LENGTH/长度、QTY/数量）
        got = []
        try:
            nr, nc = int(tbl.RowCount or 0), int(tbl.ColumnCount or 0)
            hdr_row = nr - 1 if header_bottom else 0
            heads = [str(tbl.Text(hdr_row, c)).upper() for c in range(nc)]
            col = lambda *names: next((i for i, h in enumerate(heads) if any(n in h for n in names)), None)  # noqa: E731
            ci, cl, cq = col("ITEM", "序号", "项目"), col("LENGTH", "长度"), col("QTY", "数量")
            for r in range(nr):
                if r == hdr_row or cl is None or cq is None:
                    continue
                got.append([str(tbl.Text(r, ci)).strip() if ci is not None else "",
                            str(tbl.Text(r, cl)).strip(), str(tbl.Text(r, cq)).strip()])
        except Exception:  # noqa: BLE001
            got = []
        if got:
            self.readback["cut_list_rows"] = got
        return {"rows": len(rows), "readback_rows": len(got)}

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

    # 草图几何关系 → IModelDoc2.SketchAddConstraints 的关系 ID（字符串，SW API 帮助 "sgXXX"）
    SKETCH_RELATION = {"coincident": "sgCOINCIDENT", "same_x": "sgVERTICALPOINTS2D", "same_y": "sgHORIZONTALPOINTS2D",
                       "horizontal": "sgHORIZONTAL2D", "vertical": "sgVERTICAL2D"}

    def _sketch_pick(self, pick, append: bool) -> bool:
        """pick = [u, v, kind]（草图平面 mm）；kind: seg 草图线段/圆弧上一点 | pt 草图点 | origin 草图原点。"""
        u, v, kind = pick
        ext = self.model.Extension
        if kind == "origin":
            # TODO(实机核对): 中文版 SW 原点名可能为 "Point1@原点"；失败时退回按坐标选 EXTSKETCHPOINT
            return bool(ext.SelectByID2("Point1@Origin", "EXTSKETCHPOINT", 0, 0, 0, append, 0, None, 0)
                        or ext.SelectByID2("", "EXTSKETCHPOINT", 0, 0, 0, append, 0, None, 0))
        x, y = self._sk((u, v))
        typ = "SKETCHSEGMENT" if kind == "seg" else "SKETCHPOINT"
        return bool(ext.SelectByID2("", typ, x, y, 0, append, 0, None, 0))

    def op_dimension_profile(self, sketch: str, relations, dims):
        """外形草图完全定义：按规划（sketch_plan.profile_dimension_plan）先加关系、再加驱动尺寸，
        退出草图后把含全局变量的尺寸写成方程式 "Dn@<sketch>" = <link>，并回读草图约束状态。"""
        sk = self.model.SketchManager.ActiveSketch
        if sk is None:
            raise OpFailed("no active sketch")
        toggle = self._const(["swInputDimValOnCreate"])  # 关掉"创建尺寸时弹出修改框"
        old = None
        if toggle is not None:
            old = self.sw.GetUserPreferenceToggle(toggle)
            self.sw.SetUserPreferenceToggle(toggle, False)
        problems: list[str] = []
        names: dict[str, str] = {}
        try:
            for r in relations:
                self.model.ClearSelection2(True)
                if not all(self._sketch_pick(pk, i > 0) for i, pk in enumerate(r["pick"])):
                    problems.append(f"relation {r['kind']}: pick failed")
                    continue
                self.model.SketchAddConstraints(self.SKETCH_RELATION[r["kind"]])
            for d in dims:
                self.model.ClearSelection2(True)
                if not all(self._sketch_pick(pk, i > 0) for i, pk in enumerate(d["pick"])):
                    problems.append(f"dim {d['name']}: pick failed")
                    continue
                x, y = self._sk(d["place"])
                dd = self.model.AddDimension2(x, y, 0)
                if dd is None:
                    problems.append(f"dim {d['name']}: AddDimension2 returned None")
                    continue
                names[d["name"]] = str(dd.GetDimension2(0).Name)  # 尺寸值取当前几何（规划坐标即设计值）
            self.model.ClearSelection2(True)
            full = self._const(["swConstrainedStatus_FullyConstrained"])
            st = sk.GetConstrainedStatus()
            status = "fully" if full is not None and st == full else f"raw:{st}"
        finally:
            if toggle is not None and old is not None:
                self.sw.SetUserPreferenceToggle(toggle, bool(old))
        self._close_sketch()  # 草图改名为计划名后才能写 "D1@SK_Base"
        mgr = self.model.GetEquationMgr()
        linked = 0
        for d in dims:
            if d.get("link") and d["name"] in names:
                if mgr.Add2(-1, f'"{names[d["name"]]}@{sketch}" = {d["link"]}', True) < 0:
                    problems.append(f"equation for {d['name']} rejected")
                else:
                    linked += 1
        self.readback.setdefault("sketch_status", {})[sketch] = status
        if status != "fully":
            problems.append(f"sketch {sketch} not fully defined ({status})")
        if problems:
            raise OpFailed("; ".join(problems))
        return {"relations": len(relations), "dims": len(names), "linked": linked, "status": status}

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

    def op_sheet_metal_edge_flange(self, feature: str, edge: str, edge_point, length: float, angle: float,
                                   radius: float, direction: str = "up", expect_z=None,
                                   length_type: str = "outer_virtual_sharp"):
        """边线法兰：按底板边线中点（resolve 给出，向上翻取顶面边、向下翻取底面边）选边 →
        IFeatureManager.InsertSheetMetalEdgeFlange2（材料在内：底板外形即零件外形；长度 = 外部虚拟交点高度）。"""
        self._close_sketch()
        self.model.ClearSelection2(True)
        x, y, z = (v * MM for v in edge_point)
        if not self.model.Extension.SelectByID2("", "EDGE", x, y, z, False, 0, None, 0):
            raise OpFailed(f"edge flange {feature}: no edge at {edge_point}（{edge} 边）")
        e = self.model.SelectionManager.GetSelectedObject6(1, -1)
        if e is None:
            raise OpFailed(f"edge flange {feature}: selected edge object unavailable")
        pos = self._const(["swFlangePositionTypeMaterialInside"])
        if pos is None:
            raise OpFailed("constant swFlangePositionTypeMaterialInside not found in type library")
        opts = sum(v for v in (self._const(["swInsertEdgeFlangeUseDefaultRelief"]),) if v is not None)
        arr = self.pythoncom.VT_ARRAY | self.pythoncom.VT_DISPATCH
        edges = self.win32.VARIANT(arr, [e])
        sketches = self.win32.VARIANT(arr, [None])  # TODO(实机核对): 无自定义轮廓时传 [None] 还是 Nothing
        self.model.ClearSelection2(True)
        # TODO(实机核对): 14 个实参（FlangeEdges, SketchFeats, Options, Angle, Radius, BendPosition, Length,
        #   ReliefType, ReliefRatio, ReliefWidth, ReliefDepth, ReliefTearType, CustomBendAllowance, ByRef Errors）
        res = self.model.FeatureManager.InsertSheetMetalEdgeFlange2(
            edges, sketches, opts, math.radians(angle), radius * MM, pos, length * MM, 0, 0.5, 0.0, 0.0, 0, None, 0)
        feat, err = (res[0], res[-1]) if isinstance(res, tuple) else (res, None)
        if feat is None:
            raise OpFailed(f"InsertSheetMetalEdgeFlange2 failed for {edge} edge (errors={err})")
        feat.Name = feature
        self.features[feature] = feat
        # TODO(实机核对): 长度类型（外部虚拟交点）按 SW 默认；不是时经 feat.GetDefinition() 的 IEdgeFlangeFeatureData 设置
        return {"feature": feature, "edge": edge, "length_type": length_type}

    def op_check_body_extent(self, axis: str, expect, tol: float = 0.05):
        """建模后核对实体包围盒在某轴上的范围（mm），用于发现翻边方向/长度类型与规划不一致。"""
        bodies = self.model.GetBodies2(self._k("swSolidBody", 0), False)
        if not bodies:
            raise OpFailed("no solid body")
        box = bodies[0].GetBodyBox()
        i = "xyz".index(axis)
        got = (round(box[i] / MM, 3), round(box[i + 3] / MM, 3))
        self.readback.setdefault("extent", {})[axis] = got
        if abs(got[0] - expect[0]) > tol or abs(got[1] - expect[1]) > tol:
            raise OpFailed(f"{axis} 向范围 {got} 与规划 {tuple(expect)} 不符：检查翻边方向（上/下）与长度类型")
        return {"extent": got}

    def op_hole_wizard(self, **kw):
        raise OpFailed("hole_wizard: 需实机核对 HoleWizard5 GB 标准参数表；请用 holes_via='cut'")

    def op_cosmetic_thread(self, feature: str, size: str, depth: float | None, edge_pts=None, diameter=None,
                           external: bool = False, tol: str | None = None):
        """装饰螺纹：先选中孔口（或轴段端部）圆边，再 FeatureManager.InsertCosmeticThread3。
        edge_pts 为圆边上的一个模型坐标点（mm）列表；diameter 为螺纹公称直径（mm），缺省按 size 的 M 后数字解析。
        InsertCosmeticThread3 的实参个数/含义以实机帮助为准（见 docs/WINDOWS_TEST.md 的待核对项）。"""
        self._close_sketch()
        if not edge_pts:
            raise OpFailed("cosmetic thread: no edge points given")
        if diameter is None:
            m = re.match(r"[Mm](\d+(?:\.\d+)?)", size or "")
            if not m:
                raise OpFailed(f"cosmetic thread: cannot parse nominal diameter from {size!r}")
            diameter = float(m.group(1))
        # 终止条件：给定深度 → 盲孔；否则通孔（枚举取不到时用 0/1）
        blind = depth is not None
        end_cond = self._k("swCosmeticThreadBlind", 0) if blind else self._k("swCosmeticThreadThroughAll", 1)
        ttype = self._k("swCosmeticThreadExternal", 1) if external else self._k("swCosmeticThreadInternal", 0)
        fm = self.model.FeatureManager
        done = 0
        for i, pt in enumerate(edge_pts):
            self.model.ClearSelection2(True)
            if not self.model.Extension.SelectByID2("", "EDGE", pt[0] * MM, pt[1] * MM, pt[2] * MM, False, 0, None, 0):
                self.readback.setdefault("cosmetic_thread_errors", []).append(f"{feature}[{i}]: edge not found")
                continue
            feat = fm.InsertCosmeticThread3(ttype, diameter * MM, (depth or 0.0) * MM, end_cond,
                                            "GB", "Metric Tap" if not external else "Metric Die", size or "",
                                            tol or ("6g" if external else "6H"))
            if feat is None:
                self.readback.setdefault("cosmetic_thread_errors", []).append(f"{feature}[{i}]: InsertCosmeticThread3 returned None")
                continue
            try:
                feat.Name = f"{feature}_CT{i + 1}"
            except Exception:  # noqa: BLE001
                pass
            done += 1
        self.model.ClearSelection2(True)
        self.readback["cosmetic_threads"] = self.readback.get("cosmetic_threads", 0) + done
        if done == 0:
            raise OpFailed(f"cosmetic thread {feature}: no thread created")
        return {"count": done}

    # ---- 边选择：按拓扑/几何规则枚举边（outer_vertical / top_outer / bottom_outer） ----
    _EPS = 1e-6  # 米

    def _body_edges_for_rule(self, rule: str) -> list:
        bodies = self.model.GetBodies2(self._k("swSolidBody", 0), False)
        if not bodies:
            raise OpFailed("no solid body")
        body = bodies[0]
        box = body.GetBodyBox()
        zmin, zmax = box[2], box[5]
        eps = self._EPS

        def pt(v):
            return tuple(v.GetPoint())

        def planar_outer_loop_edges(z):
            for f in body.GetFaces() or []:
                n = f.Normal
                fb = f.GetBox()
                if abs(n[2]) > 0.999 and abs(fb[2] - z) < eps and abs(fb[5] - z) < eps:
                    for lp in f.GetLoops() or []:
                        if lp.IsOuter():
                            return list(lp.GetEdges() or [])
            return []

        if rule == "top_outer":
            return planar_outer_loop_edges(zmax)
        if rule == "bottom_outer":
            return planar_outer_loop_edges(zmin)
        if rule == "outer_vertical":
            corners = set()
            for e in planar_outer_loop_edges(zmax):
                for v in (e.GetStartVertex(), e.GetEndVertex()):
                    if v is not None:
                        p = pt(v)
                        corners.add((round(p[0] / eps), round(p[1] / eps)))
            out, seen = [], set()
            for f in body.GetFaces() or []:
                if abs(f.Normal[2]) > 0.001:
                    continue
                for e in f.GetEdges() or []:
                    sv, ev = e.GetStartVertex(), e.GetEndVertex()
                    if sv is None or ev is None:
                        continue
                    a, b = pt(sv), pt(ev)
                    if abs(a[0] - b[0]) > eps or abs(a[1] - b[1]) > eps or abs(a[2] - b[2]) < eps:
                        continue
                    key = (round(a[0] / eps), round(a[1] / eps))
                    if key in corners and key not in seen:
                        seen.add(key)
                        out.append(e)
            return out
        raise OpFailed(f"unknown edge rule {rule!r}")

    def op_select_edges(self, rule: str):
        self._close_sketch()
        edges = self._body_edges_for_rule(rule)
        if not edges:
            raise OpFailed(f"no edges matched rule {rule!r}")
        self.model.ClearSelection2(True)
        n = 0
        for e in edges:
            if e.Select4(n > 0, None):
                n += 1
        if n == 0:
            raise OpFailed(f"could not select edges for rule {rule!r}")
        self.readback.setdefault("selected_edges", {})[rule] = n
        return {"count": n}

    def op_select_edges_at(self, points):
        """按模型坐标点（mm）逐个选边（追加选择），供随后的 fillet/chamfer 使用。"""
        self._close_sketch()
        self.model.ClearSelection2(True)
        n = 0
        for x, y, z in points:
            if self.model.Extension.SelectByID2("", "EDGE", x * MM, y * MM, z * MM, n > 0, 0, None, 0):
                n += 1
        if n == 0:
            raise OpFailed(f"no edge at {points}")
        self.readback.setdefault("selected_edges", {})["points"] = n
        return {"count": n, "requested": len(points)}

    def op_fillet(self, feature: str, size: float):
        """对 select_edges 选中的边倒圆角。FeatureFillet3 实参个数见 WINDOWS_TEST 待核对项。"""
        if not self.readback.get("selected_edges"):
            raise OpFailed("skipped (edges not selected)")
        r = size * MM
        feat = self.model.FeatureManager.FeatureFillet3(195, r, r, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        return self._finish_edge_feature(feat, feature, "FeatureFillet3")

    def op_chamfer(self, feature: str, size: float):
        """对选中边倒等距 C 角：InsertFeatureChamfer(Options, Type, Width, Angle, OtherDist, V1, V2, V3)。"""
        if not self.readback.get("selected_edges"):
            raise OpFailed("skipped (edges not selected)")
        d = size * MM
        ctype = self._k("swChamferEqualDistance", 4)
        feat = self.model.FeatureManager.InsertFeatureChamfer(4, ctype, d, 0.0, d, 0.0, 0.0, 0.0)
        return self._finish_edge_feature(feat, feature, "InsertFeatureChamfer")

    def _finish_edge_feature(self, feat, name: str, api: str):
        self.readback.pop("selected_edges", None)  # 选择已被消耗
        self.model.ClearSelection2(True)
        if feat is None:
            raise OpFailed(f"{api} failed")
        feat.Name = name
        self.features[name] = feat
        return {}

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

    # ---- 命名基准：平面 / 轴 / 点（供装配配合引用） ----
    _AXIS_PLANE = {0: "Right Plane", 1: "Top Plane", 2: "Front Plane"}  # 法向轴 → 默认基准面

    @staticmethod
    def _main_axis(vec) -> int:
        """轴对齐向量 → 轴号；非轴对齐抛 OpFailed。"""
        i = max(range(3), key=lambda k: abs(vec[k]))
        if abs(vec[i]) < 0.999 * math.sqrt(sum(c * c for c in vec)):
            raise OpFailed(f"only axis-aligned directions supported in v0: {list(vec)}")
        return i

    def _make_offset_plane(self, axis: int, offset_mm: float, name: str):
        """由默认基准面偏移出新基准面（偏移为 0 时用重合约束）。约束枚举按名称查，失败用帮助里的数值。"""
        self.model.ClearSelection2(True)
        if not self._ref_planes()[self._AXIS_PLANE[axis]].Select2(False, 0):
            raise OpFailed(f"could not select {self._AXIS_PLANE[axis]}")
        if abs(offset_mm) < 1e-9:
            cons, dist = self._k("swRefPlaneReferenceConstraint_Coincident", 2), 0.0
        else:
            cons, dist = self._k("swRefPlaneReferenceConstraint_Distance", 8), offset_mm * MM
        feat = self.model.FeatureManager.InsertRefPlane(cons, dist, 0, 0, 0, 0)
        if feat is None:
            raise OpFailed("InsertRefPlane failed")
        feat.Name = name
        self.features[name] = feat
        return feat

    def op_ref_plane_coincident(self, feature: str, point, normal):
        self._close_sketch()
        axis = self._main_axis(normal)
        self._make_offset_plane(axis, point[axis], feature)
        self.model.ClearSelection2(True)
        self.readback.setdefault("named_refs", []).append(feature)
        return {}

    def op_ref_axis(self, feature: str, point, direction):
        """过 point、沿 direction 的基准轴：优先选中该处圆柱面（孔壁）取其轴；
        取不到时用过该点的两个辅助偏移面求交。"""
        self._close_sketch()
        axis = self._main_axis(direction)
        perp = [k for k in range(3) if k != axis]
        feat = None
        # 1) 孔壁圆柱面：从孔内沿轴向微移的点向垂直方向射线，命中圆柱面
        self.model.ClearSelection2(True)
        d = [0.0, 0.0, 0.0]
        d[perp[0]] = 1.0
        p = [v * MM for v in point]
        p[axis] -= 0.0001 * (1 if direction[axis] > 0 else -1)  # 微入孔内约 0.1mm
        if self.model.Extension.SelectByRay(p[0], p[1], p[2], d[0], d[1], d[2], 0.0005, self.c.swSelFACES, False, 0, 0):
            feat = self.model.InsertAxis2(True)
        if feat is None:
            # 2) 两个垂直于 perp 的辅助面求交
            a1 = self._make_offset_plane(perp[0], point[perp[0]], f"{feature}_aux1")
            a2 = self._make_offset_plane(perp[1], point[perp[1]], f"{feature}_aux2")
            self.model.ClearSelection2(True)
            a1.Select2(False, 0)
            a2.Select2(True, 0)
            feat = self.model.InsertAxis2(True)
        if feat is None:
            raise OpFailed("InsertAxis2 failed")
        feat.Name = feature
        self.features[feature] = feat
        self.model.ClearSelection2(True)
        self.readback.setdefault("named_refs", []).append(feature)
        return {}

    def op_ref_point(self, feature: str, point):
        """基准点：三个辅助偏移面的交点（InsertReferencePoint 交叉类型）；失败退回 3D 草图点并改名。"""
        self._close_sketch()
        feat = None
        try:
            planes = [self._make_offset_plane(k, point[k], f"{feature}_aux{k + 1}") for k in range(3)]
            self.model.ClearSelection2(True)
            for i, pl in enumerate(planes):
                pl.Select2(i > 0, 0)
            ptype = self._k("swRefPointIntersection", 4)
            feat = self.model.FeatureManager.InsertReferencePoint(ptype, 0, 0.0, 1)
        except OpFailed:
            feat = None
        if feat is None:
            sm = self.model.SketchManager
            self.model.ClearSelection2(True)
            sm.Insert3DSketch(True)
            sm.CreatePoint(point[0] * MM, point[1] * MM, point[2] * MM)
            sm.Insert3DSketch(True)
            feat = self.model.FeatureByPositionReverse(0)
            self.readback.setdefault("named_ref_fallback", []).append(feature)
        if feat is None:
            raise OpFailed("reference point failed")
        feat.Name = feature
        self.features[feature] = feat
        self.model.ClearSelection2(True)
        self.readback.setdefault("named_refs", []).append(feature)
        return {}

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
                           frame: dict | None, fallback: str):
        """坐标系重合配合（swMateCOORDINATE，一次约束 6 个自由度）。
        frame 非空（有 offset/rotate 或阵列副本）：先在装配中按世界位姿建参考坐标系 frame["name"]，再与组件坐标系重合。
        不用"坐标系重合 + 距离/角度配合"：坐标系配合已满约束，再加距离/角度会过定义；拆成 3 距离 + 3 角度又有翻转歧义。
        失败时退回按已放入的位姿固定组件（fixed_at_transform），oplog 记原因。"""
        comp = self.features[instance]
        asm_title = self.model.GetTitle().rsplit(".", 1)[0]
        try:
            target_feat = None
            if frame:
                o = [v * MM for v in frame["origin"]]
                r = [math.radians(v) for v in frame["rotate_deg"]]
                self.model.ClearSelection2(True)
                # TODO(实机核对): 装配环境下 CreateCoordinateSystemUsingNumericalValues 是否可用（SW 2021+），
                #   以及 X/Y/Z 旋转的顺序是否为固定轴 X→Y→Z（与 IR rotate_xyz 一致：R = Rz·Ry·Rx）
                target_feat = self.model.FeatureManager.CreateCoordinateSystemUsingNumericalValues(
                    True, o[0], o[1], o[2], True, r[0], r[1], r[2])
                if target_feat is None:
                    raise OpFailed("CreateCoordinateSystemUsingNumericalValues (assembly) failed")
                target_feat.Name = frame["name"]
                self.features[frame["name"]] = target_feat
            self.model.ClearSelection2(True)
            a = f"{ref}@{comp.Name2}@{asm_title}"
            if not self.model.Extension.SelectByID2(a, "COORDSYS", 0, 0, 0, False, 1, None, 0):
                raise OpFailed(f"cannot select {a}")
            if target_feat is not None:
                if not target_feat.Select2(True, 1):
                    raise OpFailed(f"cannot select assembly csys {frame['name']}")
            elif target_instance:
                tc = self.features[target_instance]
                b = f"{target_ref}@{tc.Name2}@{asm_title}"
                if not self.model.Extension.SelectByID2(b, "COORDSYS", 0, 0, 0, True, 1, None, 0):
                    raise OpFailed(f"cannot select {b}")
            mate = self.model.AddMate5(self.c.swMateCOORDINATE, self.c.swMateAlignALIGNED, False, 0, 0, 0, 0, 0,
                                       0, 0, 0, False, False, 0, 0)
            if mate is None:
                raise OpFailed("AddMate5 coordinate mate failed")
            return {"mate": "coordinate", "via": frame["name"] if frame else None}
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

    def op_break_view(self, view: str, breaks, scale: float, gap: float, center):
        """断开画法：每个断开区间插入一对竖直断裂线（位置 = 相对视图原点的 x，轴左端为模型原点），BreakView 后
        把视图移回规划中心（断开后 SW 以视图包围盒重新定位）。
        TODO 实机核对：swBreakLineOrientation_e / swBreakLineStyle_e 取值；Position 单位（模型 m 还是图纸 m）；
        断开间隙取文档设置 swDetailingBreakLineGap（此处设为 gap mm）。"""
        v = self.views.get(view)
        if v is None:
            raise OpFailed(f"break_view: no view {view!r}")
        c = self.c
        orient = getattr(c, "swBreakLineVertical", 1)
        style = getattr(c, "swBreakLine_Curve", 2)
        try:
            self.model.Extension.SetUserPreferenceDouble(getattr(c, "swDetailingBreakLineGap", 0), 0, gap * MM)
        except Exception:  # noqa: BLE001
            pass
        made = 0
        for a, b in breaks:
            for fn, args in (("InsertBreak3", (orient, a * MM, b * MM, style, 1, False)),
                             ("InsertBreak2", (orient, a * MM, b * MM, style, 1)),
                             ("InsertBreak", (orient, a * MM, b * MM, style))):
                try:
                    if getattr(v, fn)(*args) is not None:
                        made += 1
                        break
                except Exception:  # noqa: BLE001
                    continue
        if not made:
            raise OpFailed("InsertBreak failed")
        self.model.ClearSelection2(True)
        self.model.ActivateView(v.Name)
        self.model.Extension.SelectByID2(v.Name, "DRAWINGVIEW", 0, 0, 0, False, 0, None, 0)
        self.model.BreakView()
        self.model.ClearSelection2(True)
        try:
            v.Position = (center[0] * MM, center[1] * MM)
        except Exception:  # noqa: BLE001
            pass
        return {"breaks": made}

    def op_flat_pattern_view(self, model: str, center, scale: float):
        v = self.model.CreateFlatPatternViewFromModelView3(model, "", center[0] * MM, center[1] * MM, 0, False, False)
        if v is None:
            raise OpFailed("flat pattern view failed")
        self.views["flat_pattern"] = v
        return {}

    def op_sheet_centerlines(self, lines):
        """装配图回转件轴线：在图纸（非视图）草图中画中心线。TODO(实机核对)：EditSheet 后 CreateCenterLine 的坐标系为图纸 m。"""
        try:
            self.model.EditSheet()
        except Exception:  # noqa: BLE001
            pass
        sm = self.model.SketchManager
        n = 0
        for (x1, y1), (x2, y2) in lines:
            if sm.CreateCenterLine(x1 / 1000, y1 / 1000, 0, x2 / 1000, y2 / 1000, 0) is not None:
                n += 1
        self.model.ClearSelection2(True)
        return {"lines": n}

    def op_center_marks(self, view: str, types: int = 5, connection: int = 0):
        """孔/槽中心符号（GB：孔画十字中心线）：IView.AutoInsertCenterMarks2，尺寸、间隙、延长线用文档设置。
        types 为 swAutoInsertCenterMarkTypes_e 位掩码（孔 1 | 槽 4；不含圆角 2，免得外形圆角也加中心符号）。
        TODO 实机核对：枚举取值与 2022/2024/2026 的参数语义。"""
        v = self.views.get(view)
        if v is None:
            raise OpFailed(f"view {view} not on sheet")
        self.model.ActivateView(v.Name)
        ok = v.AutoInsertCenterMarks2(types, connection, True, True, True, 0.0, 0.0, False, False, 0.0)
        if not ok:
            raise OpFailed(f"{view}: AutoInsertCenterMarks2 返回 False")
        return {"view": view}

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

    def _dim_between_edges(self, view: str, ref: str, pts, pos, orient):
        """激活视图，在图纸坐标 pts（mm）处选中两条边线，AddDimension2 在 pos（mm）放置尺寸；返回显示尺寸对象。
        选不中边线或没生成尺寸时抛 OpFailed（上层按 NON_CRITICAL 处理）；成功的 ref 记入 readback["dim_refs"]。"""
        v = self.views.get(view)
        if v is None:
            raise OpFailed(f"view {view} not on sheet")
        if not pts or len(pts) != 2 or pos is None:
            raise OpFailed(f"{ref}: 规划层没有给出图纸坐标（P1：该视图类型暂未规划，sheet_pts/sheet_pos）")
        self.model.ActivateView(v.Name)
        self.model.ClearSelection2(True)
        for i, (x, y) in enumerate(pts):
            ok = self.model.Extension.SelectByID2("", "EDGE", x * MM, y * MM, 0, i > 0, 0, None, 0)
            if not ok:
                self.model.ClearSelection2(True)
                raise OpFailed(f"{ref}: 图纸坐标 ({x:g}, {y:g}) mm 处选不中边线")
        disp = self.model.AddDimension2(pos[0] * MM, pos[1] * MM, 0)
        if disp is None:
            raise OpFailed(f"{ref}: AddDimension2 未生成尺寸（两条边线不能标注，或不在同一视图）")
        self.model.ClearSelection2(True)
        self.readback.setdefault("dim_refs", []).append(ref)
        return disp

    def op_overall_dimension(self, view: str, axis: str, value: float, ref: str = "", pts=None, pos=None, orient=None,
                             prefix: str = "", suffix: str = ""):
        disp = self._dim_between_edges(view, ref or f"overall_{axis}", pts, pos, orient)
        if prefix or suffix:  # 等距尺寸 19×50(=950)：<DIM> 保持关联实测值
            try:
                disp.SetText(self._k("swDimensionTextAll", 0), prefix + "<DIM>" + suffix)
            except Exception:  # noqa: BLE001  文字覆盖失败只影响外观
                self.readback.setdefault("dim_text_failed", []).append(ref)
        return {"value": value}

    def op_section_cut(self, view: str, parent: str, label: str, kind: str, points, center):
        """父视图中画剖切折线（图纸坐标），选中后生成剖视图并放到 center；剖视图登记为 view（后续尺寸/注释挂在它上面）。"""
        pv = self.views.get(parent)
        if pv is None:
            raise OpFailed(f"parent view {parent} not on sheet")
        self.model.ActivateView(pv.Name)
        sm = self.model.SketchManager
        segs = []
        for a, b in zip(points, points[1:]):
            seg = sm.CreateLine(a[0] * MM, a[1] * MM, 0, b[0] * MM, b[1] * MM, 0)
            if seg is None:
                raise OpFailed("cut line CreateLine failed")
            segs.append(seg)
        self.model.ClearSelection2(True)
        for sg in segs:
            sg.Select4(True, None)
        opts = self._k("swCreateSectionView_DisplaySurfaceCut", 0) * 0
        sv = self.model.CreateSectionViewAt5(center[0] * MM, center[1] * MM, 0, label, opts, None, 0)
        if sv is None:
            raise OpFailed(f"CreateSectionViewAt5 failed（{kind}；阶梯剖需折线首尾在零件外）")
        self.views[view] = sv
        self.readback.setdefault("sections", []).append(label)
        return {"name": sv.Name, "segments": len(segs)}

    def op_broken_out_section(self, view: str, label: str, points, depth: float):
        """局部剖：在视图中画封闭轮廓（图纸坐标）、选中后 CreateBreakOutSection。"""
        v = self.views.get(view)
        if v is None:
            raise OpFailed(f"view {view} not on sheet")
        self.model.ActivateView(v.Name)
        sm = self.model.SketchManager
        ring = list(points) + [points[0]]
        segs = []
        for a, b in zip(ring, ring[1:]):
            seg = sm.CreateLine(a[0] * MM, a[1] * MM, 0, b[0] * MM, b[1] * MM, 0)
            if seg is None:
                raise OpFailed("break-out profile CreateLine failed")
            segs.append(seg)
        self.model.ClearSelection2(True)
        for sg in segs:
            sg.Select4(True, None)
        ok = self.model.CreateBreakOutSection(depth * MM)
        if not ok:
            raise OpFailed("CreateBreakOutSection failed（轮廓需封闭且在视图内）")
        self.readback.setdefault("sections", []).append(label)
        return {"segments": len(segs), "depth_mm": depth}

    def op_detail_view(self, label: str, parent: str, circle, center, scale):
        """局部放大图：父视图中画圆（图纸坐标 mm）→ 选中 → IDrawingDoc.CreateDetailViewAt4 放到 center；
        锯齿轮廓（JaggedOutline）近似 GB/T 4458.1 的波浪线边界。登记为 views["detail:<label>"]。"""
        pv = self.views.get(parent)
        if pv is None:
            raise OpFailed(f"parent view {parent} not on sheet")
        self.model.ActivateView(pv.Name)
        cx, cy, r = circle
        c = self.model.SketchManager.CreateCircleByRadius(cx * MM, cy * MM, 0, r * MM)
        if c is None:
            raise OpFailed("detail circle CreateCircleByRadius failed")
        self.model.ClearSelection2(True)
        c.Select4(False, None)
        # TODO(实机核对): CreateDetailViewAt4(X, Y, Z, Style, Scale1, Scale2, LabelIn, Showtype, FullOutline,
        #   JaggedOutline, NoOutline, ShapeIntensity)；swDetViewStyle_e / swDetCircleShowType_e 取值
        style = self._k("swDetViewSTANDARD", 0)
        show = self._k("swDetCircleCIRCLE", 0)
        dv = self.model.CreateDetailViewAt4(center[0] * MM, center[1] * MM, 0, style, float(scale[0]), float(scale[1]),
                                            label, show, False, True, False, 3)
        if dv is None:
            raise OpFailed(f"CreateDetailViewAt4 failed（局部放大 {label}）")
        self.views[f"detail:{label}"] = dv
        self.readback.setdefault("details", []).append(label)
        return {"name": dv.Name, "scale": f"{scale[0]}:{scale[1]}"}

    # 移出断面图只显示切面（断面），候选枚举名按类型库实际名称查找。TODO(实机核对): swCreateSectionViewAtOptions_e 成员名
    SURFACE_CUT_NAMES = ("swCreateSectionView_DisplayOnlySurfaceCut", "swCreateSectionView_DisplaySurfaceCut",
                         "swCreateSectionView_PartialSection")

    def op_section_view(self, label: str, parent: str, z: float, center, points=None):
        """轴类移出断面图：主视图上画垂直轴线的剖切线（图纸坐标）→ 选中 → CreateSectionViewAt5（只显示切面）
        → 解除与父视图的对齐 → 移到规划位置（断面图在主视图下方、剖切位置正下方）。登记为 views["section:<label>"]。"""
        pv = self.views.get(parent)
        if pv is None:
            raise OpFailed(f"parent view {parent} not on sheet")
        if not points or len(points) != 2:
            raise OpFailed(f"section {label}: 规划没有给出剖切线图纸坐标")
        self.model.ActivateView(pv.Name)
        (ax, ay), (bx, by) = points
        seg = self.model.SketchManager.CreateLine(ax * MM, ay * MM, 0, bx * MM, by * MM, 0)
        if seg is None:
            raise OpFailed("section line CreateLine failed")
        self.model.ClearSelection2(True)
        seg.Select4(True, None)
        opts = self._const(self.SURFACE_CUT_NAMES) or 0
        sv = self.model.CreateSectionViewAt5(center[0] * MM, center[1] * MM, 0, label[0], opts, None, 0)
        if sv is None:
            raise OpFailed(f"CreateSectionViewAt5 failed（断面 {label}）")
        moved = True
        try:  # 剖视图默认与剖切线正交对齐（会跑到主视图右侧同一高度）；断面图按 GB 可移出放置
            sv.RemoveAlignment()  # TODO(实机核对): IView.RemoveAlignment
            sv.Position = (center[0] * MM, center[1] * MM)
        except Exception:  # noqa: BLE001  移不动只影响位置，断面本身已生成
            moved = False
        self.views[f"section:{label}"] = sv
        self.readback.setdefault("sections", []).append(label)
        return {"name": sv.Name, "surface_cut_option": opts, "moved": moved}

    def _set_fit(self, disp, code: str, ref: str) -> bool:
        """尺寸公差设为"配合 + 公差"：SW 按自带 ISO 286 表算偏差并叠写在代号后（与预览一致）。
        大写为孔、小写为轴。失败返回 False，由调用方退回文字后缀。"""
        try:
            dim = disp.GetDimension2(0)
            tol = dim.Tolerance
            # TODO(实机核对): swTolType_e.swTolFITWITHTOL 的取值（常量表缺失时用 9）
            tol.Type = self._k("swTolFITWITHTOL", 9)
            ok = tol.SetFitValues(code if code[0].isupper() else "", code if code[0].islower() else "")
            return ok is not False
        except Exception:  # noqa: BLE001
            self.readback.setdefault("dim_fit_failed", []).append(ref)
            return False

    def op_shaft_dimension(self, view: str, kind: str, text: str, at, ref: str, pts=None, pos=None, orient=None,
                           arc_end=None):
        """轴类尺寸：线性（轴段长/总长/键槽长与定位）= 选两条垂直边线；直径 = 选上下两条轮廓线，
        再用文字覆盖成 Φ + 公差带（text 如 Φ20k6），不依赖公差枚举。
        断面图槽宽 6N9 同样用后缀覆盖；arc_end = [第 1 点, 第 2 点] 的圆弧条件（"max" = 量到圆的远端，用于 d−t）。"""
        disp = self._dim_between_edges(view, ref, pts, pos, orient)
        if arc_end:
            conds = {"center": ("swArcEndCondition_Center",), "min": ("swArcEndCondition_Min",),
                     "max": ("swArcEndCondition_Max",)}
            defaults = {"center": 1, "min": 2, "max": 3}  # TODO(实机核对): swArcEndCondition_e 取值
            for i, c in enumerate(arc_end):
                if not c:
                    continue
                try:  # IDisplayDimension.SetArcEndCondition(Index 1/2, Condition)
                    disp.SetArcEndCondition(i + 1, self._const(conds[c]) or defaults[c])
                except Exception:  # noqa: BLE001
                    self.readback.setdefault("dim_arc_failed", []).append(ref)
        if re.match(r"^\(\s*[0-9.]+\s*\)$", text):  # 参考尺寸（封闭链的开口环）：加括号显示
            try:
                disp.ShowParenthesis = True
            except Exception:  # noqa: BLE001
                self.readback.setdefault("dim_paren_failed", []).append(ref)
            return {"text": text, "reference": True}
        m = re.match(r"^\s*([Φφ⌀]?)\s*[0-9.]+\s*(.*)$", text)
        suffix = (m.group(2) if m else "").strip()
        prefix = "<MOD-DIAM>" if kind == "diameter" else ""
        fm = re.match(r"^([A-Za-z]{1,2}\d{1,2})\(([^/]+)/([^)]+)\)$", suffix)
        if fm and self._set_fit(disp, fm.group(1), ref):  # SW 自带配合公差：显示代号 + 上下叠写偏差
            suffix = ""
        elif fm:
            suffix = f"{fm.group(1)}({fm.group(2)}/{fm.group(3)})"
        if kind == "diameter" or suffix:
            all_ = self._k("swDimensionTextAll", 0)
            try:  # <MOD-DIAM> 为 SW 直径符号标记，<DIM> 为尺寸值；公差带作为后缀文字
                disp.SetText(all_, prefix + "<DIM>" + suffix)
            except Exception as e:  # noqa: BLE001  文字覆盖失败只影响外观，尺寸本身已生成
                self.readback.setdefault("dim_text_failed", []).append(ref)
                return {"text_override": f"failed: {e}"}
        return {"text": text}

    def op_view_line(self, view: str, points, style: str = "CENTER", weight: str = "thick", ref: str = ""):
        """视图草图中画一段直线（图纸坐标 mm），设线型/线宽（局部热处理粗点画线等）。"""
        v = self.views.get(view)
        if v is None:
            raise OpFailed(f"view {view} not on sheet")
        self.model.ActivateView(v.Name)
        (ax, ay), (bx, by) = points
        seg = self.model.SketchManager.CreateLine(ax * MM, ay * MM, 0, bx * MM, by * MM, 0)
        if seg is None:
            raise OpFailed("view line CreateLine failed")
        self.model.ClearSelection2(True)
        seg.Select4(False, None)
        styled = True
        try:  # TODO(实机核对): IModelDoc2.SetLineStyle("CENTER") 线型名；SetLineWidth 取 swLineWeights_e（粗 = swLW_THICK）
            self.model.SetLineStyle(style)
            self.model.SetLineWidth(self._k("swLW_THICK", 2) if weight == "thick" else self._k("swLW_THIN", 0))
        except Exception:  # noqa: BLE001  线型设置失败只影响外观
            styled = False
        self.model.ClearSelection2(True)
        return {"ref": ref, "styled": styled}

    def op_leader_note(self, view: str, text: str, at_model, ref: str, text_pos=None, anchor=None, angle=0):
        v = self.views.get(view)
        if v is None:
            raise OpFailed(f"view {view} not on sheet")
        self.model.ActivateView(v.Name)
        # 先选中 anchor 处的模型边线：选中实体后 InsertNote 会自动带引线并附着；选不中则退回无引线注释
        self.model.ClearSelection2(True)
        attached = False
        if anchor is not None:
            try:
                attached = bool(self.model.Extension.SelectByID2("", "EDGE", anchor[0] * MM, anchor[1] * MM, 0,
                                                                  False, 0, None, 0))
            except Exception:  # noqa: BLE001
                attached = False
        note = self.model.InsertNote(text)
        if note is None:
            raise OpFailed("InsertNote failed")
        ann = note.GetAnnotation()
        if text_pos is not None:  # 规划层排好的位置（图纸 mm）
            ann.SetPosition2(text_pos[0] * MM, text_pos[1] * MM, 0)
        else:
            pos = v.Position  # 视图中心（图纸坐标，m）
            ann.SetPosition2(pos[0] + 0.03, pos[1] + 0.02, 0)
        if angle:  # 竖写（展开图竖直折弯线旁的折弯说明）：INote.Angle 为弧度
            try:
                note.Angle = math.radians(angle)
            except Exception:  # noqa: BLE001
                self.readback.setdefault("note_angle_failed", []).append(ref)
        if attached:
            self.readback["leaders_attached"] = self.readback.get("leaders_attached", 0) + 1
        self.readback.setdefault("notes", []).append(ref)
        return {"attached": attached}

    # ---- 精度符号（GB/T 1182 / GB/T 131）：原生符号，失败时退回带引线注释 ----
    # 几何特征枚举名以实机类型库为准：按候选名依次查找，都查不到就退回注释（D019）
    GCS_NAMES = {
        "flatness": ("swGcsFLAT", "swGcsFLATNESS"), "straightness": ("swGcsSTRAIGHT", "swGcsSTRAIGHTNESS"),
        "roundness": ("swGcsCIRC", "swGcsCIRCULARITY", "swGcsROUND"), "cylindricity": ("swGcsCYL", "swGcsCYLINDRICITY"),
        "parallelism": ("swGcsPAR", "swGcsPARALLEL"), "perpendicularity": ("swGcsPERP", "swGcsPERPENDICULAR"),
        "angularity": ("swGcsANG", "swGcsANGULARITY"), "position": ("swGcsPOS", "swGcsPOSITION", "swGcsTRUE_POS"),
        "coaxiality": ("swGcsCONC", "swGcsCONCENTRICITY", "swGcsCOAX"), "symmetry": ("swGcsSYM", "swGcsSYMMETRY"),
        "circular_runout": ("swGcsCIRC_RUNOUT", "swGcsRUNOUT", "swGcsCIRCULAR_RUNOUT"),
        "total_runout": ("swGcsTOTAL_RUNOUT", "swGcsTOT_RUNOUT"),
    }

    def _const(self, names) -> int | None:
        for n in names:
            v = getattr(self.c, n, None)
            if isinstance(v, int):
                return v
        return None

    def op_probe_gdt_constants(self):
        found = {n: getattr(self.c, n) for n in dir(self.c)
                 if n.startswith(("swGcs", "swMc", "swSF", "swLeader", "swDatum")) and isinstance(getattr(self.c, n, None), int)}
        return {"constants": found}

    def _select_edge_at(self, at) -> bool:
        if at is None:
            return False
        self.model.ClearSelection2(True)
        try:
            return bool(self.model.Extension.SelectByID2("", "EDGE", at[0] * MM, at[1] * MM, 0, False, 0, None, 0))
        except Exception:  # noqa: BLE001
            return False

    def _gdt_fallback(self, view, ref, text, at_model, reason):
        self.op_leader_note(view, text, at_model, ref)
        self.readback.setdefault("gdt_fallback", []).append(ref)
        return {"fallback": reason}

    def _place(self, obj, pos):
        if obj is not None and pos is not None:
            obj.GetAnnotation().SetPosition2(pos[0] * MM, pos[1] * MM, 0)

    def op_datum_tag(self, view, ref, at, pos, fallback_text, at_model, label):
        v = self.views.get(view)
        if v is None:
            raise OpFailed(f"view {view} not on sheet")
        self.model.ActivateView(v.Name)
        attached = self._select_edge_at(at)
        tag = self.model.InsertDatumTag2()
        if tag is None:
            return self._gdt_fallback(view, ref, fallback_text, at_model, "InsertDatumTag2 failed")
        tag.SetLabel(label)
        self._place(tag, pos)
        self.readback.setdefault("gdt_native", []).append(ref)
        return {"attached": attached}

    def op_gtol(self, view, ref, at, pos, fallback_text, at_model, type, diameter, value, datums, leader):  # noqa: A002
        v = self.views.get(view)
        if v is None:
            raise OpFailed(f"view {view} not on sheet")
        gcs = self._const(self.GCS_NAMES.get(type, ()))
        if gcs is None:
            return self._gdt_fallback(view, ref, fallback_text, at_model, f"常量表中没有 {type} 的几何特征枚举")
        self.model.ActivateView(v.Name)
        attached = self._select_edge_at(at) if leader else False
        if not leader:
            self.model.ClearSelection2(True)
        g = self.model.InsertGtol()
        if g is None:
            return self._gdt_fallback(view, ref, fallback_text, at_model, "InsertGtol failed")
        mc_none = self._const(("swMcNONE", "swMcNone")) or 0
        d3 = (list(datums) + ["", "", ""])[:3]
        g.SetFrameSymbols2(1, gcs, bool(diameter), mc_none, False, mc_none, mc_none, mc_none, mc_none)
        g.SetFrameValues2(1, str(value), "", d3[0], d3[1], d3[2])
        self._place(g, pos)
        self.readback.setdefault("gdt_native", []).append(ref)
        return {"gcs": gcs, "attached": attached}

    def op_surface_finish(self, view, ref, at, pos, fallback_text, at_model, ra, leader):
        v = self.views.get(view)
        if v is None:
            raise OpFailed(f"view {view} not on sheet")
        sym = self._const(("swSFMachining_Req", "swSFMachiningReq", "swSFMACHINING_REQ"))
        if sym is None:
            return self._gdt_fallback(view, ref, fallback_text, at_model, "常量表中没有 swSFMachining_Req")
        self.model.ActivateView(v.Name)
        self._select_edge_at(at)
        lt = self._const(("swSTRAIGHT", "swLeaderStraight")) if leader else self._const(("swNO_LEADER", "swLeaderNone"))
        loc = pos or at or (0, 0)
        sf = self.model.Extension.InsertSurfaceFinishSymbol3(
            sym, lt or 0, loc[0] * MM, loc[1] * MM, 0, self._const(("swSFNone_Lay", "swSFLayNone")) or 0,
            self._const(("swFILLED_ARROWHEAD", "swArrowFilled")) or 0, "", "", "", "", f"Ra {ra:g}", "", "")
        if not sf:
            return self._gdt_fallback(view, ref, fallback_text, at_model, "InsertSurfaceFinishSymbol3 failed")
        self.readback.setdefault("gdt_native", []).append(ref)
        return {}

    def op_hole_tags(self, view: str, tags):
        v = self.views.get(view)
        if v is None:
            raise OpFailed(f"view {view} not on sheet")
        self.model.ActivateView(v.Name)
        n = 0
        for t in tags:
            note = self.model.InsertNote(t["text"])
            if note is None:
                continue
            note.GetAnnotation().SetPosition2(t["at"][0] * MM, t["at"][1] * MM, 0)
            n += 1
        if n < len(tags):
            raise OpFailed(f"孔号注释只插入 {n}/{len(tags)}")
        return {"tags": n}

    def _set_cell(self, tbl, r: int, c: int, text: str) -> None:
        """ITableAnnotation.Text(row, col) 是带参数的属性：晚绑定下用 PROPERTYPUT 写入。"""
        try:
            import pythoncom
            ole = tbl._oleobj_
            ole.Invoke(ole.GetIDsOfNames("Text"), 0, getattr(pythoncom, "DISPATCH_PROPERTYPUT", 4), 0, r, c, text)
        except ImportError:  # 非 Windows（假对象测试）：只记录调用
            tbl.Text(r, c)

    def op_revision_table(self, origin, columns, widths, rows, fallback_text: str):
        """原生修订表：InsertRevisionTable2 → AddRevision 逐行 → 写单元格；任一步失败退回注释。
        行序 = rows 顺序（最新在上，同 render_svg），回读非空行数写 readback["revision_rows"]。"""
        try:
            anchor = self._k("swBOMConfigurationAnchor_BottomLeft", 3)
            rev = self.model.InsertRevisionTable2(False, origin[0] * MM, origin[1] * MM, anchor, "")
            if rev is None:
                raise OpFailed("InsertRevisionTable2 failed")
            rev = self._qi(rev, "IRevisionTableAnnotation")
            tbl = self._qi(rev, "ITableAnnotation")
            for i, r in enumerate(rows, 1):
                rev.AddRevision(str(r[0]))
                for c, val in enumerate(r):
                    self._set_cell(tbl, i, c, str(val))
            got = 0
            for i in range(1, len(rows) + 1):
                if any(str(tbl.Text(i, c)).strip() for c in range(len(columns))):
                    got += 1
            self.readback["revision_rows"] = got
            return {"rows": len(rows), "read": got}
        except Exception as e:  # noqa: BLE001
            note = self.model.InsertNote(fallback_text)
            if note is not None:
                note.GetAnnotation().SetPosition2(origin[0] * MM, (origin[1] + 8) * MM, 0)
            self.readback.setdefault("revision_fallback", []).append(f"{type(e).__name__}: {e}")
            return {"fallback": True}

    def op_hole_table(self, box, columns, widths, rows):
        x0, y0 = box[0] * MM, box[1] * MM
        tbl = self.model.InsertTableAnnotation2(False, x0, y0, self._k("swBOMConfigurationAnchor_BottomLeft", 3), "",
                                                len(rows) + 1, len(columns))
        if tbl is None:
            raise OpFailed("InsertTableAnnotation2 failed")
        for c, (name, w) in enumerate(zip(columns, widths)):
            self._set_cell(tbl, 0, c, name)
            try:
                tbl.SetColumnWidth(c, w * MM, 0)
            except Exception:  # noqa: BLE001
                pass
        for r, row in enumerate(rows, 1):
            for c, val in enumerate(row):
                self._set_cell(tbl, r, c, str(val))
        got = []
        for r in range(1, len(rows) + 1):  # 回读（D017）
            cells = [str(tbl.Text(r, c)).strip() for c in range(3)]
            if re.fullmatch(r"[A-Z]+\d+", cells[0]):
                got.append(cells)
        if got:
            self.readback["hole_table_rows"] = got
        note = self.model.InsertNote("孔表")
        if note is not None:
            note.GetAnnotation().SetPosition2(x0, (box[3] + 2) * MM, 0)
        return {"rows": len(rows), "read": len(got)}

    def op_warn(self, message: str):
        """规划层已知的缺项（不影响出图）：记入 readback 供 check_drawing 汇报。"""
        self.readback.setdefault("warnings", []).append(message)
        return {"warning": message}

    def op_table(self, origin, columns, widths, rows, title: str = ""):
        """通用表格（齿轮参数表等）：origin = 左上角（图纸 mm），表头在上；逐格写入并回读。"""
        x0, y0 = origin[0] * MM, origin[1] * MM
        # TODO(实机核对): swBOMConfigurationAnchor_TopLeft 取值（缺省 1）
        tbl = self.model.InsertTableAnnotation2(False, x0, y0, self._k("swBOMConfigurationAnchor_TopLeft", 1), "",
                                                len(rows) + 1, len(columns))
        if tbl is None:
            raise OpFailed("InsertTableAnnotation2 failed")
        for c, (name, w) in enumerate(zip(columns, widths)):
            self._set_cell(tbl, 0, c, name)
            try:
                tbl.SetColumnWidth(c, w * MM, 0)
            except Exception:  # noqa: BLE001
                pass
        for r, row in enumerate(rows, 1):
            for c, val in enumerate(row):
                self._set_cell(tbl, r, c, str(val))
        got = [[str(tbl.Text(r, c)).strip() for c in range(len(columns))] for r in range(1, len(rows) + 1)]
        self.readback.setdefault("tables", []).append({"title": title, "rows": got})
        return {"rows": len(rows)}

    def op_note(self, text: str, anchor: str, at: list[float] | None = None):
        """at：注释左上角（图纸 mm，规划层给出，与 SVG 同位置）；缺省时退回标题栏上方的旧规则。"""
        note = self.model.InsertNote(text)
        if note is None:
            raise OpFailed("InsertNote failed")
        if at is not None:
            note.GetAnnotation().SetPosition2(at[0] / 1000, at[1] / 1000, 0)
        else:
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
        for r in data_rows():  # 标准件：代号/名称/材料栏写规划显示值（标准号、名称规格、性能等级）
            it = item_of(r)
            w = want.get(it) if it is not None else None
            if not w or not w.get("override"):
                continue
            for col, key in (("代号", "part_no"), ("名称", "title"), ("材料", "material")):
                if col not in ci:
                    continue
                try:  # TODO(实机核对)：ITableAnnotation.Text 为带参属性，pywin32 动态绑定下的写法
                    tbl.SetText(r, ci[col], str(w[key]))
                except Exception:  # noqa: BLE001
                    try:
                        tbl.Text[r, ci[col]] = str(w[key])
                    except Exception as e:  # noqa: BLE001
                        notes.append(f"override row {r} {col}: {e}")
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
