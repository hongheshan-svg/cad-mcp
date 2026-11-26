"""Creo 修改现有模型：用假 creopyson Client 跑 inspect → 改尺寸 → 压缩 → 再生 → 保存（任意平台）。

假 Client 的方法名与参数名与 creopyson 0.7.x 真实 API 一致（见 FakeCreoClient 中的签名）；
调用都记录在 calls 里，用来核对"改完才保存、失败不保存"等时序。
"""
import copy
import sys
import types
from pathlib import Path

import pytest

from cadmcp.adapters.creo.adapter import CreoAdapter
from cadmcp.adapters.creo.edit import CreoEditor
from cadmcp.errors import CAPABILITY_UNSUPPORTED, CadError
from cadmcp.gateway.service import CadService, Target

# 模型单位 inch：平台 mm 与 Creo 内部值要换算
PART = {
    "length_units": "inch", "mass_units": "lbm", "mass": 2.0, "material": "STEEL",
    "dims": {"d1": 1.0, "d2": 2.0, "d3": 30.0, "rd9": 5.0},  # d3 角度；rd9 参考尺寸
    "dim_types": {"d3": "angular"},
    "features": [{"name": "EXT1", "feat_id": 1, "feat_type": "PROTRUSION", "status": "ACTIVE"},
                 {"name": "HOLE1", "feat_id": 2, "feat_type": "HOLE", "status": "ACTIVE"}],
    "params": [{"name": "PITCH", "type": "DOUBLE", "value": 80.0},
               {"name": "PARTNO", "type": "STRING", "value": "P-001"}],
    "relations": ["/* 注释 */", "d2 = d1*2"],
}
ASM = {"length_units": "mm", "mass_units": "kg", "mass": 1.5, "dims": {}, "features": [], "params": [],
       "components": ["bracket.prt", "screw.prt"]}
DRW = {"models": ["bracket.prt"], "dims": {}, "features": [], "params": []}


class FakeCreoClient:
    """内存里的 CREOSON：disk 是已保存状态，session 是会话中（可能含未保存修改）的模型。"""

    def __init__(self, fail_regen: bool = False):
        self.disk = {"bracket.prt": copy.deepcopy(PART), "line.asm": copy.deepcopy(ASM),
                     "bracket.drw": copy.deepcopy(DRW)}
        self.session: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.fail_regen = fail_regen

    def _rec(self, fn, **kw):
        self.calls.append((fn, kw))

    def _m(self, file_):
        return self.session[file_]

    def creo_cd(self, dirname):
        self._rec("creo_cd", dirname=dirname)

    def file_open(self, file_, dirname=None, generic=None, display=None, activate=None, new_window=None,
                  regen_force=None):
        self._rec("file_open", file_=file_, display=display, activate=activate)
        self.session.setdefault(file_, copy.deepcopy(self.disk[file_]))
        return {"files": [file_]}

    def file_get_length_units(self, file_=None):
        return self._m(file_).get("length_units", "mm")

    def file_get_mass_units(self, file_=None):
        return self._m(file_).get("mass_units", "kg")

    def feature_list(self, file_=None, name=None, status=None, type_=None, paths=None, no_datum=None,
                     inc_unnamed=None, no_comp=None):
        return copy.deepcopy(self._m(file_)["features"])

    def dimension_list_detail(self, name=None, file_=None, dim_type=None, encoded=None, select=False):
        m = self._m(file_)
        return [{"name": k, "value": v, "dwg_dim": False, "dim_type": m.get("dim_types", {}).get(k, "linear")}
                for k, v in m["dims"].items()]

    def dimension_list(self, name=None, file_=None, dim_type=None, encoded=None, select=False):
        return [{"name": k, "value": v, "dwg_dim": False} for k, v in self._m(file_)["dims"].items()]

    def dimension_set(self, name, value, file_=None, encoded=None):
        self._rec("dimension_set", name=name, value=value, file_=file_)
        self._m(file_)["dims"][name] = value

    def parameter_list(self, name=None, file_=None, encoded=None, value=None):
        return copy.deepcopy(self._m(file_)["params"])

    def parameter_set(self, name, value=None, file_=None, type_=None, encoded=None, designate=None,
                      description=None, no_create=None):
        self._rec("parameter_set", name=name, value=value, file_=file_, type_=type_, no_create=no_create)
        for p in self._m(file_)["params"]:
            if p["name"] == name:
                p["value"] = value
                return
        if no_create:
            raise Warning(f"parameter {name} not found")
        self._m(file_)["params"].append({"name": name, "type": type_ or "STRING", "value": value})

    def file_relations_get(self, file_=None):
        return self._m(file_).get("relations", [])

    def file_massprops(self, file_=None):
        m = self._m(file_)
        return {"mass": m["mass"] * (1 + m["dims"].get("d1", 1.0) / 100)}  # 质量随 d1 微变，验证 after 重读

    def file_get_cur_material(self, file_=None):
        return self._m(file_).get("material")

    def file_set_cur_material(self, material, file_=None):
        self._rec("file_set_cur_material", material=material, file_=file_)
        self._m(file_)["material"] = material

    def bom_get_paths(self, file_=None, paths=None, skeletons=None, top_level=None, get_transforms=None,
                      exclude_inactive=None, get_simpreps=None):
        kids = [{"file": f, "seq_path": f"root.{i + 1}"} for i, f in enumerate(self._m(file_).get("components", []))]
        return {"file": file_, "children": {"file": file_, "seq_path": "root", "children": kids}}

    def drawing_list_models(self, model=None, drawing=None):
        return self._m(drawing)["models"]

    def feature_suppress(self, file_=None, name=None, status=None, type_=None, clip=None, with_children=None):
        self._rec("feature_suppress", file_=file_, name=name)
        self._set_status(file_, name, "SUPPRESSED")

    def feature_resume(self, file_=None, name=None, status=None, type_=None, with_children=None):
        self._rec("feature_resume", file_=file_, name=name)
        self._set_status(file_, name, "ACTIVE")

    def _set_status(self, file_, name, status):
        for f in self._m(file_)["features"]:
            if f["name"] == name:
                f["status"] = status
                return
        raise Warning(f"feature {name} not found")

    def file_regenerate(self, file_=None, display=None):
        self._rec("file_regenerate", file_=file_)
        if self.fail_regen:
            raise Warning("Regeneration failed: feature EXT1")

    def file_save(self, file_=None):
        self._rec("file_save", file_=file_)
        self.disk[file_] = copy.deepcopy(self.session[file_])

    def file_erase(self, file_=None, erase_children=None):
        self._rec("file_erase", file_=file_)
        if file_:
            self.session.pop(file_, None)
        else:
            self.session.clear()


