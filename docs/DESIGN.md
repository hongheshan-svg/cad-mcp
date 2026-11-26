# CAD-MCP 统一平台设计（SolidWorks + Creo，面向 Claude Code / Codex）

> 状态：v0.2（2026-11-26）——平台核心已实现并在模拟 CAD 上跑通；SolidWorks/Creo 实机待测（§16）
> 覆盖版本：SolidWorks 2022 / 2024 / 2026，Creo 7.0 / 9.0 / 10.0
> 首期主环境：SolidWorks 2024 + Creo 10.0

---

## 0. 目标与非目标

**总目标：替代机械工程师的建模与出图工作**——工程师给出需求与关键决策，AI 完成零件建模、装配、工程图、BOM，工程师只做审核签字。

**目标**

0. 以"能直接交给车间加工的图纸"为验收标准，而不是"能操作 CAD"。
1. 一个 MCP 服务（`cadmcp`）同时供 Claude Code 和 Codex 使用，暴露**一套与 CAD 无关的机械工程工具**；AI 不需要知道底层是 SolidWorks 还是 Creo。
2. 六个 CAD 版本并存，不强制升级；每个版本由独立的 Worker 进程驱动。
3. **永不破坏原始设计数据**：所有写操作在隔离工作区里进行，带版本降级保护、快照与回滚。
4. 每次操作都返回**可验证的结果**（再生错误、质量属性、干涉结果、截图），AI 能自我检查和修正。
5. 开源组件（CADapter、CREOSON、Creopyson 等）作为**可替换的参考/依赖**，而不是平台本身。

**非目标（首期）**

- 不做“从一句话生成整台设备”。首期聚焦：读取 → 参数修改 → 零部件替换 → 校验 → 出图/BOM → 保存新版本。
- 不把几百个原生 API 平铺给 AI。原生调用只作为受控的逃生通道（默认关闭）。
- 不替代 PDM/PLM，只与之对接（检出/检入、版本号）。

---

## 1. 对原方案的修正与补充

原方案方向正确（统一接口 + 双适配器 + 多版本兼容），以下几点需要修正或补强，否则会在实施中踩坑：

| # | 问题 | 本设计的处理 |
|---|------|-------------|
| 1 | 直接把 CADapter / 0xFFD / creo-mcp 当 MCP 服务挂到 AI 上，会得到三套互不一致的工具集，Skills 无法复用 | 自研统一 Gateway，开源项目只作为 Adapter 内部依赖或代码参考 |
| 2 | 同一台机器多 SolidWorks 版本时，`SldWorks.Application` 只指向最后注册的版本 | Worker 用**带版本号的 ProgID** 启动/附着（2022=`.30`，2024=`.32`，2026=`.34`），并强烈建议“一台机器/VM 只装一个 SW 大版本” |
| 3 | 单位不一致：SW API 用米/弧度，Creo 取决于模型单位 | 平台统一对外 **mm / deg / kg**，单位换算只在 Adapter 内做 |
| 4 | AI 选取面/边几乎不可靠（实体 ID 不稳定、跨版本不同） | 建立**命名基准规范**（接口平面、安装轴、连接坐标系），工具只接受命名引用，见 §6.3 |
| 5 | CAD 的 Undo 不可靠，“失败回滚”不能依赖 CAD 内部撤销 | 回滚基于**文件快照**（变更会话开始时复制），见 §7 |
| 6 | 大装配（1000–5000 件）读结构树会撑爆模型上下文、且超过客户端工具超时（Codex 默认约 60s） | 分页 + 深度限制 + 摘要返回，全量结果写入工作区文件；长操作走**异步 Job**，见 §5.4 |
| 7 | CREOSON 没有现成的干涉检查、复杂装配约束等能力 | 在 CREOSON（开源 J-Link Java 服务）中**增加自定义命令**，比另起 TOOLKIT 工程更省；只有 J-Link 做不到的才上 Object TOOLKIT C++（需核实授权） |
| 8 | 开源项目许可证/成熟度 | creo-mcp 无明确许可证 → 只读参考，不复制代码；CADapter（Apache-2.0）可引入但需锁定 commit 并做内部审计 |
| 9 | 文中引用的项目版本/测试记录尚未实机验证 | 第 1–3 周用**契约测试**在六个版本上逐项验证，结果写进能力矩阵（§9），以测试结果而非文档为准 |

---

## 1.5 核心决策：设计规格（Design IR）驱动，而不是让 AI 点 CAD

要替代画图，AI 必须能**从零**建模和出图。让 AI 逐步调用"选面→画草图→拉伸"既不稳定也无法检查。本平台改为：

```
需求 ──AI──► 设计规格 spec（JSON：参数、基体、孔、型腔、基准、装配、出图要求）
                 │
                 ├─► IR 解析（CAD 无关）：参数求值、孔标准展开、包围盒、质量估算、孔标注文字
                 ├─► 设计规则检查（CAD 无关）：孔边距、破边、盲孔深度、螺纹深度、孔距、折弯边距…
                 ├─► 出图规划（CAD 无关）：图幅/比例、投影布局、坐标尺寸、孔标注、技术要求、标题栏、明细表
                 │
                 └─► 适配器"编译"成原生操作计划 ──执行──► SolidWorks / Creo 原生模型与工程图
                                                          └─► 回读：再生错误、实测质量、尺寸数量 → 与 IR 对账
```

| 好处 | 说明 |
|------|------|
| 稳定 | 孔尺寸查标准、坐标由程序算，AI 只决定"要什么" |
| 可检查 | 建模前就能做规则检查；建模后用 CAD 实测质量与估算对账（偏差 >3% 报警） |
| 一份描述两个 CAD | 同一 spec 编译到 SW 或 Creo |
| 参数化 | spec.parameters → SW 全局变量 / Creo 参数；改参数自动重建零件及依赖它的装配与图纸 |
| 可版本化 | spec 是 JSON，随模型一起保存，变更可 diff、可回滚 |

