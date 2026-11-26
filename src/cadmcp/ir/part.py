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
    shoulder_fillet: Num = Field(0.0, description="轴肩根部过渡圆角 R（mm，所有轴肩；靠轴肩有环槽处不倒）；"
                                 "图上不画，技术要求注\"未注轴肩圆角 R…\"；0 = 不倒")
    center_holes: "CenterHoles | None" = Field(None, description="轴端中心孔（GB/T 145，图上按 GB/T 4459.5 简化标注）")
    bores: list["ShaftBore"] = Field([], description="内孔（套类/空心轴）：从轴端起的同轴圆孔；有内孔时主视图全剖")
    end_holes: list["EndHole"] = Field([], description="轴端螺孔（装挡圈/压板），主视图细虚线 + 端面引线标注")
    flange_holes: list["FlangeHoles"] = Field([], description="法兰/端盖轴向均布通孔（需有内孔，主视图全剖）")
    cross_holes: list["CrossHole"] = Field([], description="径向横孔（销孔/油孔/紧定螺钉孔）：孔轴线垂直于主视图，"
                                           "主视图上画成圆并引线标注，另注孔心到轴段起点的定位尺寸")


class CrossHole(Strict):
    """轴上径向横孔：孔轴线沿主视图投射方向（主视图上是圆）。diameter（光孔）与 size（螺孔，如 M6）二选一；
    depth 省略 = 通孔（穿过轴线两侧）；盲孔从主视图朝向观察者的一侧钻入。"""
    step: int
    position: Num | None = Field(None, description="孔心距该轴段起点的距离；缺省为轴段中点")
    diameter: Num | None = Field(None, description="光孔直径（销孔常配 tolerance H7）")
    size: str | None = Field(None, description="螺孔：M6 或 M6x0.75")
    depth: Num | None = Field(None, description="孔深（螺孔为螺纹深）；省略 = 通孔")
    drill_depth: Num | None = Field(None, description="螺孔底孔深；缺省 = 螺纹深 + 0.5d（取整）")
    tolerance: str | None = Field(None, description="光孔公差带（如 H7）；螺孔缺省 6H")


class EndHole(Strict):
    side: Literal["start", "end"] = "start"
    size: str = Field(description="M8 或 M8x1")
    depth: Num = Field(description="螺纹深度")
    drill_depth: Num | None = Field(None, description="底孔深度；缺省 = 螺纹深度 + 0.5d（取整）")
    tolerance: str = "6H"


class ShaftBore(Strict):
    diameter: Num
    length: Num | None = Field(None, description="孔深（平底）；省略 = 通孔")
    side: Literal["start", "end"] = Field("start", description="从哪一端起（z=0 为 start）")
    tolerance: str | None = Field(None, description="孔公差带，如 H7")
    keyway: bool = Field(False, description="孔内轮毂键槽（GB/T 1095，按孔径查槽宽 b 与槽深 t2，贯通孔长，槽宽 JS9）")
    keyway_width: Num | None = Field(None, description="键槽宽，缺省按孔径查表")


class FlangeHoles(Strict):
    """法兰/端盖上的轴向均布通孔：贯穿某一轴段（该段两侧须是更小的轴段或端面），孔心在分布圆上均布。
    图上按 GB/T 4458.1 规定画法旋转到剖切平面画出，左视图注 n×Φd EQS 与分布圆直径。"""
    step: int
    count: int = Field(ge=2, le=36)
    pcd: Num = Field(description="分布圆直径")
    diameter: Num = Field(description="孔径（通孔）")
    offset_deg: Num = Field(0, description="第一个孔相对主视图正上方的转角（度）")


class CenterHoles(Strict):
    type: Literal["A", "B"] = Field("B", description="A 不带护锥；B 带 120° 护锥（完工后保留时常用）")
    d: float | None = Field(None, description="中心孔公称直径 1/1.6/2/2.5/3.15/4/6.3/10；缺省按轴端直径推荐")
    ends: Literal["both", "start", "end"] = "both"
    keep: Literal["required", "optional", "forbidden"] = Field(
        "optional", description="完工零件上：required 要求保留、optional 可以保留、forbidden 不允许保留")


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


