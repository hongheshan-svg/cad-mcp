# Creo TOOLKIT 服务（cadmcp_tk）设计与协议

> 状态：首期面向 Creo 10.0（Visual Studio 2022，x86e_win64）。源码在 `creo_toolkit/`，需在装有 Creo + TOOLKIT 的 Windows 上编译。

## 1. 为什么、怎么接

CREOSON（J-Link）能打开模型、读写参数、建视图、导出，但不能从零建特征、标尺寸、写注释、建表格。
公司有 Creo TOOLKIT 授权，因此：

| 能力 | 走哪条路 |
|---|---|
| 打开/参数/再生/保存/导出、一般视图与投影视图、BOM 路径、改尺寸（修改现有模型） | CREOSON（已实现，保留） |
| 从零建模（拉伸、旋转、孔、型腔、凸台、圆角倒角、基准、装饰螺纹） | **cadmcp_tk**（本文） |
| 出图细节（尺寸、注释、表格、基准/公差/粗糙度符号、剖视、球标） | **cadmcp_tk**（本文） |

`cadmcp_tk.exe` 是 TOOLKIT **异步模式**程序：启动后 `ProEngineerConnect` 连到已运行的 Creo 会话（同一台机器），
在 `127.0.0.1:<port>`（默认 9057）监听 TCP，单线程逐条处理请求并直接调用 TOOLKIT。不用 DLL 插件模式，
原因：DLL 模式下 TOOLKIT 只能在 Creo 主线程调用，要收网络请求得绕线程；异步程序崩溃不拖垮 Creo，重启调试方便。

## 2. 传输协议

- TCP，UTF-8，**每行一个 JSON 对象**（`\n` 结尾，JSON 内不含裸换行）。
- 请求：`{"id": 17, "cmd": "drw.note", "args": {...}}`
- 应答：`{"id": 17, "ok": true, "result": {...}}` 或
  `{"id": 17, "ok": false, "error": "说明", "code": "PRO_TK_E_NOT_FOUND"}`（code 为 ProError 名或 `BAD_ARGS` / `UNKNOWN_CMD` / `EXCEPTION`）。
- 单位：**长度 mm，角度 deg**（模型用 mmNs 单位制；C++ 端负责与模型/图纸单位换算）。
- 图纸坐标：**图纸左下角为原点，mm**（与规划层 `DrawingPlan` 一致；C++ 端换算成图纸单位）。
- 模型坐标：零件默认坐标系（`PRT_CSYS_DEF`），mm。
- 名称：字符串 UTF-8，C++ 端转 `wchar_t`（`ProName` 最长 31 字符，超长报 BAD_ARGS）。
- 服务端出错不得崩溃：每条命令包在 try/catch 中，TOOLKIT 返回非 `PRO_TK_NO_ERROR` 即应答 ok=false。

## 3. 命令

### 3.1 会话（cmd_session.cpp）
| cmd | args | result |
|---|---|---|
| `ping` | – | `{"creo": "10.0.x", "toolkit": "..." }` |
| `model.open` | `file`（完整路径） | `{"name", "type": "part|assembly|drawing"}` |
| `model.new_part` | `name`, `start_part`（可空：空则 `ProSolidCreate` 并设 mmNs 单位） | `{"name"}` |
| `model.regenerate` | `name` | `{"status": "ok|errors", "failed": [特征名]}` |
| `model.save` | `name`, `dir`（可空） | `{"path"}` |
| `model.feature_status` | `name` | `{"features": [{"name","id","type","status"}]}` |
| `model.param_set` | `name`, `param`, `value`, `type`（string/double/int/bool） | – |

### 3.2 建模（cmd_model.cpp）
所有特征命令都带 `model`（模型名）与 `feature`（新特征名，创建后 `ProModelitemNameSet`），返回 `{"id": 特征 id}`。
草图截面统一用 **2D 实体列表**，坐标为草绘平面内 (u, v)，mm：
`{"type":"line","p1":[u,v],"p2":[u,v]}`、`{"type":"arc","center":[u,v],"r":R,"a0":deg,"a1":deg}`、`{"type":"circle","center":[u,v],"r":R}`。
草绘平面 `plane`：`{"ref": "TOP|FRONT|RIGHT|<基准面名>", "offset": mm}`（offset≠0 时先建偏移基准面，名为 `<feature>_PLN`）。
平面 → 模型坐标约定：TOP：u=+X，v=+Y，法向 +Z；FRONT：u=+X，v=+Z，法向 −Y；RIGHT：u=+Y，v=+Z，法向 +X。

**IR 坐标 → Creo 坐标**：协议中的模型坐标一律是平台 IR 坐标（Z 向上）。Creo 默认 Y 向上，C++ 端在边界做刚体旋转
`IR (x, y, z) → Creo (x, z, −y)`。于是 IR 的 TOP/FRONT/RIGHT 正好是 Creo 默认基准面 TOP/FRONT/RIGHT，
CREOSON 按命名视图 FRONT/TOP/RIGHT 建的工程图视图与规划层一致。偏移基准面沿参照面正法向偏移（IR TOP→+Z，FRONT→−Y，RIGHT→+X）。
草绘 (u, v) → IR 三维点 → Creo 三维点 → 用 `ProSectionLocationGet` 的逆矩阵得截面二维坐标，不依赖草绘器 H/V 约定。

