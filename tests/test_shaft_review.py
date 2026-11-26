"""轴类结构审查规则 S006–S010。"""
from cadmcp import rules
from cadmcp.ir import PartSpec
from cadmcp.ir.resolve import resolve_part


def _rules(steps, features=(), **base):
    spec = {"name": "R", "part_no": "R-01", "title": "轴", "material": "45#",
            "base": {"type": "shaft", "chamfer": 1, "steps": steps, **base}, "features": list(features)}
    return {(f["rule"], f["target"]) for f in rules.check_part(resolve_part(PartSpec.model_validate(spec)))}


def test_low_bearing_shoulder_and_grinding():
    r = _rules([{"length": 20, "diameter": 30, "tolerance": "k6"}, {"length": 30, "diameter": 33}])
    assert ("S006", "step0") in r and ("S008", "step0") in r
    ok = _rules([{"length": 20, "diameter": 30, "tolerance": "k6"}, {"length": 30, "diameter": 36}],
                [{"feature": "groove", "name": "G", "type": "grinding", "step": 0}])
    assert not {x for x in ok if x[0] in ("S006", "S008")}
    assert not {x for x in _rules([{"length": 20, "diameter": 30, "tolerance": "k6"}, {"length": 30, "diameter": 36}],
                                  shoulder_fillet=0.5) if x[0] == "S008"}


def test_cross_hole_on_fit_and_keyway_without_fit():
    r = _rules([{"length": 40, "diameter": 30, "tolerance": "h7"}, {"length": 40, "diameter": 25}],
               [{"feature": "keyway", "name": "KW", "step": 1, "length": 25}],
               cross_holes=[{"step": 0, "diameter": 6}])
    assert ("S007", "XH1") in r and ("S010", "step1") in r


def test_thread_into_shoulder():
    steps = [{"length": 20, "diameter": 20}, {"length": 30, "diameter": 30}]
    r = _rules(steps, [{"feature": "thread", "name": "T", "step": 0, "size": "M20"}])
    assert ("S009", "T") in r
    ok = _rules(steps, [{"feature": "thread", "name": "T", "step": 0, "size": "M20", "length": 15},
                        {"feature": "groove", "name": "U", "type": "undercut", "step": 0, "width": 3, "depth": 1.5}])
    assert not {x for x in ok if x[0] == "S009"}