class PlateHole(Strict):
    """焊接板上的孔：位置相对板中心，在板面内（水平板 (x, y)；立板 (横向, z)）。"""
    name: str
    type: Literal["simple", "clearance", "tapped", "counterbore", "countersink"] = "clearance"
    size: str | Num
    positions: list[Vec2] = Field(min_length=1)
    tolerance: str | None = None


class WeldMember(Strict):
    """型钢杆件：start/end 为两端面中心（只支持沿 X/Y/Z 轴向）。下料长度 = |end − start|。"""
    name: str
    section: str = Field(description='方管 "SHS40x3"、矩形管 "RHS60x40x3"（高×宽×壁厚）、角钢 "L50x5"、'
                                     '圆管 "CHS48x3"、扁钢 "FB50x6"、槽钢 "C10"、工字钢 "I14"')
    start: Vec3
    end: Vec3
    rotate: Literal[0, 90] = Field(0, description="截面绕杆件轴转 90°（矩形管/槽钢换向）")


class WeldPlate(Strict):
    """焊接板（安装板、地脚板、筋板、封板）：center 为板体中心，normal 为板厚方向。"""
    name: str
    size: Vec2 = Field(description="板面尺寸 (a, b)：水平板 (X, Y)；normal=y 的立板 (X, Z)；normal=x 的立板 (Y, Z)")
    thickness: Num
    center: Vec3
    normal: Literal["x", "y", "z"] = "z"
    holes: list[PlateHole] = []
    machined: bool = Field(False, description="焊后加工（安装基准面）：图上注明焊后加工与平面度")


class WeldmentBase(Strict):
    """焊接件：型钢杆件 + 板件焊成一体（多实体零件）。焊缝由平台按接触自动识别。"""
    type: Literal["weldment"] = "weldment"
    members: list[WeldMember] = []
    plates: list[WeldPlate] = []
    weld_size: Num | None = Field(None, description="角焊缝焊脚 K（mm）；默认按较薄件自动取")
    end_caps: bool = Field(False, description="敞口管端加塑料堵头（否则报 W003 提醒）")
    stress_relief: bool = False
    weld_process: Literal["135", "111", "141"] = Field(
        "135", description="焊接方法（GB/T 5185）：135 MAG/CO2 气保焊、111 焊条电弧焊、141 TIG")

    @model_validator(mode="after")
    def _some(self):
        if not self.members and not self.plates:
            raise ValueError("weldment needs members or plates")
        names = [m.name for m in self.members] + [p.name for p in self.plates]
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            raise ValueError(f"duplicate weldment body names: {sorted(dup)}")
        return self


Base = Annotated[ExtrudeBase | ShaftBase | SheetMetalBase | WeldmentBase, Field(discriminator="type")]


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
    """轴上环槽：circlip=轴用弹性挡圈槽（GB/T 894 查表），undercut=退刀槽（宽×深），
    grinding=砂轮越程槽（GB/T 6403.5 磨外圆，按轴径查 b×h；缺省放在靠轴肩的一端，配局部放大图）。"""
    feature: Literal["groove"]
    name: str
    type: Literal["circlip", "undercut", "grinding"]
    step: int
    position: Literal["start", "end"] | Num | None = Field(
        None, description="槽在轴段中的位置：start/end 或距轴段起点的距离；缺省 end（grinding 缺省靠轴肩一端）")
    width: Num | None = Field(None, description="undercut 默认 2；grinding 默认查表")
    depth: Num | None = Field(None, description="undercut 默认 0.5；grinding 默认查表")


