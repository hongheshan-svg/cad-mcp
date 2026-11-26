"""Drawing spec: what a mechanical engineer decides when detailing a part/assembly."""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import Num, Strict

ViewType = Literal["front", "top", "bottom", "left", "right", "back", "iso", "flat_pattern"]


class ViewSpec(Strict):
    type: ViewType
    scale: Num | Literal["sheet"] = "sheet"


class DrawingSpec(Strict):
    kind: Literal["drawing"] = "drawing"
    model: str = Field(description="name of a part/assembly built or opened in this session")
    name: str | None = Field(None, description="drawing file base name; default = model name")
    sheet: Literal["auto", "A4", "A3", "A2", "A1", "A0"] = "auto"
    template: str = "company_default"
    projection: Literal["first_angle", "third_angle"] = "first_angle"
    views: list[ViewSpec] = Field(default_factory=lambda: [ViewSpec(type="front"), ViewSpec(type="top"),
                                                           ViewSpec(type="left"), ViewSpec(type="iso")],
                                  description="默认 主视图+俯视图+左视图+轴测图（GB 第一角）")
    dimensions: Literal["auto", "model", "none"] = Field(
        "auto", description="auto = company dimensioning rules (overall + ordinate holes); model = import model dims")
    hole_callouts: bool = True
    hole_table: bool = False
    general_tolerance: str = "GB/T 1804-m"
    surface_finish: str | None = "Ra 3.2"
    notes: list[str] = Field([], description="额外技术要求。平台已自动生成：未注公差、锐边倒钝去毛刺、表面处理（来自零件）、钣金折弯半径——不要重复写")
    bom_table: bool = Field(True, description="assemblies only")
    balloons: bool = Field(True, description="assemblies only")
    title_block: dict[str, str] = Field({}, description="overrides: designer, checker, approver, date, ...")
    revisions: list[dict[str, str]] = Field([], description="版本记录（发布时由平台写入）：rev/date/description/by")
