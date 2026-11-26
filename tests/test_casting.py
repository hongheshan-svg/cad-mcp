import pytest
from cast_bracket import cast_base

from cadmcp import casting
from cadmcp.errors import CadError
from cadmcp.ir import DrawingSpec, PartSpec
from cadmcp.ir.resolve import resolve_part


def test_blank_derivation():
    spec = PartSpec.model_validate(cast_base())
    blank, info = casting.blank_spec(spec)
    assert blank["base"]["thickness"] == 36 and blank["part_no"] == "CB-01-M"
    holes = {f["name"]: f for f in blank["features"] if f["feature"] == "hole"}
    assert list(holes) == ["Bore_CAST"] and holes["Bore_CAST"]["size"] == 54    # M12 过孔 < Φ25 不铸出
    pk = next(f for f in blank["features"] if f["feature"] == "pocket")
    assert pk["depth"] == 12 and pk["profile"]["width"] == 114                  # 底面加工：铸出深 = 12+3-3
    assert blank["references"][0] == {"name": "CS_Finished", "origin": [0, 0, 3.0]}
    b = resolve_part(PartSpec.model_validate(blank))
    assert b.casting["blank"] and b.mass_kg > resolve_part(spec).mass_kg


def test_casting_rules():
    from cadmcp import rules  # noqa: F401
    d = cast_base()
    d["base"]["thickness"] = 5
    d["casting"]["machined_faces"] = ["top"]
    spec = PartSpec.model_validate(d)
    r = {f["rule"] for f in casting.check(spec, resolve_part(spec))}
    assert {"C001", "C002", "C004"} <= r          # 壁厚 5 < HT200 6；底面有基准/平面度却未加工
    d = cast_base()
    d["material"] = "Q235"
    spec = PartSpec.model_validate(d)
    assert any(f["rule"] == "C005" for f in casting.check(spec, resolve_part(spec)))
    with pytest.raises(Exception):
        PartSpec.model_validate({**cast_base(), "casting": None})


def test_casting_build_and_drawings(svc):
    svc.change_begin("CB")
    r = svc.build(PartSpec.model_validate(cast_base()))
    assert r["blank"]["name"] == "CB1_BLANK" and r["blank"]["drilled_after_casting"] == ["Bolt（4×Φ13.5 通）"]
    d = svc.build(DrawingSpec.model_validate({"model": "CB1", "title_block": {"designer": "AI"}}))
    assert d["rules"]["passed"] and d["blank_drawing"]["rules"]["passed"]
    part_plan = svc._get("CB1_DRW").resolved
    blank_plan = svc._get("CB1_BLANK_DRW").resolved
    assert part_plan.title_block["surface_finish"] == "cast" and any("CB-01-M" in n for n in part_plan.notes)
    assert not any("1804" in n for n in blank_plan.notes) and any("余量 3（底面、顶面）" in n for n in blank_plan.notes)
    assert svc._gates()["every_part_has_drawing"]
    # 改零件 → 毛坯和两张图跟着更新
    d2 = cast_base()
    d2["base"]["thickness"] = 40
    r = svc.build(PartSpec.model_validate(d2))
    assert svc._get("CB1_BLANK").resolved.bbox.size[2] == 46
    dims = {d.ref: d.text for d in svc._get("CB1_BLANK_DRW").resolved.dims}
    assert dims["overall_z"] == "46"  # 毛坯图跟随更新


def test_casting_unsupported_profile():
    d = cast_base()
    d["base"]["profile"] = {"shape": "circle", "diameter": 200}
    d["features"] = []
    d["casting"]["machined_faces"] = ["left"]
    with pytest.raises(CadError):
        casting.blank_spec(PartSpec.model_validate(d))