**代价（能力边界）**：spec 描述不了的零件 AI 就画不了。v0 覆盖：

| 类型 | 状态 |
|------|------|
| 板/块类机加工件（外形、各类孔、型腔、凸台、倒角圆角） | ✅ |
| 阶梯轴 | ✅ |
| 简单钣金（底板 + 直边折弯、展开尺寸） | ✅ |
| 标准件/外购件装配（坐标系对齐、线性阵列） | ✅ |
| 键槽、退刀槽、斜孔、型材端面加工、焊接件、铸件、复杂曲面 | ❌ 按需扩展 spec |

**下一步必须做**：让工程师统计常画零件类型及占比，按占比决定 spec 扩展顺序。

**退路**：spec 覆盖不了的零件，走"打开现有模型 → 改参数/替换"或受控原生调用（`cad_native_call`，默认关闭）。

---

## 2. 总体架构

```
 ┌───────────────────────┐   ┌───────────────────────┐
 │      Claude Code      │   │       Codex CLI       │
 │  + 企业 Skills / CLAUDE.md │ │  + AGENTS.md / Skills │
 └──────────┬────────────┘   └──────────┬────────────┘
            │  MCP (stdio 本机 / Streamable HTTP 远程)
            ▼
 ┌──────────────────────────────────────────────────────┐
 │                cadmcp Gateway (Python, FastMCP)       │
 │  ┌────────────┐ ┌────────────┐ ┌───────────────────┐ │
 │  │ 统一工具层  │ │ 策略/审批   │ │ 变更会话 & 快照     │ │
 │  │ (~30 tools)│ │ Policy     │ │ ChangeSet/Snapshot │ │
 │  └─────┬──────┘ └────────────┘ └───────────────────┘ │
 │  ┌─────┴──────┐ ┌────────────┐ ┌───────────────────┐ │
 │  │ 路由/调度   │ │ 能力矩阵    │ │ 审计日志 / Job 队列 │ │
 │  │ Router     │ │ Capability │ │ Audit / Jobs      │ │
 │  └─────┬──────┘ └────────────┘ └───────────────────┘ │
 └────────┼─────────────────────────────────────────────┘
          │ Worker 协议：JSON-RPC 2.0 over HTTP（localhost 或内网 + token）
   ┌──────┴───────────────┬──────────────────────┬───────────────...
   ▼                      ▼                      ▼
 ┌──────────────┐   ┌──────────────┐      ┌──────────────┐
 │ SW Worker    │   │ SW Worker    │      │ Creo Worker  │   × 每个 CAD 会话一个
 │ ver=2024     │   │ ver=2022     │      │ ver=10.0     │
 │ SolidWorks   │   │ SolidWorks   │      │ Creopyson ──►│
 │ Adapter      │   │ Adapter      │      │ CREOSON(+ext)│
 │ (COM/pywin32)│   │ (COM/pywin32)│      │  ──► J-Link  │
 └──────┬───────┘   └──────┬───────┘      └──────┬───────┘
        ▼                  ▼                     ▼
   SOLIDWORKS 2024    SOLIDWORKS 2022         Creo 10.0
        └──────────── 工作区（本地 SSD）◄──── PDM/PLM/NAS 检出 ───┘
```

**三层职责划分**

| 层 | 职责 | 不做什么 |
|----|------|---------|
| Gateway | MCP 协议、统一工具 schema、参数校验、单位/引用规范化、路由、策略审批、变更会话、快照、审计、Job | 不直接调用任何 CAD API |
| Worker | 绑定**一个** CAD 进程（一个版本一个会话），串行执行命令，心跳上报版本/能力/状态 | 不做业务决策，不管文件版本策略 |
| Adapter | 把统一命令翻译成原生 API（SW COM / CREOSON / J-Link 扩展），返回统一结构 | 不暴露原生对象到 Worker 外 |

为什么把 Gateway 和 Worker 分进程：

- CAD COM 调用会卡死/崩溃，Worker 挂掉不影响 Gateway，可自动重启并报告；
- 同一个 Gateway 能同时连本机的 SW 2024 和远程节点上的 Creo 7；
- Worker 协议与语言无关：首期 SW Adapter 用 Python（pywin32），若遇到 COM ByRef/Variant 问题严重，可单独把 SW Worker 换成 C#/.NET 实现，Gateway 不变。

---

## 3. 部署拓扑与多版本共存

### 3.1 推荐拓扑

| 模式 | 适用 | 说明 |
|------|------|------|
| **A. 单机模式**（首期） | 工程师在自己的 Windows 工作站上跑 Claude Code/Codex | Gateway 以 stdio 启动，Worker 在同机 localhost。零网络配置 |
| **B. 节点池模式**（第 2 阶段） | 旧版本（SW 2022、Creo 7）集中在少量节点上 | 每个节点运行 `cadmcp-node`（Worker 管理器），向 Gateway 注册；Gateway 可本地也可集中部署 |

### 3.2 多版本共存规则

1. **SolidWorks：一台机器（或 VM）只装一个大版本。** 多版本共存时 COM 注册、Toolbox、插件、文件关联会互相覆盖。确需共存时，Worker 必须用带版本号的 ProgID：

   | 版本 | Revision | ProgID |
   |------|----------|--------|
   | 2022 | 30 | `SldWorks.Application.30` |
   | 2024 | 32 | `SldWorks.Application.32` |
   | 2026 | 34 | `SldWorks.Application.34` |

   并在连接后校验 `ISldWorks.RevisionNumber()` 前缀，不匹配直接拒绝。
