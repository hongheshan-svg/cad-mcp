"""Part spec: the CAD-independent description an AI writes instead of clicking CAD.

Adapters compile it into native features (SolidWorks / Creo). Coordinates: the base
sketch lies in XY at Z=0, mm, and the body grows towards +Z (shaft: along +Z).
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .common import REF_PREFIXES, Face, Num, Profile, Strict, Vec2, Vec3


# ---------- base bodies ----------
class ExtrudeBase(Strict):
    """Plates, blocks, brackets: a 2D profile extruded along +Z."""
    type: Literal["extrude"] = "extrude"
    profile: Profile
    thickness: Num


class ShaftStep(Strict):
    length: Num
    diameter: Num
    tolerance: str | None = Field(None, description="配合公差带，如 k6、h7、f7；标在直径后")


class ShaftBase(Strict):
    """Turned parts: coaxial cylinders stacked along +Z starting at Z=0."""
    type: Literal["shaft"] = "shaft"
    steps: list[ShaftStep] = Field(min_length=1)
    chamfer: Num = Field(0.0, description="两端倒角 C 值（mm），0 为不倒角")


class Flange(Strict):
    edge: Literal["front", "back", "left", "right"]
    length: Num = Field(description="outer flange height, mm")
    angle: Num = 90.0
    direction: Literal["up", "down"] = "up"


class SheetMetalBase(Strict):
    """Sheet metal: a flat base flange in XY plus edge flanges on its outline edges."""
    type: Literal["sheet_metal"] = "sheet_metal"
    profile: Profile
    thickness: Num
    bend_radius: Num | None = Field(None, description="default = thickness")
    k_factor: float = 0.44
    flanges: list[Flange] = []


Base = Annotated[ExtrudeBase | ShaftBase | SheetMetalBase, Field(discriminator="type")]


# ---------- features ----------
class GridPattern(Strict):
    kind: Literal["grid"] = "grid"
    origin: Vec2
    nx: Num = 1.0
    ny: Num = 1.0
    dx: Num = 0.0
    dy: Num = 0.0


class CircularPattern(Strict):
    kind: Literal["circular"] = "circular"
    center: Vec2 = (0.0, 0.0)
    radius: Num
    count: Num
    start_angle: Num = 0.0


Pattern = Annotated[GridPattern | CircularPattern, Field(discriminator="kind")]


class Hole(Strict):
    """Hole group. Positions are 2D in the face's own frame:
    top/bottom → (x, y); front/back → (x, z); left/right → (y, z)."""
    feature: Literal["hole"] = "hole"
    name: str
    face: Face = "top"
    type: Literal["simple", "clearance", "tapped", "counterbore", "countersink", "slot"] = Field(
        description="slot = 腰型孔（调节孔）：size 为螺钉规格(取过孔宽)或宽度 mm，slot_length 为中心距")
    size: str | Num = Field(description='"M6" for metric types, or a diameter in mm for simple holes')
    depth: Num | Literal["through"] = "through"
    thread_depth: Num | None = None
    tolerance: str | None = Field(None, description='fit, e.g. "H7" for dowel holes')
    slot_length: Num | None = Field(None, description="腰型孔两端圆心距（调节行程），mm")
    slot_angle: Num = Field(0.0, description="腰型孔方向，相对面内第一轴，度")
    at: Num | None = Field(None, description="非外表面时：该面所在平面在其法向轴上的坐标（如台阶面 z=10、L 形内侧面 y=8）")
    positions: list[Vec2] = []
    pattern: Pattern | None = None

    @model_validator(mode="after")
    def _has_location(self):
        if not self.positions and self.pattern is None:
            raise ValueError(f"hole {self.name!r}: give positions or a pattern")
        if self.type not in ("simple", "slot") and not isinstance(self.size, str):
            raise ValueError(f"hole {self.name!r}: type {self.type} needs a metric size like 'M6'")
        if self.type == "slot" and self.slot_length is None:
            raise ValueError(f"hole {self.name!r}: slot needs slot_length (center distance)")
        return self


class Pocket(Strict):
    """Material removal by a profile sketched on a face (cut-extrude)."""
    feature: Literal["pocket"] = "pocket"
    name: str
    face: Face = "top"
    profile: Profile
    depth: Num | Literal["through"]
    at: Num | None = Field(None, description="非外表面时：面所在平面在法向轴上的坐标")


class Boss(Strict):
    """Material addition by a profile sketched on a face."""
    feature: Literal["boss"] = "boss"
    name: str
    face: Face = "top"
    profile: Profile
    height: Num
    at: Num | None = Field(None, description="非外表面时：面所在平面在法向轴上的坐标")


class EdgeFinish(Strict):
    feature: Literal["fillet", "chamfer"]
    name: str
    edges: Literal["outer_vertical", "top_outer", "bottom_outer"]
    size: Num


class Keyway(Strict):
    """轴上普通平键键槽（A 型，圆头），位于 +X 方向（angle 绕轴旋转）。宽、深默认按 GB/T 1095 查表。"""
    feature: Literal["keyway"]
    name: str
    step: int = Field(description="所在轴段序号（从 0 起）")
    length: Num = Field(description="键槽长度，取 GB/T 1096 长度系列")
    offset: Num | None = Field(None, description="键槽起点距该轴段起点的距离；默认居中")
    width: Num | None = None
    depth: Num | None = Field(None, description="轴槽深 t1")
    angle: Num = 0.0


class Groove(Strict):
    """轴上环槽：circlip=轴用弹性挡圈槽（GB/T 894 查表），undercut=退刀槽（宽×深）。"""
    feature: Literal["groove"]
    name: str
    type: Literal["circlip", "undercut"]
    step: int
    position: Literal["start", "end"] | Num = Field("end", description="槽在轴段中的位置：start/end 或距轴段起点的距离")
    width: Num | None = Field(None, description="undercut 默认 2")
    depth: Num | None = Field(None, description="undercut 默认 0.5")


class Mount(Strict):
    """外购件安装接口：按标准件库登记的接口自动开螺钉孔 + 止口/让位孔，并生成装配用坐标系 CS_<name>
    （z 沿面外法向；把外购件的接口坐标系对齐到它即可装配）。"""
    feature: Literal["mount"]
    name: str
    item: str = Field(description="库编码（可带参数，如 'LGR-15?L=400'），见 cad_search_library")
    face: Literal["top", "bottom"] = "top"
    center: Vec2 = (0.0, 0.0)
    rotate: Num = Field(0.0, description="绕面法向旋转角，度")
    screw_hole: Literal["clearance", "counterbore", "tapped"] = Field(
        "clearance", description="螺钉从本零件穿过 clearance/counterbore；外购件螺钉拧入本零件 tapped")
    depth: Num | Literal["through"] = "through"
    thread_depth: Num | None = None
    pilot: bool = Field(True, description="是否开止口/让位孔（库中登记了 pilot 时）")


Feature = Annotated[Hole | Pocket | Boss | EdgeFinish | Keyway | Groove | Mount, Field(discriminator="feature")]


# ---------- named references ----------
class RefSpec(Strict):
    """Named reference used for assembly placement. Prefix sets the kind:
    CS_ coordinate system (origin+rotate), MP_ plane (face), AX_ axis (hole instance
    or origin+direction), PT_ point (origin)."""
    name: str
    origin: Vec3 | None = None
    rotate: Vec3 = (0.0, 0.0, 0.0)
    face: Face | None = None
    hole: str | None = None
    index: int = 0
    direction: Vec3 | None = None

    @model_validator(mode="after")
    def _check(self):
        prefix = self.name[:3]
        if prefix not in REF_PREFIXES:
            raise ValueError(f"reference {self.name!r} must start with one of {list(REF_PREFIXES)}")
        kind = REF_PREFIXES[prefix]
        if kind in ("csys", "point") and self.origin is None:
            raise ValueError(f"{self.name}: {kind} needs origin")
        if kind == "plane" and self.face is None:
            raise ValueError(f"{self.name}: plane needs face")
        if kind == "axis" and self.hole is None and (self.origin is None or self.direction is None):
            raise ValueError(f"{self.name}: axis needs hole, or origin+direction")
        return self


# ---------- 精度：基准、几何公差、表面粗糙度 ----------
TARGET_DOC = ("被标注对象：face:<top|bottom|front|back|left|right>（外表面）、hole:<孔特征名>（孔轴线/孔壁）、"
              "step:<段号>（轴段圆柱面/轴线）")


class Datum(Strict):
    name: str = Field(pattern=r"^[A-HJ-NP-Z]$", description="基准字母（不用 I、O）")
    target: str = Field(description=TARGET_DOC)


GeoTolType = Literal["flatness", "straightness", "roundness", "cylindricity", "parallelism", "perpendicularity",
                     "angularity", "position", "coaxiality", "symmetry", "circular_runout", "total_runout"]
GEOTOL_SYMBOL = {"flatness": "⏥", "straightness": "—", "roundness": "○", "cylindricity": "⌭", "parallelism": "∥",
                 "perpendicularity": "⊥", "angularity": "∠", "position": "⌖", "coaxiality": "◎", "symmetry": "⌯",
                 "circular_runout": "↗", "total_runout": "⌰"}
FORM_TOLS = {"flatness", "straightness", "roundness", "cylindricity"}  # 形状公差：不带基准


class GeoTol(Strict):
    type: GeoTolType
    value: Num = Field(description="公差值 mm")
    target: str = Field(description=TARGET_DOC)
    datums: list[str] = Field([], description="基准字母，按主次顺序")


class Roughness(Strict):
    target: str = Field(description=TARGET_DOC)
    ra: Num = Field(description="Ra 值 μm，优先 0.4/0.8/1.6/3.2/6.3/12.5")


class PartSpec(Strict):
    kind: Literal["part"] = "part"
    name: str = Field(description="file base name, e.g. 'J2611-020-003_MountPlate'")
    part_no: str = Field(description="drawing / ERP number")
    title: str = Field(description="part name shown on drawing/BOM, e.g. '气缸安装板'")
    material: str
    category: Literal["machined", "sheet_metal", "purchased", "standard"] = "machined"
    surface_treatment: str | None = Field(None, description="e.g. '阳极氧化黑', '镀锌'")
    parameters: dict[str, Num] = Field({}, description="driving parameters → SW global variables / Creo parameters")
    base: Base
    features: list[Feature] = []
    references: list[RefSpec] = []
    properties: dict[str, str] = {}
    datums: list[Datum] = Field([], description="基准；几何公差引用其字母")
    tolerances: list[GeoTol] = Field([], description="几何公差（形位公差）")
    roughness: list[Roughness] = Field([], description="个别表面粗糙度；其余表面按出图 surface_finish")

    @model_validator(mode="after")
    def _unique_names(self):
        names = [f.name for f in self.features] + [r.name for r in self.references]
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            raise ValueError(f"duplicate feature/reference names: {sorted(dup)}")
        holes = {f.name for f in self.features if f.feature == "hole"}
        for r in self.references:
            if r.hole and r.hole not in holes:
                raise ValueError(f"{r.name}: hole {r.hole!r} not defined")
        return self