def make_adapter(client: FakeCreoClient) -> CreoAdapter:
    ad = CreoAdapter.__new__(CreoAdapter)
    ad.version, ad.c = "10.0", client
    ad.ext = {"part": ".prt", "assembly": ".asm", "drawing": ".drw"}
    ad.start_part = ad.udf_dir = ad.library_root = ""
    ad.editor = CreoEditor(client)
    return ad


@pytest.fixture
def creo(tmp_path):
    client = FakeCreoClient()
    return make_adapter(client), client, tmp_path


def names(client):
    return [c[0] for c in client.calls]


def test_inspect_structure_and_units(creo):
    ad, client, tmp = creo
    info = ad.inspect(tmp / "bracket.prt")
    dims = {d["name"]: d for d in info["dimensions"]}
    assert dims["d1"]["value"] == pytest.approx(25.4) and dims["d1"]["unit"] == "mm"   # inch → mm
    assert dims["d3"] == {"name": "d3", "value": 30.0, "unit": "deg", "feature": None, "driven": False}
    assert dims["rd9"]["driven"] is True
    assert [(f["name"], f["suppressed"]) for f in info["features"]] == [("EXT1", False), ("HOLE1", False)]
    assert info["variables"][0]["name"] == "PITCH" and info["properties"] == [{"name": "PARTNO", "value": "P-001"}]
    assert info["equations"] == [{"lhs": "d2", "rhs": "d1*2", "index": 0}]
    assert info["kind"] == "part" and info["material"] == "STEEL"
    assert info["mass_kg"] == pytest.approx(2.0 * 1.01 * 0.45359237, abs=1e-4)  # lbm → kg


def test_inspect_unknown_units_warns(creo):
    ad, client, tmp = creo
    client.disk["bracket.prt"]["length_units"] = ""
    info = ad.inspect(tmp / "bracket.prt")
    assert any("长度单位" in w for w in info["warnings"])
    assert {d["name"]: d for d in info["dimensions"]}["d1"]["value"] == 1.0  # 按 mm