2. **Creo：可在一台机器多版本共存**，每个 Creo 会话配一个独立 CREOSON 实例（不同端口，如 9056/9057/9058），每个 CREOSON 指向对应版本的安装目录与 J-Link。
3. **每个 Worker 一个 CAD 进程、一个命令队列**，命令严格串行。不同 Worker 之间可并行。
4. Worker 注册信息（示例）：

```yaml
# cadmcp-node.yaml
node_id: ws-mech-017
workers:
  - id: sw2024
    cad: solidworks
    version: "2024"           # 期望版本，启动后用实际 revision 校验
    progid: SldWorks.Application.32
    visible: true             # 首期可见，方便工程师旁观
    port: 7311
  - id: creo10
    cad: creo
    version: "10.0"
    creoson: { host: 127.0.0.1, port: 9058, home: "D:/creoson-creo10" }
    creo_cmd: "C:/PTC/Creo 10.0.x/Parametric/bin/parametric.bat"
    port: 7312
workspace_root: "D:/cadmcp/workspaces"
```

---

## 4. 组件设计

### 4.1 Gateway（`cadmcp`）

- 框架：Python 3.12 + 官方 MCP Python SDK（FastMCP），同时支持 `stdio` 和 `streamable-http`。
- 模块：
  - `tools/`：统一工具定义（pydantic 模型 = 输入/输出 schema）。
  - `router.py`：根据 `target`（worker id）或文档所属会话选择 Worker；未指定时用“当前活动目标”。
  - `policy.py`：读 `policy.yaml`，判定工具是否允许、是否需要变更会话、是否允许版本升级保存。
  - `changeset.py`：变更会话、快照、回滚、提交（§7）。
  - `jobs.py`：长操作异步化，返回 `job_id`，配合 MCP progress 通知。
  - `audit.py`：每次调用写 JSONL（时间、客户端、工具、参数、Worker、耗时、结果摘要、错误）。
  - `capabilities.py`：合并 Worker 上报能力与静态能力矩阵（§9）。

### 4.2 Worker（`cadmcp-worker`）

- 一个进程绑定一个 CAD 会话：启动或附着 CAD → 校验版本 → 注册 → 进入命令循环。
- 单线程执行（COM 需 STA；CREOSON 本身也是串行），队列 + 每命令超时 + 看门狗。
- 心跳内容：`{cad, version, revision, sp, pid, status: idle|busy|hung|dead, open_docs, capabilities_hash}`。
- 卡死处理：命令超过硬超时 → 标记 `hung` → 通知 Gateway → 可配置自动 kill 并重启 CAD（未保存修改丢失，但因所有修改都在快照工作区内，可回滚到会话开始）。

### 4.3 Adapter 接口（Python 协议）

```python
class CadAdapter(Protocol):
    # 会话
    def info(self) -> CadInfo: ...                       # 版本、SP、单位、授权模块
    def capabilities(self) -> dict[str, CapabilityLevel]: ...

    # 文档
    def open(self, path: str, readonly: bool) -> DocRef: ...
    def close(self, doc: DocRef, save: bool = False) -> None: ...
    def save_as(self, doc: DocRef, path: str, copy: bool) -> SaveResult: ...
    def file_saved_version(self, path: str) -> str: ...  # 文件最后保存的 CAD 版本

    # 读取
    def structure(self, doc: DocRef, depth: int, page: Page) -> StructureNode: ...
    def feature_tree(self, doc: DocRef, page: Page) -> list[FeatureNode]: ...
    def get_parameters(self, doc: DocRef, filter: ParamFilter) -> list[Param]: ...
    def mass_properties(self, doc: DocRef) -> MassProps: ...

    # 修改（都在变更会话内）
    def set_parameters(self, doc: DocRef, updates: list[ParamUpdate]) -> list[ParamResult]: ...
    def replace_component(self, asm: DocRef, comp: CompRef, new_path: str, all_instances: bool) -> OpResult: ...
    def insert_component(self, asm: DocRef, path: str, placement: Placement) -> CompRef: ...
    def add_mate(self, asm: DocRef, mate: MateSpec) -> OpResult: ...
    def create_part(self, spec: PartSpec) -> DocRef: ...  # 草图+特征的声明式描述

    # 验证
    def regenerate(self, doc: DocRef) -> RegenResult: ...   # 含每个失败特征的错误码
    def interference(self, asm: DocRef, opts: InterfOpts) -> list[Interference]: ...
    def clearance(self, asm: DocRef, pairs: list[CompPair], min_mm: float) -> list[Clearance]: ...

    # 输出
    def export_bom(self, asm: DocRef, opts: BomOpts) -> BomTable: ...
    def make_drawing(self, doc: DocRef, template: str, views: ViewSpec) -> DocRef: ...
    def export(self, doc: DocRef, fmt: str, path: str) -> str: ...  # step/pdf/dxf/png
    def screenshot(self, doc: DocRef, view: str) -> bytes: ...
```

所有方法：输入/输出都是平台单位（mm/deg/kg），抛出的异常统一映射为 `CadError(code, message, native_code, hint)`。

---

## 5. 统一 MCP 工具接口

### 5.1 设计原则