**实现说明**：`feat.hole` 用拉伸切除实现——同一组孔的所有孔口圆放在一个草图里，沉孔一级、钻孔一级；沉头孔用 D×D 倒角
（D = (csink − d)/2）；`drill_point` 首期不建钻尖（返回 `"drill_point": "omitted"`，剖视由规划层画出）。
`feat.cosmetic_thread` 用 `PRO_FEAT_THREAD` 元素树（ProThread.h）：孔特征中每个孔（圆柱面按轴线去重）建一个简单螺纹，起始面取孔拉伸特征的草绘平面，盲深 `length`；结果 `{"id": 第一个, "features": [全部]}`，其余命名 `<feature>_2`…
`feat.pattern_points` 预留，不注册。

| cmd | 关键 args |
|---|---|
| `feat.extrude` | `plane`, `entities`, `depth`, `direction`（`normal`/`reverse`/`both`）, `op`（`add`/`cut`）, `through_all`（bool，cut 用） |
| `feat.revolve` | `plane`（含轴线的草绘面）, `entities`（闭合半截面）, `axis`（`{"p1":[u,v],"p2":[u,v]}` 草绘中心线）, `angle`（默认 360）, `op` |
| `feat.hole` | `face`（`top|bottom|front|back|left|right`）, `points`（模型坐标孔口中心列表）, `diameter`, `depth`, `through`, `cbore`（`[直径, 深度]`或 null）, `csink`（直径或 null）, `drill_point`（bool，盲孔 118° 尖）。实现可用旋转切除（推荐，形状完全可控）或 Creo 孔特征 |
| `feat.cosmetic_thread` | `hole_feature`（孔特征名）, `major_diameter`, `length`；轴外螺纹改用 `edge_point`（螺纹圆柱面端部圆边上一点）+ `start_edge_point`（起始端面上一条边上的点），按边的邻面取圆柱面与起始平面 |
| `feat.round` | `edges`（模型坐标的边中点列表）, `radius` |
| `feat.chamfer` | `edges`, `distance` |
| `datum.plane` | `ref`（`TOP|FRONT|RIGHT|<面名>`）, `offset` |
| `datum.axis` | `p1`, `p2`（模型坐标） |
| `datum.point` | `at`（模型坐标） |
| `datum.csys` | `origin`, `x_dir`, `y_dir`（模型坐标） |
| `feat.pattern_points` | 预留（首期孔用 `points` 多点一次建） |

### 3.3 出图（cmd_drawing.cpp）
视图由 CREOSON 建（视图名与 `creo/drawing.py` 的 `view_name()` 一致，如 `V_FRONT`），本服务只做细节。
所有命令带 `drawing`（图纸名）。带 `view` 的命令可按视图名找 `ProView`；需要选中模型边线的命令给 **图纸坐标点**，
C++ 端在该视图内找投影距离最近的模型边（容差 1.5 mm 图纸），找不到应答 `PRO_TK_E_NOT_FOUND`。

