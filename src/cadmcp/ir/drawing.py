"""Drawing spec: what a mechanical engineer decides when detailing a part/assembly."""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import Num, Strict

ViewType = Literal["front", "top", "bottom", "left", "right", "back", "iso", "flat_pattern"]


class ViewSpec(Strict):
    type: ViewType
    scale: Num | Literal["sheet"] = "sheet"


class SectionSpec(Strict):
    """剖视图（GB/T 4458.6）。剖切线画在俯视图上；剖视图替代主视图（或左视图）的位置。"""
    kind: Literal["full", "half", "step"] = Field("full", description="全剖 / 半剖（对称件）/ 阶梯剖（多个平行剖切面）")
    view: Literal["front", "left"] = Field("front", description="被剖视替代的视图")
    at: list[Num] = Field([], description="剖切面位置（主视图剖视为 Y 坐标，左视图剖视为 X 坐标）；阶梯剖可给多个")
    through: list[str] = Field([], description="剖切面穿过这些特征（孔/型腔名）的中心；比 at 更常用")
    label: str | None = Field(None, description="剖切符号字母，默认 A、B…")


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
    sections: Literal["auto", "none"] | list[SectionSpec] = Field(
        "auto", description="auto：有沉孔/螺纹孔/盲孔/盲型腔/侧面孔时自动把主视图画成全剖或阶梯剖；none：不剖；或逐个指定")
    bom_table: bool = Field(True, description="assemblies only")
    balloons: bool = Field(True, description="assemblies only")
    title_block: dict[str, str] = Field({}, description="overrides: designer, checker, approver, date, ...")
    revisions: list[dict[str, str]] = Field([], description="版本记录（发布时由平台写入）：rev/date/description/by")
