"""MCP 服务：把 CadService 暴露为统一工具。Claude Code / Codex 通过 stdio 或 HTTP 连接。"""
from __future__ import annotations

import functools
import traceback
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..errors import NATIVE_ERROR, CadError
from ..ir import AssemblySpec, DrawingSpec, PartSpec
from .service import CadService

INSTRUCTIONS = """\
CAD-MCP：用"设计规格 (spec)"驱动 SolidWorks / Creo 建模与出图。你写 spec，平台编译成原生模型。

标准流程：
1. cad_list_targets → 确认 CAD 与版本；cad_change_begin(project) 开启变更会话（所有写操作都在隔离工作区）。
2. 零件：cad_build_part(spec)。用 parameters 定义驱动参数，尺寸写表达式（如 "L-2*e"）。
   孔用 type=tapped/counterbore/clearance/simple + 公制规格（M6），不要自己算底孔/沉孔尺寸。
   为装配定位建 CS_ 坐标系基准（references），装配只认命名坐标系，不选面不选边。
   与外购件对接的孔用 mount 特征（item=库编码），自动按接口开孔并生成 CS_<name>。
3. 装配：涉及气缸、电机、丝杠、螺栓受力时先 cad_calc 选型（生成计算书），再 cad_search_library 查标准件/外购件 → cad_build_assembly(spec)，组件 source 用 part:/lib:/asm:。
4. 每个零件/装配都要出图：cad_build_drawing(spec)。看返回的 rules/findings，有 error 必须修。
5. 修改：cad_set_parameters 改参数，依赖它的装配和工程图会自动重建。
6. cad_validate → cad_check_interference → cad_export_bom → cad_change_commit(revision)。
   不满意可 cad_checkpoint / cad_change_rollback。

规则：单位 mm/deg/kg；不要覆盖原始文件（平台会拒绝）；遇到错误先读 error.hint。
"""

mcp = MCPServer("cadmcp", instructions=INSTRUCTIONS, version="0.1.0")
_svc: CadService | None = None

RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
WR = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
COMMIT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)


def init(service: CadService) -> MCPServer:
    global _svc
    _svc = service
    return mcp


def svc() -> CadService:
    assert _svc is not None, "service not initialised"
    return _svc


def safe(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw) -> dict[str, Any]:
        try:
            return fn(*a, **kw)
        except CadError as e:
            return e.to_dict()
        except Exception as e:  # noqa: BLE001 — CAD 异常不能让服务崩溃
            return CadError(NATIVE_ERROR, f"{type(e).__name__}: {e}",
                            hint="内部错误，已记录；可重试或回滚").to_dict() | {"trace": traceback.format_exc(limit=3)}
    return wrapper


# ---------- 目标 ----------
@mcp.tool(annotations=RO)
@safe
def cad_list_targets() -> dict[str, Any]:
    """列出可用的 CAD 目标（CAD 类型、版本、节点、状态）。"""
    return svc().list_targets()


@mcp.tool(annotations=RO)
@safe
def cad_select_target(target: str) -> dict[str, Any]:
    """设置默认 CAD 目标（新变更会话使用）。"""
    return svc().select_target(target)


@mcp.tool(annotations=RO)
@safe
def cad_get_capabilities(target: str | None = None) -> dict[str, Any]:
    """查询目标对各能力的支持级别（full/partial/none）。不支持的能力不要尝试。"""
    return svc().capabilities(target)


# ---------- 变更会话 ----------
@mcp.tool(annotations=WR)
@safe
def cad_change_begin(project: str, sources: list[str] | None = None, target: str | None = None) -> dict[str, Any]:
    """开启变更会话：创建隔离工作区（可复制已有设计文件进来），后续所有建模/修改都在其中。"""
    return svc().change_begin(project, sources, target)


@mcp.tool(annotations=RO)
@safe
def cad_change_status() -> dict[str, Any]:
    """当前会话：模型列表、检查点、提交门禁状态。"""
    return svc().change_status()


@mcp.tool(annotations=WR)
@safe
def cad_checkpoint(label: str = "") -> dict[str, Any]:
    """打检查点（工作区快照），之后可回滚到这里。"""
    return svc().checkpoint(label)


@mcp.tool(annotations=DESTRUCTIVE)
@safe
def cad_change_rollback(to: int = 0) -> dict[str, Any]:
    """回滚到检查点（0 = 会话开始）。之后的修改会丢弃。"""
    return svc().rollback(to)


@mcp.tool(annotations=COMMIT)
@safe
def cad_change_commit(revision: str, description: str = "", by: str = "") -> dict[str, Any]:
    """把工作区发布为新修订版本（不可覆盖）。门禁（再生无错、规则无 error、图纸无 error）不通过会被拒绝。
    description 为更改内容（写进图纸更改记录表；首版可省略），by 为更改人。图纸标题栏自动写入版本号。"""
    return svc().commit(revision, description, by)


