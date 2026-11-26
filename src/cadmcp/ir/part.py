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


class ShaftBase(Strict):
    """Turned parts: coaxial cylinders stacked along +Z starting at Z=0."""
    type: Literal["shaft"] = "shaft"
    steps: list[ShaftStep] = Field(min_length=1)


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
    type: Literal["simple", "clearance", "tapped", "counterbore", "countersink"]
    size: str | Num = Field(description='"M6" for metric types, or a diameter in mm for simple holes')
    depth: Num | Literal["through"] = "through"
    thread_depth: Num | None = None
    tolerance: str | None = Field(None, description='fit, e.g. "H7" for dowel holes')
    positions: list[Vec2] = []
    pattern: Pattern | None = None

    @model_validator(mode="after")
    def _has_location(self):
        if not self.positions and self.pattern is None:
            raise ValueError(f"hole {self.name!r}: give positions or a pattern")
        if self.type != "simple" and not isinstance(self.size, str):
            raise ValueError(f"hole {self.name!r}: type {self.type} needs a metric size like 'M6'")
        return self


class Pocket(Strict):
    """Material removal by a profile sketched on a face (cut-extrude)."""
    feature: Literal["pocket"] = "pocket"
    name: str
    face: Face = "top"
    profile: Profile
    depth: Num | Literal["through"]


class Boss(Strict):
    """Material addition by a profile sketched on a face."""
    feature: Literal["boss"] = "boss"
    name: str
    face: Face = "top"
    profile: Profile
    height: Num


class EdgeFinish(Strict):
    feature: Literal["fillet", "chamfer"]
    name: str
    edges: Literal["outer_vertical", "top_outer", "bottom_outer"]
    size: Num


Feature = Annotated[Hole | Pocket | Boss | EdgeFinish, Field(discriminator="feature")]


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
