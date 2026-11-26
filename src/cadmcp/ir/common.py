from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# A number or a parametric expression string such as "L - 2*edge".
Num = float | str
Vec2 = tuple[Num, Num]
Vec3 = tuple[Num, Num, Num]

# Faces are named by the outward normal of the part's bounding box in the part frame.
# Base sketch lies in XY at Z=0 and the body grows towards +Z.
Face = Literal["top", "bottom", "front", "back", "left", "right"]
FACE_NORMAL: dict[str, tuple[int, int, int]] = {
    "top": (0, 0, 1), "bottom": (0, 0, -1),
    "front": (0, -1, 0), "back": (0, 1, 0),
    "left": (-1, 0, 0), "right": (1, 0, 0),
}

REF_PREFIXES = {"MP_": "plane", "AX_": "axis", "CS_": "csys", "PT_": "point"}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Rect(Strict):
    shape: Literal["rect"] = "rect"
    width: Num = Field(description="X size, mm")
    height: Num = Field(description="Y size, mm")
    center: Vec2 = (0.0, 0.0)
    corner_radius: Num = 0.0


class Circle(Strict):
    shape: Literal["circle"] = "circle"
    diameter: Num
    center: Vec2 = (0.0, 0.0)


class Slot(Strict):
    shape: Literal["slot"] = "slot"
    length: Num = Field(description="overall length incl. round ends, mm")
    width: Num
    center: Vec2 = (0.0, 0.0)
    angle: Num = 0.0


class Polygon(Strict):
    shape: Literal["polygon"] = "polygon"
    points: list[Vec2] = Field(min_length=3, description="closed polygon, counter-clockwise, mm")


Profile = Annotated[Rect | Circle | Slot | Polygon, Field(discriminator="shape")]