def test_assembly_inspect_and_references(creo):
    ad, client, tmp = creo
    info = ad.inspect(tmp / "line.asm")
    assert info["kind"] == "assembly" and [c["file"] for c in info["components"]] == ["bracket.prt", "screw.prt"]
    assert ad.references(tmp / "line.asm") == ["bracket.prt", "screw.prt"]
    assert ad.references(tmp / "bracket.drw") == ["bracket.prt"]
    assert ad.references(tmp / "bracket.prt") is None


def test_where_used_default(creo):
    ad, client, tmp = creo
    for n in ("bracket.prt", "line.asm", "bracket.drw", "other.asm"):
        (tmp / n).write_text("x")
    client.disk["other.asm"] = {**copy.deepcopy(ASM), "components": ["screw.prt"]}
    assert [q.name for q in ad.where_used(tmp / "bracket.prt", tmp)] == ["bracket.drw", "line.asm"]


def test_apply_edits_flow_and_order(creo):
    from cadmcp.edit import parse_edits
    ad, client, tmp = creo
    edits = parse_edits([{"op": "set_dimension", "name": "d1", "value": 50.8},
                         {"op": "set_dimension", "name": "d3", "value": 45},
                         {"op": "set_variable", "name": "PITCH", "value": 90},
                         {"op": "set_property", "name": "PARTNO", "value": "P-002"},
                         {"op": "suppress", "feature": "HOLE1", "suppressed": True},
                         {"op": "set_material", "material": "ALUMINUM"}])
    res = ad.apply_edits(tmp / "bracket.prt", edits)
    assert res["regen"]["status"] == "ok"
    sets = {c[1]["name"]: c[1]["value"] for c in client.calls if c[0] == "dimension_set"}
    assert sets == {"d1": pytest.approx(2.0), "d3": 45}           # mm → inch；角度不换算
    assert ("parameter_set", {"name": "PITCH", "value": 90, "file_": "bracket.prt", "type_": None, "no_create": True}) in client.calls
    assert any(c[0] == "parameter_set" and c[1]["type_"] == "STRING" for c in client.calls)
    # 时序：全部修改 → 再生 → 保存 → 重新读取
    seq = [n for n in names(client) if n in ("dimension_set", "feature_suppress", "file_regenerate", "file_save")]
    assert seq == ["dimension_set", "dimension_set", "feature_suppress", "file_regenerate", "file_save"]
    saved = client.disk["bracket.prt"]
    assert saved["dims"]["d1"] == pytest.approx(2.0) and saved["features"][1]["status"] == "SUPPRESSED"
    after = res["after"]
    assert {f["name"]: f["suppressed"] for f in after["features"]}["HOLE1"] is True
    assert after["material"] == "ALUMINUM"


def test_resume_feature(creo):
    from cadmcp.edit import parse_edits
    ad, client, tmp = creo
    client.disk["bracket.prt"]["features"][1]["status"] = "SUPPRESSED"
    ad.apply_edits(tmp / "bracket.prt", parse_edits([{"op": "suppress", "feature": "HOLE1", "suppressed": False}]))
    assert "feature_resume" in names(client) and client.disk["bracket.prt"]["features"][1]["status"] == "ACTIVE"


def test_regen_failure_does_not_save(tmp_path):
    from cadmcp.edit import parse_edits
    client = FakeCreoClient(fail_regen=True)
    ad = make_adapter(client)
    res = ad.apply_edits(tmp_path / "bracket.prt", parse_edits([{"op": "set_dimension", "name": "d1", "value": 50.8}]))
    assert res["regen"]["status"] == "errors" and "Regeneration failed" in res["regen"]["errors"][0]
    assert "file_save" not in names(client)
    assert client.disk["bracket.prt"]["dims"]["d1"] == 1.0           # 磁盘未变
    assert "bracket.prt" not in client.session                        # 内存中的脏模型已清出


def test_replace_component_unsupported(creo):
    from cadmcp.edit import parse_edits
    ad, client, tmp = creo
    with pytest.raises(CadError) as ei:
        ad.apply_edits(tmp / "line.asm", parse_edits([{"op": "replace_component", "instance": "root.1", "source": "x.prt"}]))
    assert ei.value.code == CAPABILITY_UNSUPPORTED
    assert ad.retarget(tmp / "line.asm", {}) == 0 and ad.capabilities()["replace_component"] == "none"