1. **工具数量控制在 30 个左右**，按任务而不是按 API 划分；每个工具描述写清楚“何时用 / 不要何时用”。
2. **每个工具都带 MCP 注解**：`readOnlyHint` / `destructiveHint` / `idempotentHint`，让 Claude Code / Codex 的审批机制正确生效（只读工具可放行，写工具需确认）。
3. **写操作必须在变更会话内**（`cad_change_begin` 之后），否则 Gateway 直接拒绝并提示。
4. **返回值 = 结果 + 证据**：写操作默认附带再生状态；可选附带截图。
5. **大结果分页 + 落盘**：超过阈值（如 200 条或 30KB）时返回摘要 + `artifact_path`（工作区内 JSON/CSV），AI 需要时再读。
6. 所有工具都有可选 `target` 参数（worker id，如 `sw2024`）；不传时使用当前活动目标。

### 5.2 工具清单

| 分组 | 工具 | 读/写 | 说明 |
|------|------|------|------|
| 会话 | `cad_list_targets` | R | 列出可用 Worker：CAD、版本、状态、所在节点 |
| | `cad_select_target` | R | 设置当前活动目标 |
| | `cad_get_capabilities` | R | 当前目标对每个工具的支持级别（full/partial/none）及限制说明 |
| 项目 | `cad_open` | R | 打开模型（默认只读）；返回 `doc_id`、文件保存版本、是否会被升级 |
| | `cad_clone_project` | W | 从历史项目复制到新项目（SW 用 Pack and Go；Creo 用 Save a Copy + 重命名），自动改名规则 |
| | `cad_list_open_docs` | R | 当前打开文档 |
| 变更会话 | `cad_change_begin` | W | 开始变更会话：检出/复制到工作区、快照、锁定；返回 `change_id` |
| | `cad_change_status` | R | 当前会话已执行操作、受影响文件、再生状态 |
| | `cad_change_commit` | W | 校验通过后保存为新版本（新文件名/新修订号），可选检入 PDM |
| | `cad_change_rollback` | W | 回滚到会话开始（或某个检查点） |
| | `cad_checkpoint` | W | 会话内打检查点 |
| 读取 | `cad_get_structure` | R | 装配结构树（深度、分页、过滤：按名称/属性/是否标准件） |
| | `cad_get_features` | R | 零件特征树（含抑制状态、错误状态） |
| | `cad_get_parameters` | R | 全局变量/方程式/尺寸/自定义属性（SW）；参数/尺寸/关系（Creo）统一成 `Param` |
| | `cad_get_references` | R | 列出模型中的命名基准（接口平面/轴/坐标系），供装配与约束使用 |
| | `cad_where_used` | R | 某零件在装配中的所有实例及其约束 |
| 修改 | `cad_set_parameters` | W | 批量改参数，默认改完自动再生并返回结果 |
| | `cad_replace_component` | W | 替换零部件（如换气缸/电机型号），返回失效约束列表 |
| | `cad_insert_component` | W | 插入零部件（标准件库 ID 或路径），按命名基准放置 |
| | `cad_add_constraint` | W | 添加装配约束（只接受命名引用） |
| | `cad_create_part` | W | 用声明式 PartSpec 创建零件（草图 + 拉伸/旋转/孔/阵列等） |
| | `cad_suppress` | W | 抑制/解除抑制特征或零部件 |
| 校验 | `cad_regenerate` | R* | 重建并返回所有错误/警告特征 |
| | `cad_check_interference` | R | 干涉检查（可限定组件集合，忽略螺纹/标准件配合） |
| | `cad_check_clearance` | R | 最小间隙检查 |
| | `cad_mass_properties` | R | 质量、重心、惯性 |
| | `cad_validate_design` | R | 执行企业规则（命名、属性完整性、材质、最小壁厚、标准件编码等），规则在 Gateway 中实现，跨 CAD 复用 |
| 输出 | `cad_generate_drawing` | W | 按公司模板生成工程图（视图、尺寸导入、BOM 表、标题栏属性） |
| | `cad_export_bom` | R | 从结构树 + 自定义属性生成 BOM（不依赖工程图表格），输出 CSV/XLSX |
| | `cad_export` | R | 导出 STEP/PDF/DXF/PNG 到工作区 |
| | `cad_screenshot` | R | 指定视角截图，供多模态模型目视检查 |
| 异步 | `cad_job_status` / `cad_job_cancel` | R | 查询/取消长任务 |
| 逃生 | `cad_native_call` | W | 原生 API 调用（默认关闭；仅管理员 policy 打开，且仅在变更会话内） |

\* `cad_regenerate` 会改变模型的内存状态但不落盘，归为只读+需会话。

### 5.3 关键工具 schema 示例

```jsonc
// cad_set_parameters 输入
{
  "doc_id": "d-7f3a",
  "updates": [
    { "name": "Frame_Length", "value": 1450, "unit": "mm" },
    { "name": "Frame_Width",  "value": 820,  "unit": "mm" },
    { "name": "Material",     "value": "AL6061-T6", "kind": "property" }
  ],
  "regenerate": true,
  "screenshot": "iso"          // 可选：none | iso | front | top ...
}

// 输出（统一结果信封）
{
  "ok": false,
  "change_id": "c-20261126-003",
  "applied": [
    { "name": "Frame_Length", "old": 1200, "new": 1450, "unit": "mm" },
    { "name": "Frame_Width",  "old": 800,  "new": 820,  "unit": "mm" }
  ],
  "rejected": [
    { "name": "Material", "code": "PARAM_NOT_FOUND",
      "hint": "该文档无名为 Material 的自定义属性；可用：Material_Spec, 材料" }
  ],
  "regen": {
    "status": "warnings",     // ok | warnings | errors
    "errors": [],
    "warnings": [
      { "feature": "Mounting_Plate_Hole_Pattern", "code": "SW_FEATURE_WARN_DANGLING",
        "message": "阵列参考尺寸超出板边界" }
    ]
  },
  "screenshot_path": "D:/cadmcp/workspaces/c-20261126-003/shots/0004_iso.png",
  "duration_ms": 3820,
  "target": { "id": "sw2024", "cad": "solidworks", "version": "2024 SP3" }
}
```

