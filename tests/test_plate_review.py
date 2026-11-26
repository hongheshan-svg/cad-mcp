"""板块类加工审查规则 R009–R011。"""
from cadmcp import rules
from cadmcp.ir import PartSpec
from cadmcp.ir.resolve import resolve_part


def _rules(features, t=60):
    spec = {"name": "P", "part_no": "P-01", "title": "块", "material": "45#", "surface_treatment": "发黑",
            "base": {"type": "extrude", "profile": {"shape": "rect", "width": 100, "height": 80}, "thickness": t},
            "features": features}
    return {(f["rule"], f["target"], f["severity"]) for f in rules.check_part(resolve_part(PartSpec.model_validate(spec)))}


def test_deep_hole():
    assert ("R009", "H", "warning") in _rules([{"feature": "hole", "name": "H", "type": "simple", "size": 4,
                                                 "positions": [[0, 0]]}])
    assert not any(r[0] == "R009" for r in _rules([{"feature": "hole", "name": "H", "type": "simple", "size": 8,
                                                      "positions": [[0, 0]]}]))


def test_blind_tap_drill_margin():
    r = _rules([{"feature": "hole", "name": "T", "type": "tapped", "size": "M8", "depth": 14, "thread_depth": 12,
                 "positions": [[0, 0]]}])
    assert ("R010", "T", "warning") in r


def test_pocket_corners():
    pk = {"feature": "pocket", "name": "PK", "profile": {"shape": "rect", "width": 40, "height": 30}, "depth": 18}
    assert ("R011", "PK", "warning") in _rules([pk])
    small = {**pk, "profile": {**pk["profile"], "corner_radius": 2}}
    assert ("R011", "PK", "info") in _rules([small])
    ok = {**pk, "profile": {**pk["profile"], "corner_radius": 4}}
    assert not any(r[0] == "R011" for r in _rules([ok]))
