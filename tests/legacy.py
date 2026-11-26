"""模拟公司现有的旧模型（mock 文件，native 段模拟 CAD 内部特征树/尺寸/方程式）。"""
import json
from pathlib import Path

HDR = {"magic": "cadmcp-mock", "cad": "solidworks", "version": "2024"}


def write_legacy(d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    part = {**HDR, "kind": "part", "name": "Bracket", "native": {
        "features": [
            {"name": "Sketch1", "type": "ProfileFeature", "suppressed": False},
            {"name": "Boss-Extrude1", "type": "Extrusion", "suppressed": False, "parents": ["Sketch1"]},
            {"name": "Sketch2", "type": "ProfileFeature", "suppressed": False},
            {"name": "Cut-Extrude1", "type": "Cut", "suppressed": False, "parents": ["Sketch2", "Boss-Extrude1"]},
            {"name": "Fillet1", "type": "Fillet", "suppressed": False, "parents": ["Boss-Extrude1"]}],
        "dimensions": [
            {"name": "D1@Sketch1", "value": 120, "unit": "mm", "feature": "Sketch1"},
            {"name": "D2@Sketch1", "value": 80, "unit": "mm", "feature": "Sketch1"},
            {"name": "D1@Boss-Extrude1", "value": 10, "unit": "mm", "feature": "Boss-Extrude1"},
            {"name": "D1@Sketch2", "value": 9, "unit": "mm", "feature": "Sketch2"},
            {"name": "D3@Sketch2", "value": 80, "unit": "mm", "feature": "Sketch2"},
            {"name": "D1@Fillet1", "value": 5, "unit": "mm", "feature": "Fillet1"}],
        "variables": [{"name": "pitch", "value": 80}],
        "equations": [{"lhs": "D3@Sketch2", "rhs": '"pitch"'}],
        "constraints": [{"lt": ["D3@Sketch2", "D1@Sketch1"], "feature": "Cut-Extrude1"}],
        "properties": {"PartNo": "OLD-001", "Description": "支架"},
        "material": "Q235", "mass_kg": 0.75, "bbox_mm": [120, 80, 10],
        "mass_dims": ["D1@Sketch1", "D2@Sketch1", "D1@Boss-Extrude1"]}}
    drw = {**HDR, "kind": "drawing", "name": "Bracket_DRW", "native": {
        "references": ["Bracket.mprt"],
        "annotations": ["D1@Sketch1", "D2@Sketch1", "D1@Boss-Extrude1", "D3@Sketch2", "D1@Fillet1"]}}
    cyl = {**HDR, "kind": "part", "name": "CYL-SC32x50", "native": {
        "features": [], "dimensions": [], "cs": ["CS_Mount", "CS_RodEnd"], "material": "AL6061-T6",
        "mass_kg": 0.62, "bbox_mm": [48, 48, 196], "properties": {"PartNo": "CYL-SC32x50"}}}
    asm = {**HDR, "kind": "assembly", "name": "Station", "native": {
        "references": ["CYL-SC32x50.mprt", "Bracket.mprt"],
        "components": [{"instance": "CYL-SC32x50-1", "file": "CYL-SC32x50.mprt", "code": "CYL-SC32x50", "mass_kg": 0.62},
                       {"instance": "Bracket-1", "file": "Bracket.mprt", "code": "Bracket", "mass_kg": 0.75}],
        "mates": [{"name": "Coincident1", "a": "CYL-SC32x50-1", "a_ref": "CS_Mount", "b": "Bracket-1", "b_ref": "CS_Cyl"}],
        "mass_kg": 1.37}}
    (d / "CYL-SC32x50.mprt").write_text(json.dumps(cyl, ensure_ascii=False), encoding="utf-8")
    (d / "Station.masm").write_text(json.dumps(asm, ensure_ascii=False), encoding="utf-8")
    (d / "Bracket.mprt").write_text(json.dumps(part, ensure_ascii=False), encoding="utf-8")
    (d / "Bracket.mdrw").write_text(json.dumps(drw, ensure_ascii=False), encoding="utf-8")
    return d