class ShaftThread(Strict):
    """轴上外螺纹（普通螺纹 GB/T 193/196）：车在某轴段上（该段直径 = 螺纹公称直径），从轴段一端起。
    图上螺纹代号替代该段直径尺寸（如 M20×1.5-6g），未满整段时另注螺纹长度。"""
    feature: Literal["thread"]
    name: str
    step: int
    size: str = Field(description="M20（粗牙）或 M20x1.5（细牙）")
    length: Num | None = Field(None, description="螺纹长度；默认整个轴段")
    side: Literal["start", "end"] | None = Field(None, description="从轴段哪一端起；默认靠轴端的一侧")
    tolerance: str = Field("6g", description="外螺纹公差带")


class ShaftGear(Strict):
    """齿轮轴：在某轴段上加工直齿（渐开线标准齿廓，GB/T 1356）。该段直径 = 齿顶圆 da = m(z + 2 + 2x)，
    齿宽 = 该段长度。出图按 GB/T 4459.2（齿顶线粗实线、分度线细点画线、齿根线细实线）并附参数表。"""
    feature: Literal["gear"]
    name: str
    step: int
    module: Num
    teeth: int = Field(ge=6)
    pressure_angle: Num = 20.0
    shift: Num = 0.0
    accuracy: str = "8 GB/T 10095.1"
    tip_tolerance: str = "h11"
    flank_ra: Num = 1.6
    mate: str | None = None


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


Feature = Annotated[Hole | Pocket | Boss | EdgeFinish | Keyway | Groove | ShaftThread | ShaftGear | Mount,
                    Field(discriminator="feature")]


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
              "step:<段号>（轴段圆柱面/轴线）、bore:<内孔序号，从 0 起>（套类内孔孔壁/轴线）、"
              "shoulder:<i>（第 i 与 i+1 段之间的轴肩端面；仅几何公差，如轴承定位轴肩的端面圆跳动）、"
              "keyway:<键槽名>（键槽中心平面；仅几何公差，如对称度，画在断面图槽宽尺寸线上）")


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


class GearSpec(Strict):
    """渐开线直齿圆柱外齿轮（GB/T 1356 标准齿廓）。底座写 extrude + circle：直径 = 齿顶圆 da = m(z + 2 + 2x)，
    厚度 = 齿宽 b；孔、键槽（贯通型腔）、减重孔照常写在 features 里。平台按渐开线生成真实齿形建模，
    出图按 GB/T 4459.2 规定画法并在右上角附参数表。"""
    module: Num = Field(description="模数 m（GB/T 1357 系列）")
    teeth: int = Field(ge=6, description="齿数 z")
    pressure_angle: Num = 20.0
    shift: Num = Field(0.0, description="变位系数 x")
    accuracy: str = Field("8 GB/T 10095.1", description="精度等级")
    tip_tolerance: str = Field("h11", description="齿顶圆直径公差带")
    flank_ra: Num = Field(1.6, description="齿面粗糙度 Ra")
    mate: str | None = Field(None, description="配对齿轮图号（参数表可选行）")
    blank_tolerances: bool = Field(True, description="自动标齿坯公差：轴孔为基准 A，齿顶圆径向圆跳动、基准端面圆跳动（按分度圆直径与精度等级查表）")


class LocalHardening(Strict):
    """局部表面淬火：整段轴颈（step:i），图上沿表面画粗点画线（GB/T 4457.4），技术要求写硬度与淬硬层深。"""
    target: str = Field(description="step:<段号>")
    method: Literal["induction", "flame"] = Field("induction", description="induction=高频感应淬火，flame=火焰淬火")
    hardness: str | None = Field(None, description="如 50~55HRC；缺省按材料取经验值")
    depth: str | None = Field(None, description="淬硬层深，如 1~1.5（mm）；缺省按材料")


class HeatTreatment(Strict):
    """整体热处理 + 局部表面淬火。整体硬度缺省按材料取经验值（45# 调质 220~250HBW）。"""
    process: Literal["quench_temper", "normalize", "none"] = Field("quench_temper", description="调质 / 正火 / 不做整体热处理")
    hardness: str | None = None
    local: list[LocalHardening] = []