### 5.4 长任务（Job）

- 干涉检查、大装配打开、出图、BOM、导出 STEP 预计 > 20s 的，Gateway 立即返回：
  `{ "job_id": "j-91", "status": "running", "eta_s": 90 }`，并发送 MCP progress 通知。
- AI 用 `cad_job_status` 轮询。这样不受 Claude Code / Codex 工具超时限制。
- Codex 侧仍建议在配置里调大 `tool_timeout_sec`（如 180）。

### 5.5 统一错误码

`TARGET_UNAVAILABLE`、`TARGET_BUSY`、`CAPABILITY_UNSUPPORTED`、`NO_CHANGE_SESSION`、`POLICY_DENIED`、`VERSION_DOWNGRADE_RISK`、`DOC_NOT_FOUND`、`PARAM_NOT_FOUND`、`REF_NOT_FOUND`、`REGEN_FAILED`、`CAD_TIMEOUT`、`CAD_CRASHED`、`NATIVE_ERROR`。
每个错误都带 `hint`，写给 AI 看：下一步该怎么做。

---

## 6. 统一数据模型

### 6.1 单位

对外一律 **mm、deg、kg、N、N·m、s**。Adapter 负责换算：SW API 内部是 m / rad；Creo 读取模型单位系统（mmNs、inlbs 等）后换算。参数若为无量纲或字符串，`unit` 为空。

### 6.2 参数统一

| 统一 `kind` | SolidWorks | Creo |
|------------|-----------|------|
| `global` | 全局变量（Equations Manager） | 模型参数（Parameters） |
| `dimension` | 尺寸 `D1@Sketch1@Part.SLDPRT` | 尺寸 `d12`（建议改为命名尺寸） |
| `equation` | 方程式 | 关系（Relations） |
| `property` | 自定义属性 / 配置特定属性 | 参数（字符串型，用于 BOM） |
| `config` | 配置（Configuration） | 族表实例（Family Table instance） |

### 6.3 命名引用（关键）

AI 不选面、不选边。所有装配放置与约束只接受**命名引用**：

```
<component_path>/<reference_name>
例：Frame-1/MP_Top          (机架实例 1 的顶面安装平面)
    Cylinder_SC32x50-2/AX_Rod  (气缸实例 2 的活塞杆轴线)
    Base_Plate-1/CS_Mount       (底板的安装坐标系)
```

**企业建模规范（需配套推行）**：标准件、外购件、常用机构件都要建好命名基准：

| 前缀 | 含义 |
|------|------|
| `MP_` | Mounting Plane 安装平面 |
| `AX_` | 轴线（安装轴、运动轴） |
| `CS_` | 连接坐标系（首选：一个坐标系对齐就完成定位） |
| `PT_` | 关键点 |

`cad_get_references` 会列出这些基准；没有基准的模型只能做参数修改/替换，不能可靠地新增约束——这是平台能力的硬边界，要在 Skills 中写明。

### 6.4 结构节点

```jsonc
{
  "path": "Machine-1/Frame_Asm-1/Upright-3",
  "name": "Upright", "instance": 3,
  "file": "Upright_40x40_L1450.SLDPRT",
  "type": "part",                // part | assembly
  "config": "Default",
  "suppressed": false, "virtual": false,
  "standard_part": { "code": "MIS-HFS8-4040", "source": "misumi" },  // 来自属性
  "properties": { "PartNo": "J2611-020-003", "Material": "AL6063" },
  "children_count": 0
}
```

---

## 7. 文件安全：工作区、版本保护、快照与回滚

### 7.1 工作区

```
D:/cadmcp/workspaces/<change_id>/
  ├─ src/          # 从 PDM/NAS 检出或复制的原始文件（只读）
  ├─ work/         # AI 实际修改的文件
  ├─ snapshots/    # 检查点（硬链接/复制）
  ├─ shots/        # 截图
  ├─ out/          # 导出的 BOM/工程图/STEP
  └─ changeset.jsonl  # 本会话所有操作日志
```

- 原始目录（PDM/NAS）永远只读；Gateway 拒绝对 `workspace_root` 以外的路径执行保存。
- 装配引用重定向：SW 用 Pack and Go 复制到 `work/`；Creo 复制到 `work/` 后设置 working directory 并只从此目录检索。

### 7.2 版本降级保护

打开前读取文件的保存版本（SW：`ISldWorks.VersionHistory`；Creo：文件头版本信息）：

| 情况 | 策略 |
|------|------|
| 文件版本 = Worker 版本 | 正常 |
| 文件版本 < Worker 版本（如 2022 文件在 2026 打开） | 默认**只读**；如要修改，必须在 policy 中允许 `allow_upgrade_save`，且只能另存为新文件，结果中显式标记 `upgraded_from: 2022` |
| 文件版本 > Worker 版本 | 返回 `VERSION_DOWNGRADE_RISK`，并推荐合适的 Worker |

**路由建议**：Gateway 自动按“文件保存版本”优先选择同版本 Worker；这就是为什么需要保留 SW 2022 / Creo 7 Worker。

### 7.3 变更会话生命周期

```
cad_change_begin(project, target)
   ├─ 检出/复制 → work/，记录源文件哈希
   └─ 快照 #0
cad_set_parameters / replace / insert ...   （每步记录到 changeset.jsonl）
cad_checkpoint  → 快照 #n（可选）
cad_regenerate / interference / validate    （提交前必须通过的检查由 policy 定义）
cad_change_commit(rev_rule)
   ├─ 保存到新文件名/新修订号（绝不覆盖 src/）
   ├─ 生成变更报告（参数前后值、替换清单、校验结果、截图）
   └─ 可选：检入 PDM 并写回修订号
cad_change_rollback(to=#k) → 关闭文档 → 恢复快照 → 重新打开
```

