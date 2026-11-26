"""铝型材机架生成（与 CAD 无关）：给外形尺寸和型材，生成立柱 + 各层横梁的装配 spec、下料清单、紧固件。

坐标：机架底面中心为原点，X 宽、Y 深、Z 高。立柱通长，横梁夹在立柱之间（下料长 = 外形 − 2×型材宽）。
型材库件约定：CS_End0 在端面中心、Z 沿长度方向。
"""
from __future__ import annotations

from typing import Any

from pydantic import Field

from .errors import SPEC_INVALID, CadError
from .ir.common import Num, Strict
from .library import Library


class FrameSpec(Strict):
    kind: str = "frame"
    name: str
    part_no: str
    title: str = "机架"
    width: Num = Field(description="X 外形 mm")
    depth: Num = Field(description="Y 外形 mm")
    height: Num = Field(description="Z 外形（立柱长）mm")
    profile: str = Field("PRF-4040", description="型材库编码")
    levels: list[Num] | None = Field(None, description="横梁层中心高度 mm；默认底层和顶层")
    beams_x: bool = Field(True, description="前后两侧沿 X 的横梁")
    beams_y: bool = Field(True, description="左右两侧沿 Y 的横梁")
    bracket: str | None = Field("BRK-4040", description="角码库编码；None 不用角码")
    brackets_per_joint: int = 1
    fasteners_per_bracket: list[str] = Field(["TNUT-M8-40", "SHCS-M8x16"],
                                             description="每个角码每侧用 1 套：T 型螺母 + 螺钉")
    min_beam_length: Num = 50.0


def frame_assembly(spec: FrameSpec, library: Library | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    lib = library or Library.default()
    if spec.profile not in lib.items:
        raise CadError(SPEC_INVALID, f"profile {spec.profile!r} not in library", hint="cad_search_library('型材')")
    raw = lib.items[spec.profile]
    pb = lib.get(spec.profile)
    p = pb.bbox.xmax - pb.bbox.xmin  # 型材截面宽
    W, D, H = float(spec.width), float(spec.depth), float(spec.height)
    if min(W, D) < 2 * p + float(spec.min_beam_length):
        raise CadError(SPEC_INVALID, f"frame {W:g}×{D:g} too small for {p:g} profile")
    levels = [float(z) for z in (spec.levels or [p / 2, H - p / 2])]
    for z in levels:
        if not p / 2 - 1e-6 <= z <= H - p / 2 + 1e-6:
            raise CadError(SPEC_INVALID, f"level {z:g} outside post height", hint=f"层高范围 {p / 2:g}–{H - p / 2:g}")
    if "L" not in raw.get("parameters", {}):
        raise CadError(SPEC_INVALID, f"{spec.profile} has no length parameter L")

    comps: list[dict[str, Any]] = []
    cx, cy = W / 2 - p / 2, D / 2 - p / 2
    for i, (sx, sy) in enumerate([(-1, -1), (1, -1), (1, 1), (-1, 1)]):
        comps.append({"id": f"post{i + 1}", "source": f"lib:{spec.profile}?L={H:g}",
                      "place": {"align": "CS_End0", "to": "ASM.CS_Origin", "offset": [sx * cx, sy * cy, 0]}})
    beams = 0
    Lx, Ly = W - 2 * p, D - 2 * p
    for k, z in enumerate(levels):
        if spec.beams_x:
            for sy, tag in ((-1, "F"), (1, "B")):
                comps.append({"id": f"bx{tag}{k + 1}", "source": f"lib:{spec.profile}?L={Lx:g}",
                              "place": {"align": "CS_End0", "to": "ASM.CS_Origin",
                                        "offset": [-(W / 2 - p), sy * cy, z], "rotate": [0, 90, 0]}})
                beams += 1
        if spec.beams_y:
            for sx, tag in ((-1, "L"), (1, "R")):
                comps.append({"id": f"by{tag}{k + 1}", "source": f"lib:{spec.profile}?L={Ly:g}",
                              "place": {"align": "CS_End0", "to": "ASM.CS_Origin",
                                        "offset": [sx * cx, -(D / 2 - p), z], "rotate": [-90, 0, 0]}})
                beams += 1
    extras = []
    if spec.bracket:
        nb = beams * 2 * spec.brackets_per_joint
        extras.append({"source": f"lib:{spec.bracket}", "qty": nb, "note": "横梁两端"})
        for f in spec.fasteners_per_bracket:
            extras.append({"source": f"lib:{f}", "qty": nb * 2, "note": "角码每侧 1 套"})
    else:
        extras += [{"source": "lib:SHCS-M8x16", "qty": beams * 2, "note": "端面攻丝连接（需加工型材端面 M8 + 立柱过孔）"}]
    asm = {"kind": "assembly", "name": spec.name, "part_no": spec.part_no, "title": spec.title,
           "components": comps, "bom_extras": extras,
           "properties": {"外形": f"{W:g}×{D:g}×{H:g}", "型材": spec.profile}}
    cut = {}
    for c in comps:
        L = float(c["source"].split("L=")[1])
        cut[L] = cut.get(L, 0) + 1
    cut_list = [{"profile": spec.profile, "length_mm": L, "qty": n,
                 "use": "立柱" if L == H else ("X 向横梁" if L == Lx else "Y 向横梁")} for L, n in sorted(cut.items(), reverse=True)]
    return asm, cut_list