| cmd | 关键 args |
|---|---|
| `drw.note` | `text`（可多行）, `at`（文字左下角）, `height`（mm，默认 3.5）, `leader_to`（图纸点或 null：有则引线指向该点处的模型边） , `view`（leader 用） |
| `drw.dim` | `view`, `kind`（`linear|diameter|radius|ordinate`）, `p1`, `p2`（被标注边上的点；diameter/radius 只用 p1；ordinate 的 p1 为基准轮廓边、p2 为要素边，孔量到圆心）, `at`（尺寸文字位置）, `orient`（`h|v|aligned`；ordinate 须 h/v）, `text`（覆盖文字，可空；如 `Φ20k6`）, `tol`（可选 `[上偏差, 下偏差]` mm，原生正负公差叠写显示；应答带 tol_set 布尔） → ordinate：同视图同方向同基准边共用一条基线（首个转 `ProDrawingOrdbaselineCreate`，其余 `ProDrawingDimToOrdinate`）；p2 与基准同一条边时只返回基线 |
| `drw.datum` | `view`, `at`（贴边点）, `pos`（方框位置）, `label` |
| `drw.gtol` | `view`, `at`（被测要素点，null 表示不带引线）, `pos`, `type`（flatness/position/... 与 IR 同名）, `value`, `diameter`（bool）, `datums`（≤3） |
| `drw.surface_finish` | `view`, `at`, `pos`, `ra`, `removal`（`required|prohibited`） |
| `drw.table` | `origin`（左下角）, `columns`（`[{"title","width"}]`）, `rows`（字符串二维表）, `row_height`（默认 6）, `header_bottom`（bool：GB 明细表表头在下） |
| `drw.section` | `parent`（画剖切线的视图，显示剖切箭头；local 可空）, `replaces`（被设为剖视的已有视图）, `label`, `kind`（full/step/half/local）, `cut_points`（模型坐标折线，在零件顶面上；full/half/local 取前两点）, `center`（视图位置，仅校验）, `boundary`（local 必填：局部区域闭合折线，图纸 mm） → `ProXSectionOffsetCreate` 建剖截面（一段=平面剖，多段=阶梯剖）后 `ProDrawingView2DSectionSet`；half 的分界面为垂直剖切线的默认基准面 |
| `drw.removed_section` | `view`（CREOSON 已建的沿轴线观察视图，如 V_SEC_A）, `label`（剖截面名，如 A）, `z`（剖切位置：IR Z 坐标=轴向，mm）, `parent`（画剖切箭头的视图，可空）, `half_span`（剖切线半长，默认 500） → 偏移剖截面（平面 IR z=const）+ 2D 剖视 + 只显示切面（`ProDrawingView2DSectionAreaSet`） |
| `drw.detail_view` | `parent`（父视图）, `view`（新视图别名，如 V_DET_I；后续 drw.dim 用它）, `circle`（`[cx, cy, r]` 图纸 mm：放大区域）, `center`（放大图位置，图纸 mm）, `scale`（放大比例，如 5） → 圆内最近模型边作参考点 + 圆形样条边界 → `ProDrawingViewDetailCreate` → `ProDrawingViewScaleSet`；返回 `{"view", "scale_set"}` |
| `drw.balloon` | `view`, `component`（组件路径名列表）, `item`, `at`, `pos` |
| `drw.show_axes` | `view` → 遍历零件基准轴（`ProSolidAxisVisit`），逐个 `ProAnnotationShow` 显示在该视图中（孔/圆柱中心线）；返回 `{"axes", "shown", "failed"}`，失败不中断 |
| `drw.readback` | – → `{"dims": n, "notes": n, "tables": [{"rows": [[...]]}], "views": [...], "symbols": n}` |

### 3.4 装配（cmd_assembly.cpp）
所有装配命令带 `asm`（装配名）。打开/参数/再生/保存仍用 §3.1 的 `model.*`（`name` = 装配名）。
位姿 `transform` 为 **IR 坐标** 的 4×4 齐次矩阵（列向量约定：`p_asm = T · p_comp`，平移在第 4 列，mm），
C++ 端换算为 Creo 坐标：`T_creo = Q · T · Qᵀ`，Q 为 IR→Creo 的旋转 `(x, y, z) → (x, z, −y)`，再转成 `ProMatrix`（行向量约定）。

| cmd | 关键 args |
|---|---|
| `asm.new` | `name`, `start_asm`（可空：空则 `ProSolidCreate(PRO_ASSEMBLY)` 并设 mmNs） → `{"name"}` |
| `asm.add_component` | `file`（零件/子装配完整路径，未在会话中则先打开）, `name`（实例 id，服务端记住实例 id 与元件特征 id 的对应，供后续 `target` 引用）, `transform`（4×4，IR）, `csys`（可空：`{"ref": 元件坐标系名, "target": 目标实例 id 或 null=装配本身, "target_ref": 目标坐标系名}`，有则建坐标系重合约束，约束失败或为空时按 `transform` 固定放置） → `{"id": 元件特征 id, "placement": "csys|fixed"}` |
| `asm.interference` | – → `{"pairs": [{"components": [实例 id, 实例 id], "volume_mm3": v}]}`（全局干涉） |

## 4. Python 端
- `src/cadmcp/adapters/creo/toolkit.py`：`ToolkitClient(host, port)`：`call(cmd, **args)`，超时、id 递增、ok=false 抛 `ToolkitError(code, msg)`。
- 装配：`creo/compile.py` 的 `compile_assembly_tk(spec, asm, files, library_root)` → `tk.asm.*` + `tk.model.*`。
- 建模：`creo/compile.py` 的 `compile_part_tk(spec, resolved)` → `NativeOp("tk.<cmd>", args)`；CREOSON 只做打开/参数/保存。
- 出图：`creo/drawing.py` 现有 `ext.cadmcp_*` 粗粒度命令改为逐项 `tk.drw.*`（坐标来自规划层：`Dim.extra["label"]`、`sheet_pts`、`sheet_pos`、`sheet_at`，`hole_table_box` 等）。
- 测试用 `tests/fake_toolkit.py`：本地线程起一个假服务，按本文 schema 校验 args（缺必填/多余键报 BAD_ARGS）。

## 5. 编译与部署（Windows）
见 `creo_toolkit/README.md`：CMake + VS2022，`PROTOOLKIT_DIR` 指向 `<Creo>\Common Files\protoolkit\x86e_win64`；
运行时需 Creo 已启动，`cadmcp_tk.exe --port 9057`；`config.yaml` 中 creo 目标加 `toolkit: {host, port}`。