### 7.4 Policy 示例

```yaml
# policy.yaml
defaults:
  require_change_session_for_writes: true
  allow_upgrade_save: false
  commit_requires: [regen_ok, validate_design]
  max_open_assembly_components: 8000
tools:
  cad_native_call: { enabled: false }
  cad_change_commit:
    require_human_confirm: true      # 通过 MCP elicitation 或客户端审批
paths:
  read_allow:  ["\\\\nas01\\design\\", "D:/PDM_Vault/"]
  write_allow: ["D:/cadmcp/workspaces/"]
```

---

## 8. Adapter 实现要点

### 8.1 SolidWorks Adapter

| 能力 | 主要 API | 备注 |
|------|---------|------|
| 连接 | `win32com.client.Dispatch("SldWorks.Application.32")` / `GetActiveObject` | 用 makepy 为每个版本生成早绑定类型库；CADapter 的 typelib 方案可直接借鉴 |
| 打开/另存 | `OpenDoc6`、`Extension.SaveAs3`（`swSaveAsOptions_Copy`） | 静默模式、处理缺失引用对话框 |
| 克隆项目 | `Extension.GetPackAndGo` + `SavePackAndGo` | 支持前后缀改名 |
| 参数 | `EquationMgr`、`Parameter("D1@Sketch1")`、`CustomPropertyManager` | 注意配置特定属性 |
| 结构树 | `Configuration.GetRootComponent3` 递归 `GetChildren` | 大装配用轻化模式打开、只读属性时不解析 |
| 再生 | `ForceRebuild3`，遍历 `Feature.GetErrorCode2` | 输出失败特征清单 |
| 替换 | `AssemblyDoc.ReplaceComponents2` | 之后检查 `Mate2.Status` 找失效配合 |
| 配合 | `AssemblyDoc.AddMate5` / `CreateMate` + `SelectByID2` 命名基准 | 只用命名基准选择 |
| 干涉 | `AssemblyDoc.InterferenceDetectionManager` | 大装配走 Job |
| 质量 | `Extension.CreateMassProperty` | |
| 工程图 | `NewDocument`(模板) + `Create3rdAngleViews2`/`CreateDrawViewFromModelView3` + `InsertModelAnnotations3` | 首期只做“标准三视图+轴测+BOM 表+标题栏” |
| 截图 | `ModelView` 设置方向 + `SaveAs` PNG | |

**Python vs C# 决策**：首期 Python（与 Gateway 同语言、可复用 CADapter 代码）。若契约测试中 ByRef 输出参数、`SelectByID2` 回调、事件等问题超过可接受范围，再把 SW Worker 用 C#（.NET Framework 4.8 + 对应版本 interop dll）重写——Worker 协议不变。

### 8.2 Creo Adapter

```
Creo Worker (Python)
   └─ Creopyson ──HTTP JSON──► CREOSON server ──J-Link──► Creo
                                   └─ 企业扩展命令（Java，J-Link）
```

| 能力 | 实现 | 备注 |
|------|------|------|
| 打开/保存/复制 | CREOSON `file` 命令组 | `creo_set_creo_version`：Creo 7 需显式设置 |
| 参数/尺寸 | `parameter`、`dimension` 命令组 | 建议模板中尺寸命名化 |
| 关系 | `file` / `relations` 相关命令 | |
| 族表 | `familytable` 命令组 | 对应统一 `config` |
| 结构/BOM | `bom` 命令组 + 参数 | |
| 再生 | `file_regenerate` | 失败特征需扩展命令读取详细状态 |
| 装配 | `file_assemble`（含约束） | 复杂约束走扩展 |
| 质量 | `file_massprops` | |
| 工程图 | `drawing` 命令组 | 首期：模板 + 一般视图 + 投影视图 + 表格 |
| **干涉/间隙** | **扩展命令**：J-Link `pfcInterference` 全局干涉评估 | CREOSON 原生没有 |
| **特征失败详情** | **扩展命令** | |
| 截图 | `interface_export_image` | |

**⚠ 从零建模是 Creo 侧最大风险**：J-Link / CREOSON 不能直接创建草图特征。三个选项：

| 方案 | 能力 | 成本 |
|------|------|------|
| A. TOOLKIT 授权（元素树 `ProFeatureCreate`） | 完整 | 需购买授权、C/C++ 开发 |
| B. 起始件 + UDF 库 + J-Link 放置 UDF（`compile.py` 已按此编译） | 板件/轴类够用 | 需 Creo 工程师一次性建 UDF 库 |
| C. Mapkey/Trail 回放 | 脆弱 | 不推荐 |

建议：先确认是否已有 TOOLKIT 授权；没有则走 B。首期 Creo 侧以"打开现有模型→改参数→再生→BOM/导出"为主。

**扩展 CREOSON 的方式**：CREOSON 是开源 Java（基于 J-Link），按它的 command handler 结构新增 `cadmcp_*` 命令，打成企业内部分支包，**每个 Creo 版本分别编译测试**（J-Link jar 随版本变化）。J-Link 做不到的（个别高级几何/出图功能），再评估 Object TOOLKIT C++ 及其授权成本。

---

## 9. 能力矩阵与版本兼容

能力矩阵由**契约测试自动生成**，不是手写的期望值：