def test_refresh_assembly(creo):
    ad, client, tmp = creo
    r = ad.refresh(tmp / "line.asm", preload=[tmp / "bracket.prt"])
    assert r == {"status": "ok", "dangling": 0, "exports": []}
    seq = [(c[0], c[1].get("file_")) for c in client.calls if c[0] in ("file_open", "file_regenerate", "file_save")]
    assert seq == [("file_open", "bracket.prt"), ("file_open", "line.asm"),
                   ("file_regenerate", "line.asm"), ("file_save", "line.asm")]


def test_create_adapter_real_ctor_with_fake_module(monkeypatch):
    """构造函数走真实路径：注入假 creopyson 模块，核对 Client 参数名与连接序列。"""
    seen = {}

    class C:
        def __init__(self, ip_adress="localhost", port=9056):
            seen["init"] = (ip_adress, port)

        def connect(self):
            seen["connect"] = True

        def creo_set_creo_version(self, v):
            seen["ver"] = v

        def is_running(self):
            return True

        is_creo_running = is_running

    monkeypatch.setitem(sys.modules, "creopyson", types.SimpleNamespace(Client=C))
    ad = CreoAdapter("7.0", port=9057)
    assert seen == {"init": ("localhost", 9057), "connect": True, "ver": 7} and ad.editor.c is ad.c


# ---- 端到端：通过 CadService ----
@pytest.fixture
def creo_svc(tmp_path, monkeypatch):
    client = FakeCreoClient()
    ad = make_adapter(client)
    monkeypatch.setattr(CreoAdapter, "file_version", lambda self, p: ("creo", "10.0"))
    src = tmp_path / "designs" / "OLD"
    src.mkdir(parents=True)
    for n in ("bracket.prt", "line.asm", "bracket.drw"):
        (src / n).write_text("creo placeholder")
    svc = CadService.from_config(None, None, str(tmp_path / "ws"))
    svc.targets["fake-creo"] = Target("fake-creo", ad)
    svc.select_target("fake-creo")
    svc.policy.read_allow.append((tmp_path / "designs").resolve())
    svc.change_begin("OLD", sources=[str(src)])
    return svc, client


def test_service_inspect_existing(creo_svc):
    svc, client = creo_svc
    r = svc.inspect_existing("bracket.prt")
    assert r["ok"] and {d["name"] for d in r["dimensions"]} == {"d1", "d2", "d3", "rd9"}
    assert r["used_by"] == ["bracket.drw", "line.asm"]


def test_service_edit_existing_validation(creo_svc):
    svc, _ = creo_svc
    r = svc.edit_existing("bracket.prt", [{"op": "set_dimension", "name": "d99", "value": 12}])
    assert not r["applied"] and r["findings"][0]["rule"] == "E001"
    r = svc.edit_existing("bracket.prt", [{"op": "set_dimension", "name": "rd9", "value": 12}])
    assert r["findings"][0]["rule"] == "E003"                         # 参考尺寸不可改
    r = svc.edit_existing("bracket.prt", [{"op": "set_dimension", "name": "d2", "value": 12}])
    assert r["findings"][0]["rule"] == "E003" and "d1*2" in r["findings"][0]["message"]  # 关系式驱动


def test_service_edit_existing_end_to_end(creo_svc):
    svc, client = creo_svc
    r = svc.edit_existing("bracket.prt", [{"op": "set_dimension", "name": "d1", "value": 50.8},
                                          {"op": "suppress", "feature": "HOLE1", "suppressed": True}])
    assert r["applied"], r
    assert {"name": "d1", "before": pytest.approx(25.4), "after": pytest.approx(50.8)} in r["diff"]["dimensions"]
    assert r["diff"]["features"] == [{"feature": "HOLE1", "suppressed": True}]
    assert [x["file"] for x in r["refreshed"]] == ["bracket.drw", "line.asm"]
    assert all(x["status"] == "ok" for x in r["refreshed"])
    assert client.disk["bracket.prt"]["dims"]["d1"] == pytest.approx(2.0)


def test_service_edit_existing_regen_failure(creo_svc):
    svc, client = creo_svc
    client.fail_regen = True
    r = svc.edit_existing("bracket.prt", [{"op": "set_dimension", "name": "d1", "value": 50.8}])
    assert not r["ok"] and not r["applied"] and r["regen"]["status"] == "errors"
    assert client.disk["bracket.prt"]["dims"]["d1"] == 1.0