class CastingSpec(Strict):
    """铸件：spec 描述加工后的零件；平台据此生成毛坯（加工面加余量、小孔不铸出、铸孔留加工量）。"""
    process: Literal["sand", "investment", "die"] = Field("sand", description="砂型 / 熔模精铸 / 压铸")
    machined_faces: list[Face] = Field([], description="机加工面（加余量）")
    allowance: Num | None = Field(None, description="单边加工余量 mm；默认 砂型 3 / 精铸 1 / 压铸 0.5")
    draft_deg: Num | None = Field(None, description="拔模斜度；默认 砂型 1.5° / 精铸 0.5° / 压铸 1°")
    min_cast_hole: Num | None = Field(None, description="小于此直径的孔不铸出、铸后钻；默认 砂型 25 / 精铸 8 / 压铸 4")
    fillet: Num = Field(3.0, description="未注铸造圆角 R")
    blank_of: str | None = Field(None, description="（平台内部）本件是该零件的毛坯")


CAST_DEFAULTS = {"sand": {"allowance": 3.0, "draft": 1.5, "min_hole": 25.0, "tol": "DCTG 10"},
                 "investment": {"allowance": 1.0, "draft": 0.5, "min_hole": 8.0, "tol": "DCTG 6"},
                 "die": {"allowance": 0.5, "draft": 1.0, "min_hole": 4.0, "tol": "DCTG 5"}}


class PartSpec(Strict):
    kind: Literal["part"] = "part"
    name: str = Field(description="file base name, e.g. 'J2611-020-003_MountPlate'")
    part_no: str = Field(description="drawing / ERP number")
    title: str = Field(description="part name shown on drawing/BOM, e.g. '气缸安装板'")
    material: str
    category: Literal["machined", "sheet_metal", "weldment", "casting", "purchased", "standard"] = "machined"
    surface_treatment: str | None = Field(None, description="e.g. '阳极氧化黑', '镀锌'")
    parameters: dict[str, Num] = Field({}, description="driving parameters → SW global variables / Creo parameters")
    base: Base
    features: list[Feature] = []
    references: list[RefSpec] = []
    properties: dict[str, str] = {}
    datums: list[Datum] = Field([], description="基准；几何公差引用其字母")
    tolerances: list[GeoTol] = Field([], description="几何公差（形位公差）")
    roughness: list[Roughness] = Field([], description="个别表面粗糙度；其余表面按出图 surface_finish")
    auto_precision: bool = Field(False, description="轴类：按工程惯例自动补基准（两端轴承位 A、B）、轴承位圆柱度（GB/T 275）、"
                                 "其余配合段/齿轮段径向圆跳动（GB/T 1184）与配合面粗糙度；已写的基准/公差不改动")
    casting: CastingSpec | None = Field(None, description="铸件（category=casting）：加工面、余量、拔模、最小铸孔")
    heat_treatment: HeatTreatment | None = Field(None, description="热处理（调质/正火 + 轴颈局部淬火）→ 技术要求与粗点画线")
    gear: GearSpec | None = Field(None, description="直齿圆柱齿轮：底座为 extrude + circle（直径 = 齿顶圆）")

    @model_validator(mode="after")
    def _unique_names(self):
        if self.gear is not None and (self.base.type != "extrude" or self.base.profile.shape != "circle"):
            raise ValueError("gear: base must be extrude with a circle profile (diameter = tip diameter da)")
        if self.casting is not None and self.base.type != "extrude":
            raise ValueError("casting: only extrude-based parts are supported in v1")
        if (self.category == "casting") != (self.casting is not None):
            raise ValueError("category 'casting' needs a casting block (machined_faces, process …) and vice versa")
        names = [f.name for f in self.features] + [r.name for r in self.references]
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            raise ValueError(f"duplicate feature/reference names: {sorted(dup)}")
        holes = {f.name for f in self.features if f.feature == "hole"}
        for r in self.references:
            if r.hole and r.hole not in holes:
                raise ValueError(f"{r.name}: hole {r.hole!r} not defined")
        return self

ShaftBase.model_rebuild()
