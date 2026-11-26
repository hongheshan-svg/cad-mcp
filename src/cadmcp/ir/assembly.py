"""Assembly spec. Placement is by named coordinate systems only — no face picking."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .common import Num, Strict, Vec3


class Placement(Strict):
    """Align component reference `align` onto `to` ("<component_id>.<CS_name>" or
    "ASM.CS_Origin"), then apply offset (mm, in `to` frame) and rotate (deg, XYZ)."""
    align: str = "CS_Origin"
    to: str = "ASM.CS_Origin"
    offset: Vec3 = (0.0, 0.0, 0.0)
    rotate: Vec3 = (0.0, 0.0, 0.0)


class LinearArray(Strict):
    count: Num
    step: Vec3 = Field(description="offset between instances, mm, in assembly frame")


class ComponentSpec(Strict):
    id: str = Field(description="unique instance id, used in placements and BOM balloons")
    source: str = Field(description='"part:<name>" (built in this session), "asm:<name>", "lib:<code>" (standard library) or "file:<path>"')
    place: Placement | None = Field(None, description="omit → fixed at assembly origin")
    array: LinearArray | None = None

    @model_validator(mode="after")
    def _src(self):
        if self.source.split(":", 1)[0] not in ("part", "asm", "lib", "file") or ":" not in self.source:
            raise ValueError(f"{self.id}: source must be part:/asm:/lib:/file:")
        return self


class AssemblySpec(Strict):
    kind: Literal["assembly"] = "assembly"
    name: str
    part_no: str
    title: str
    parameters: dict[str, Num] = {}
    components: list[ComponentSpec] = Field(min_length=1)
    properties: dict[str, str] = {}

    @model_validator(mode="after")
    def _ids(self):
        ids = [c.id for c in self.components]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ValueError(f"duplicate component ids: {sorted(dup)}")
        for c in self.components:
            if c.place and c.place.to.split(".", 1)[0] not in ({"ASM"} | set(ids)):
                raise ValueError(f"{c.id}: placement target {c.place.to!r} unknown")
        return self
