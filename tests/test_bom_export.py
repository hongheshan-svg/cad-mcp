import csv
import zipfile

from cast_bracket import cast_base
from cylinder_station import ASSEMBLY, BASE_PLATE, MOUNT_PLATE
from weld_base import weld_base

from cadmcp.ir import AssemblySpec, DrawingSpec, PartSpec


def _top(svc, tmp_path, code_map=None):
    svc.change_begin("BOM")
    for d in (BASE_PLATE, MOUNT_PLATE, weld_base(), cast_base()):
        svc.build(PartSpec.model_validate(d))
    svc.build(AssemblySpec.model_validate(ASSEMBLY))
    top = {"name": "M01-000_Machine", "part_no": "M01-000", "title": "推料机",
           "components": [{"id": "frame", "source": f"part:{weld_base()['name']}"},
                          {"id": "st1", "source": f"asm:{ASSEMBLY['name']}"},
                          {"id": "st2", "source": f"asm:{ASSEMBLY['name']}", "place": {"offset": [400, 0, 0]}},
                          {"id": "cb", "source": "part:CB1", "place": {"offset": [0, 400, 0]}}],
           "bom_extras": [{"source": "lib:SHCS-M8x16", "qty": 8, "note": "机架固定"}]}
    svc.build(AssemblySpec.model_validate(top))
    for n in (BASE_PLATE["name"], MOUNT_PLATE["name"], "CB1"):
        svc.build(DrawingSpec.model_validate({"model": n}))
    if code_map:
        p = tmp_path / "codes.csv"
        p.write_text("图号,物料编码\n" + "".join(f"{k},{v}\n" for k, v in code_map.items()), encoding="utf-8-sig")
        svc.policy.bom_code_map = p
    return top


def test_multilevel_summary_purchase_raw(svc, tmp_path):
    top = _top(svc, tmp_path)
    r = svc.export_bom(top["name"])
    assert r["files"][0].endswith(".xlsx")
    with zipfile.ZipFile(r["files"][0]) as z:
        assert len([n for n in z.namelist() if n.startswith("xl/worksheets/")]) == 5
        assert "多级BOM" in z.read("xl/workbook.xml").decode()
    ml = {x["seq"]: x for x in r["multilevel"]}
    assert ml[""]["part_no"] == "M01-000"
    st = [x for x in r["multilevel"] if x["part_no"] == "CS01-000"]
    assert len(st) == 1 and st[0]["qty"] == 2                       # 两个相同子装配合并为一行，用量 2
    cyl = next(x for x in r["bom"] if x["part_no"].startswith("CYL-SC32x50"))
    assert cyl["total_qty"] == 2                                     # 每个工位 1 个 × 2
    screw = next(x for x in r["bom"] if x["part_no"].startswith("SHCS-M6x20"))
    assert screw["total_qty"] == 4
    assert any(x["part_no"] == "CB-01-M" and x["make_buy"] == "外协" for x in r["purchase"])  # 铸件毛坯外协
    forms = {x["form"] for x in r["raw_material"]}
    assert {"板材", "型材"} <= forms                                  # 机加工板 + 焊接件型材
    base_plate = next(x for x in r["raw_material"] if BASE_PLATE["part_no"] in x["parts"])
    assert base_plate["qty"] == 2                                    # 两个工位各一块底板
    rules = {f["rule"] for f in r["findings"]}
    assert "B002" in rules                                           # 焊接机架/总装没出图


def test_erp_profile_and_code_map(svc, tmp_path):
    top = _top(svc, tmp_path, code_map={"M01-000": "1.01.0001", BASE_PLATE["part_no"]: "1.02.0007"})
    r = svc.export_bom(top["name"], profile="erp", fmt="csv")
    rows = list(csv.reader(open(r["files"][0], encoding="utf-8-sig")))
    assert rows[0][:3] == ["父项物料编码", "子项物料编码", "子项名称"]
    assert all(row[0] for row in rows[1:])                           # erp 格式不含根行，每行都有父项
    bp = next(row for row in rows if row[1] == "1.02.0007")
    assert bp[0] == "CS01-000"                                       # 父项（未建档）用规则生成的编码
    assert any(row[0] == "1.01.0001" for row in rows)
    assert any(f["rule"] == "B005" for f in r["findings"])
    assert {p.name for p in map(__import__("pathlib").Path, r["files"])} >= {"M01-000_Machine_BOM.csv",
                                                                             "M01-000_Machine_purchase.csv"}


def test_custom_profile_rejects_unknown_field(svc, tmp_path):
    import pytest

    from cadmcp.errors import CadError
    top = _top(svc, tmp_path)
    svc.policy.bom_profiles = {"mine": {"columns": [["编码", "code"], ["X", "nope"]]}}
    with pytest.raises(CadError):
        svc.export_bom(top["name"], profile="mine")
    svc.policy.bom_profiles = {"mine": {"columns": [["编码", "code"], ["数量", "total_qty"]]}}
    assert svc.export_bom(top["name"], profile="mine")["ok"]


def test_bom_follows_drawing_item_numbers(svc, tmp_path):
    top = _top(svc, tmp_path)
    svc.build(DrawingSpec.model_validate({"model": ASSEMBLY["name"]}))
    plan = svc._get(ASSEMBLY["name"] + "_DRW").resolved
    drawing = {r["part_no"]: r["item"] for r in plan.bom}
    r = svc.export_bom(top["name"])
    station = next(x for x in r["multilevel"] if x["part_no"] == "CS01-000")
    kids = [x for x in r["multilevel"] if x["seq"].startswith(station["seq"] + ".") and x["seq"].count(".") == 1]
    for x in kids:  # 工位下一层的序号 = 工位装配图上的序号
        assert x["seq"] == f"{station['seq']}.{drawing[x['part_no']]}"
    assert [int(x["seq"].split(".")[1]) for x in kids] == sorted(drawing.values())
    cyl = next(x for x in r["bom"] if x["part_no"].startswith("CYL-SC32x50"))
    assert f"CS01-000（序号{drawing[cyl['part_no']]}）" in cyl["note"]