# ---------- 建模 ----------
@mcp.tool(annotations=WR)
@safe
def cad_build_part(spec: PartSpec) -> dict[str, Any]:
    """按零件 spec 建模（新建或按同名覆盖重建）。返回尺寸、质量、孔标注、设计规则检查结果。
    基准：草图在 XY(Z=0)，实体向 +Z；面名 top/bottom/front(-Y)/back(+Y)/left(-X)/right(+X)。"""
    return svc().build(spec)


@mcp.tool(annotations=WR)
@safe
def cad_build_assembly(spec: AssemblySpec) -> dict[str, Any]:
    """按装配 spec 建装配：组件用 CS_ 坐标系对齐定位（align → to + offset/rotate），可线性阵列。返回 BOM 和初步干涉。"""
    return svc().build(spec)


@mcp.tool(annotations=WR)
@safe
def cad_build_drawing(spec: DrawingSpec) -> dict[str, Any]:
    """按出图 spec 生成工程图：自动选图幅比例、视图、总体尺寸、孔坐标与孔标注、技术要求、标题栏、明细表，并做出图检查。"""
    return svc().build(spec)


@mcp.tool(annotations=WR)
@safe
def cad_set_parameters(name: str, updates: dict[str, float | str]) -> dict[str, Any]:
    """修改模型驱动参数（值或表达式），自动重建该模型及依赖它的装配/工程图。"""
    return svc().set_parameters(name, updates)


# ---------- 读取 ----------
@mcp.tool(annotations=RO)
@safe
def cad_get_model(name: str) -> dict[str, Any]:
    """读取模型：spec、解析结果摘要、规则发现、（装配）BOM、（图纸）尺寸清单。"""
    return svc().get_model(name)


@mcp.tool(annotations=WR)
@safe
def cad_build_frame(spec: dict[str, Any]) -> dict[str, Any]:
    """铝型材机架：给 name/part_no/title、width/depth/height（外形 mm）、profile（默认 PRF-4040）、
    levels（横梁层中心高度，默认底层+顶层）、bracket（角码，默认 BRK-4040），生成立柱+横梁装配并建模。
    返回下料清单 cut_list、型材总长、BOM（含角码/T 螺母/螺钉）。之后可用 asm:<name> 把机架装进总装，
    或在其上加安装板（坐标原点在机架底面中心）。"""
    return svc().build_frame(spec)


@mcp.tool(annotations=WR)
@safe
def cad_calc(kind: Literal["cylinder", "screw_drive", "belt_drive", "bolt"], params: dict[str, Any]) -> dict[str, Any]:
    """选型计算，返回带公式过程的计算书与推荐型号（有变更会话时计算书存入工作区并随版本发布）。
    cylinder: load_kg 或 force_N, orientation horizontal|vertical, stroke_mm, pressure_MPa=0.5, direction push|pull
    screw_drive: load_kg, stroke_mm, speed_mm_s, accel_time_s=0.2, orientation, lead_mm(可选), screw_diameter_mm=16
    belt_drive: load_kg, stroke_mm, speed_mm_s, accel_time_s=0.2, pulley_pd_mm=30
    bolt: size(M6), count, grade 8.8|10.9|12.9, tension_N, shear_N
    先算再建模：用 selection 结果选标准件（cad_search_library）和定尺寸。"""
    return svc().calc(kind, params)


@mcp.tool(annotations=RO)
@safe
def cad_search_library(query: str = "", category: Literal["purchased", "standard"] | None = None) -> dict[str, Any]:
    """搜索标准件/外购件库。返回编码、参数和可用的命名基准。装配中用 source="lib:<code>?L=600"。"""
    return svc().search_library(query, category)


@mcp.tool(annotations=RO)
@safe
def cad_open_existing(path: str, write: bool = False) -> dict[str, Any]:
    """打开已有设计文件（版本保护：新版本 CAD 默认只读打开旧版本文件，旧版本 CAD 拒绝打开新文件）。"""
    return svc().open_existing(path, write)


# ---------- 校验与输出 ----------
@mcp.tool(annotations=RO)
@safe
def cad_validate(name: str | None = None) -> dict[str, Any]:
    """运行设计规则与出图检查，返回提交门禁状态。"""
    return svc().validate(name)


@mcp.tool(annotations=RO)
@safe
def cad_check_interference(name: str) -> dict[str, Any]:
    """装配干涉检查。"""
    return svc().interference(name)


@mcp.tool(annotations=RO)
@safe
def cad_export_bom(name: str) -> dict[str, Any]:
    """导出装配 BOM（CSV，UTF-8 BOM 便于 Excel 打开）。"""
    return svc().export_bom(name)


@mcp.tool(annotations=RO)
@safe
def cad_export(name: str, format: Literal["step", "pdf", "dxf", "svg", "json"]) -> dict[str, Any]:
    """导出文件到会话 out/ 目录。"""
    return svc().export(name, format)