```yaml
# capabilities.generated.yaml （示意，值来自测试结果）
solidworks:
  "2024": { cad_set_parameters: full, cad_replace_component: full,
            cad_generate_drawing: partial, cad_check_interference: full, ... }
  "2022": { ... }
creo:
  "10.0": { cad_set_parameters: full, cad_check_interference: full(ext), ... }
  "7.0":  { cad_check_interference: partial, notes: "需 creo_set_creo_version(7)" }
```

- `cad_get_capabilities` 返回该矩阵当前目标的那一列，Skills 在执行前先查。
- 工具调用时若能力为 `none`，Gateway 直接返回 `CAPABILITY_UNSUPPORTED` + 替代方案提示，而不是让 CAD 报一个看不懂的错。

---

## 10. Claude Code / Codex 接入

### 10.1 单机（stdio）

**Claude Code**（项目级 `.mcp.json`，随仓库分发）：

```json
{
  "mcpServers": {
    "cad": {
      "command": "cadmcp",
      "args": ["serve", "--stdio", "--node-config", "D:/cadmcp/cadmcp-node.yaml"]
    }
  }
}
```

或：`claude mcp add --scope user cad -- cadmcp serve --stdio`

**Codex**（`~/.codex/config.toml`）：

```toml
[mcp_servers.cad]
command = "cadmcp"
args = ["serve", "--stdio", "--node-config", "D:/cadmcp/cadmcp-node.yaml"]
tool_timeout_sec = 180
startup_timeout_sec = 60
```

### 10.2 集中部署（Streamable HTTP）

Gateway 以 `cadmcp serve --http :8765` 运行，客户端用 URL 配置，鉴权用每人一个 token（便于审计到人）。

### 10.3 客户端侧指令

- 仓库根目录同时放 `CLAUDE.md` 和 `AGENTS.md`（内容相同，或 `AGENTS.md` 引用 `CLAUDE.md`），写入：永远先 `cad_change_begin`；修改后必须 `cad_regenerate` + `cad_validate_design`；不确定命名引用时先 `cad_get_references`；禁止要求用户手工选择几何。
- Skills（§11）以 `SKILL.md` 形式放在仓库 `skills/`，两个客户端都能加载。

---

## 11. MCP 与 Skills 的分工

| 层 | 内容 | 例子 |
|----|------|------|
| MCP 工具（本平台） | 软件操作，CAD 无关 | `cad_set_parameters`、`cad_check_interference` |
| 工程计算库（Python 包 `mecalc`，也作为 MCP 工具暴露） | 可验证的计算，**不靠 prompt** | 气缸推力、丝杆寿命、电机惯量比、型材挠度 |
| Skills | 工作流 + 公司规范 + 何时调用哪些工具/计算 | `machine-frame`、`fixture-design`、`design-review` |
| 知识库/RAG | 历史设备、标准件库、选型手册 | 供 Skills 检索 |

建议把工程计算暴露为第二个小 MCP 服务 `mecalc`（`calc_cylinder_force`、`calc_ballscrew_life`、`calc_servo_sizing`、`calc_beam_deflection`……），与 CAD 无关、无需 Windows，可单元测试，也方便工程师单独审核公式。

---

## 12. 测试策略

1. **契约测试（每个 CAD 版本一套环境）**：同一组测试用例（`tests/contract/`）对 6 个 Worker 逐一运行，输出能力矩阵。用例针对统一工具，而非原生 API。
2. **金样模型**：准备 3 套公司真实模型（单工位夹具 / 上下料机构 / 整机），每套在 SW 和 Creo 各一份（如有），并为每个版本保存一份“该版本原生保存”的副本，用来测版本保护逻辑。
3. **结果断言**：不仅断言“调用成功”，还断言几何结果：质量属性（容差 0.1%）、包围盒、特征数、BOM 行数、干涉对数。
4. **Mock Adapter**：Gateway、Policy、ChangeSet、Job 全部在 macOS/Linux 上用 Mock Adapter 跑 CI；只有契约测试需要 Windows + CAD。
5. **AI 端到端评测**：把原方案的 12 项验收清单写成脚本化任务，用 Claude Code / Codex 非交互模式批量跑，记录成功率、工具调用次数、返工点。

---

## 13. 代码仓库结构（v0.2 实际）

```
cad-mcp/
├─ src/cadmcp/
│  ├─ ir/                 # 设计规格：part / assembly / drawing 模型 + 解析（resolve、assembly_resolve）
│  ├─ expr.py             # 安全参数表达式（映射 SW 方程式 / Creo 关系）
│  ├─ standards.py        # 公制螺纹底孔、过孔、沉孔标准
│  ├─ materials.py        # 材料与密度
│  ├─ library.py + data/library.yaml   # 标准件/外购件库（带 CS_ 基准）
│  ├─ rules.py            # 设计规则（R001–R008）
│  ├─ detailing.py        # 出图规划 + 出图检查（D001–D008）
│  ├─ render_svg.py       # 工程图 SVG 预览（mock 目标 / AI 目视自检）
│  ├─ versioning.py       # SW ProgID/版本号映射、版本降级保护
│  ├─ policy.py / changeset.py         # 策略、隔离工作区、快照、回滚、不可覆盖发布
│  ├─ adapters/
│  │  ├─ base.py, native.py, mock.py
│  │  ├─ solidworks/      # compile.py（IR→原生操作，可测）+ executor.py（COM 执行）+ adapter.py
│  │  └─ creo/            # compile.py（IR→UDF 计划）+ adapter.py（Creopyson）
│  ├─ gateway/            # service.py（核心服务）+ server.py（MCP 工具，19 个）
│  └─ cli.py              # cadmcp serve / doctor
├─ examples/cylinder_station.py        # 示例：气缸推料工位（底板+立柱+安装板+气缸）
├─ skills/mechanical-drawing/SKILL.md  # AI 出图工作流
├─ scripts/win/           # Windows 安装与实机探针
├─ config/                # 节点配置、策略、Codex 配置示例
├─ tests/                 # 15 个测试（mock，无需 CAD）
├─ .mcp.json, CLAUDE.md, AGENTS.md
└─ docs/DESIGN.md, docs/WINDOWS_TEST.md
```

