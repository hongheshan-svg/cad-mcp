"""Creo TOOLKIT 路径：假 cadmcp_tk（按 docs/CREO_TOOLKIT.md 校验参数）+ 假 creopyson 上跑建模与出图。"""
import sys

import pytest
from fake_creopyson import install_fake_creopyson
from fake_toolkit import FakeToolkit, doc_schema
from test_core import L_BRACKET, PLATE_PRECISION, SHAFT
from test_hole_table import GRID

from cadmcp.adapters.creo.adapter import CreoAdapter
from cadmcp.adapters.creo.compile import compile_part_tk
from cadmcp.adapters.creo.toolkit import ToolkitClient, ToolkitError
from cadmcp.detailing import plan_part_drawing
from cadmcp.errors import CadError
from cadmcp.ir import DrawingSpec, PartSpec
from cadmcp.ir.resolve import resolve_part

TEMPLATES = {f"drawing_{s}": f"{s.lower()}.drw" for s in ("A4", "A3", "A2", "A1", "A0")}


@pytest.fixture
def creo():
    saved = sys.modules.get("creopyson")
    fake = install_fake_creopyson()
    yield fake
    if saved is None:
        sys.modules.pop("creopyson", None)
    else:
        sys.modules["creopyson"] = saved


@pytest.fixture
def tk():
    f = FakeToolkit()
    yield f
    f.close()


def _adapter(tk):
    return CreoAdapter("10.0", templates=TEMPLATES, toolkit={"host": "127.0.0.1", "port": tk.port, "timeout": 5})


def _resolve(d):
    return PartSpec.model_validate(d), resolve_part(PartSpec.model_validate(d))


# ---------------- 客户端 ----------------
def test_client_roundtrip_and_errors(tk):
    with ToolkitClient("127.0.0.1", tk.port, timeout=5) as c:
        assert c.call("ping")["toolkit"] == "fake"
        with pytest.raises(ToolkitError) as e:
            c.call("no.such")
        assert e.value.code == "UNKNOWN_CMD"
        with pytest.raises(ToolkitError) as e:
            c.call("model.save", name="X", bogus=1)
        assert e.value.code == "BAD_ARGS"


def test_client_unreachable():
    with pytest.raises(CadError) as e:
        ToolkitClient("127.0.0.1", 1, timeout=1).call("ping")
    assert e.value.code == "TARGET_UNAVAILABLE" and "cadmcp_tk" in (e.value.hint or "")


def test_doc_schema_covers_all_sections():
    s = doc_schema()
    assert {"ping", "model.new_part", "feat.extrude", "feat.hole", "datum.csys", "drw.note", "drw.readback"} <= set(s)


# ---------------- 建模 ----------------
@pytest.mark.parametrize("spec", [PLATE_PRECISION, L_BRACKET, SHAFT, GRID], ids=["plate", "bracket", "shaft", "grid"])
def test_build_part_via_toolkit(creo, tk, tmp_path, spec):
    s, r = _resolve(spec)
    br = _adapter(tk).build_part(s, r, tmp_path)
    cmds = tk.cmds()
    assert cmds[0] == "model.new_part" and cmds[-1] == "model.save"
    assert "model.regenerate" in cmds
    assert br.path.suffix == ".prt" and (tmp_path / f"{s.name}.oplog.json").exists()
    assert br.regen["status"] in ("ok", "warnings")
    base = next(a for c, a in tk.calls if c in ("feat.extrude", "feat.revolve"))
    assert base["feature"] == "Base"


def test_shaft_plan_revolve_and_keyways():
    s, r = _resolve(SHAFT)
    ops = compile_part_tk(s, r)
    rev = next(o for o in ops if o.op == "tk.feat.revolve")
    assert rev.args["plane"] == {"ref": "FRONT", "offset": 0} and rev.args["axis"]["p1"] == [0, 0]
    us = [p for e in rev.args["entities"] for p in (e["p1"], e["p2"])]
    assert min(u for u, _ in us) == 0 and max(u for u, _ in us) == 15  # 最大半径 Φ30/2
    kws = [o for o in ops if o.op == "tk.feat.extrude" and o.args["feature"].startswith("KW")]
    assert len(kws) == 2 and all(o.args["op"] == "cut" for o in kws)
    assert any(o.op == "tk.feat.revolve" and o.args["op"] == "cut" for o in ops)  # 环槽


def test_tapped_hole_cosmetic_thread(creo, tk, tmp_path):
    s, r = _resolve(GRID)
    br = _adapter(tk).build_part(s, r, tmp_path)
    assert br.regen["status"] == "ok"
    thr = next(a for c, a in tk.calls if c == "feat.cosmetic_thread")
    assert thr["hole_feature"] == "M6" and thr["major_diameter"] == 6 and thr["length"] == 10
    holes = [a for c, a in tk.calls if c == "feat.hole"]
    m6 = next(a for a in holes if a["feature"] == "M6")
    assert len(m6["points"]) == 15 and m6["face"] == "top"
    fix = next(a for a in holes if a["feature"] == "Fix")
    assert fix["cbore"] and fix["cbore"][0] > fix["diameter"]


def test_thread_failure_is_non_critical(creo, tk, tmp_path):
    tk.fail.add("feat.cosmetic_thread")
    s, r = _resolve(GRID)
    br = _adapter(tk).build_part(s, r, tmp_path)
    assert br.regen["status"] == "warnings"
    assert any("cosmetic_thread" in w for w in br.regen["warnings"])
    assert "model.save" in tk.cmds()


def test_feature_failure_is_error(creo, tk, tmp_path):
    tk.fail.add("feat.hole")
    s, r = _resolve(PLATE_PRECISION)
    with pytest.raises(CadError) as e:
        _adapter(tk).build_part(s, r, tmp_path)
    assert e.value.code == "NATIVE_ERROR" and "feat.hole" in e.value.message
    assert "model.save" not in tk.cmds()


def test_capabilities_with_toolkit(creo, tk):
    caps = _adapter(tk).capabilities()
    assert caps["build_part.extrude"] == "partial" and caps["build_part.shaft"] == "partial"
    assert CreoAdapter("10.0").capabilities()["build_part.extrude"] == "none"


# ---------------- 出图 ----------------
def _plan(spec, **kw):
    r = resolve_part(PartSpec.model_validate(spec))
    plan = plan_part_drawing(DrawingSpec.model_validate({"model": r.name, "title_block": {"designer": "AI"}, **kw}), r)
    plan.name = r.name + "_DRW"
    return plan


def test_drawing_details_via_toolkit(creo, tk, tmp_path):
    plan = _plan(PLATE_PRECISION, revisions=[{"rev": "A", "description": "首版", "date": "2026-11-26", "by": "AI"}])
    br = _adapter(tk).build_drawing(plan, tmp_path / "pp.prt", tmp_path, None)
    cmds = tk.cmds()
    assert not [n for n, _ in creo.calls if n.startswith("ext:")]  # 不再走 CREOSON 扩展
    for c in ("drw.dim", "drw.note", "drw.datum", "drw.gtol", "drw.surface_finish", "drw.table", "drw.readback"):
        assert c in cmds, c
    assert all(a["drawing"] == plan.name for _, a in tk.calls)  # 不带 .drw 扩展名
    rev = next(a for c, a in tk.calls if c == "drw.table")
    assert [col["title"] for col in rev["columns"]][0] == "版本" or rev["rows"][0][0] == "A"
    assert br.extra["rendered"]["dim_count"] == cmds.count("drw.dim")
    assert br.regen["status"] in ("ok", "warnings")


def test_shaft_dims_carry_text(creo, tk, tmp_path):
    plan = _plan(SHAFT)
    _adapter(tk).build_drawing(plan, tmp_path / "sh1.prt", tmp_path, None)
    dims = [a for c, a in tk.calls if c == "drw.dim"]
    assert dims and any(a["text"].startswith(("Φ", "⌀", "%")) for a in dims)
    assert all(len(a["p1"]) == 2 and len(a["at"]) == 2 for a in dims)


def test_hole_table_and_tags(creo, tk, tmp_path):
    plan = _plan(GRID)
    assert plan.hole_table
    _adapter(tk).build_drawing(plan, tmp_path / "gp.prt", tmp_path, None)
    tables = [a for c, a in tk.calls if c == "drw.table"]
    ht = next(a for a in tables if a["columns"][0]["title"] == "孔号")
    assert len(ht["rows"]) == len(plan.hole_table) and ht["origin"] == [round(plan.hole_table_box[0], 3),
                                                                        round(plan.hole_table_box[1], 3)]
    tags = [a for c, a in tk.calls if c == "drw.note" and a.get("height") == 2.5]
    assert len(tags) >= len({r["tag"] for r in plan.hole_table})


def test_toolkit_failure_only_warns(creo, tk, tmp_path):
    tk.fail.add("drw.gtol")
    br = _adapter(tk).build_drawing(_plan(PLATE_PRECISION), tmp_path / "pp.prt", tmp_path, None)
    assert br.regen["status"] == "warnings" and any("drw.gtol" in w for w in br.regen["warnings"])
    assert "interface_export_pdf" in [n for n, _ in creo.calls]