一期为单包结构，Worker 与 Gateway 同进程（`Target` + 锁串行化）；节点池模式时再拆为独立 Worker 进程，适配器接口不变。

## 14. 分阶段计划（替代原 90 天表）

| 阶段 | 周 | 交付 | 验收标准 |
|------|----|------|---------|
| P0 骨架 | 1–2 | core 数据模型、Gateway、Mock Adapter、ChangeSet/快照、审计；Claude Code 与 Codex 都能连上并跑通 mock | CI 全绿；两个客户端能完成 mock 的“改参数→再生→提交” |
| P1 只读 | 3–4 | SW 2024 + Creo 10：打开、结构树、参数、质量、再生、截图、BOM | 三套金样模型结构树与 BOM 和人工结果一致 |
| P2 修改 | 5–7 | 改参数、替换零部件、插入+命名基准约束、干涉（含 CREOSON 扩展） | 验收清单第 1–9 项在两个主版本通过 |
| P3 兼容 | 6–8（并行） | SW 2022/2026、Creo 7/9 契约测试 → 能力矩阵；版本保护路由 | 6 个版本能力矩阵生成；降级保护用例 100% 通过 |
| P4 输出 | 8–9 | 工程图（公司模板）、BOM 导出、`cad_validate_design` 规则 | 出图返工时间 vs 人工基线 |
| P5 Skills | 9–11 | `mecalc` + 首批 Skills（机架、夹具、气缸/电机替换、design-review） | 验收清单 12 项端到端，AI 成功率指标 |
| P6 集成 | 11–12 | PDM/Windchill 检出检入、节点池模式、推广培训 | 试点小组真实项目使用 2 周 |

---

## 15. 待确认事项

1. 每个版本的确切 SP/维护版本号（SW 2024 SPx、Creo 10.0.x.y）。
2. 是否有 PDM（SOLIDWORKS PDM / Windchill / 其他），以及 API 访问权限。
3. Creo 是否已有 TOOLKIT（Object TOOLKIT C++）授权；J-Link 是否已安装（Creo 安装时可选组件）。
4. 工作站能否装 Python 3.12 / Java，是否有网络隔离（影响 `uv` 安装与 MCP 远程模式）。
5. 现有标准件库是否已有命名基准；若无，补基准的工作量需要单独排期（这是 §6.3 的前提）。
6. Claude Code / Codex 的使用方式：工程师本机交互为主，还是需要批量/无人值守任务。

---

## 16. 实现状态（2026-11-26）

| 模块 | 状态 | 验证方式 |
|------|------|---------|
| 设计规格 IR（板件/L 形件/轴/钣金/装配/出图） | ✅ | 单元测试 |
| 孔类：螺纹/过孔/沉孔/沉头/销孔/腰型孔，内侧面（`at`） | ✅ | 单元测试 |
| 轴类：键槽（GB/T 1095/1096）、挡圈槽（GB/T 894）、退刀槽、端部倒角、配合公差 | ✅ | 单元测试 + 预览图 |
| 精度：基准、几何公差、表面粗糙度，配合面粗糙度规则 | ✅（SW 端暂以注释输出） | 单元测试 + 预览图 |
| 外购件安装接口（`mount`）与装配接口校验（A003/A004） | ✅ | 单元测试 |
| 选型计算 `cad_calc`（气缸、伺服+丝杠/同步带、螺栓组），计算书随版本归档 | ✅ | 单元测试 |
| 规则检查、出图规划（GB 第一角、优先 1:1 与 A4/A3、坐标尺寸、分布圆、孔标注、轴类断面图、文字重叠 D009、缺视图 D010） | ✅ | 单元测试 + 预览图人工检查 |
| 变更会话、快照回滚、不可覆盖发布、提交门禁、关联重建 | ✅ | 单元测试 |
| MCP 服务（stdio / HTTP，20 个工具，读写注解） | ✅ | stdio 实测 |
| Claude Code 端到端（自然语言 → spec → 出图 → 发布） | ✅（mock） | `claude -p` + Sonnet |
| SolidWorks 编译器（IR → 原生操作计划） | ✅ | 单元测试 |
| SolidWorks COM 执行器（零件/装配/工程图） | ⚠ 已编写，未实机运行；假 COM 对象跑通全流程、核对参数个数 | 待 `scripts/win/probe_sw.py` |
| 缺失标准件模型时的包络块占位 | ✅ | 假 COM 测试 |
| Creo 适配器（打开/参数/再生/质量/导出） | ⚠ 已编写，未实机运行 | 待 `scripts/win/probe_creo.py` |
| Creo 从零建模 | ❌ 依赖 UDF 库或 TOOLKIT（§8.2） | — |
| 精确干涉检查 | mock 为包围盒法；SW 执行器用 InterferenceDetectionManager | 待实机 |
| SW 原生：断面图、轴类尺寸、几何公差框格、草图尺寸参数化 | ⚠ 占位（非致命），待实机实现 | — |
| CI | ✅ GitHub Actions：Windows / Linux / macOS | 每次推送 |
| PDM 集成、节点池、焊接件、铸件 | ❌ 后续阶段 | — |

**下一步**：按 `docs/WINDOWS_TEST.md` 在 SolidWorks 2024 / Creo 10 测试机上运行探针，根据 oplog 修复执行器；
同时用公司真实板件做对照出图，评估"可直接用于加工"的比例。
