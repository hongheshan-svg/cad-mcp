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
`feat.cosmetic_thread` 首期返回 `PRO_TK_NOT_IMPLEMENTED`（元素树需实机核对），孔标注中已写明螺纹规格。
`feat.pattern_points` 预留，不注册。

| cmd | 关键 args |
|---|---|
| `feat.extrude` | `plane`, `entities`, `depth`, `direction`（`normal`/`reverse`/`both`）, `op`（`add`/`cut`）, `through_all`（bool，cut 用） |
| `feat.revolve` | `plane`（含轴线的草绘面）, `entities`（闭合半截面）, `axis`（`{"p1":[u,v],"p2":[u,v]}` 草绘中心线）, `angle`（默认 360）, `op` |
| `feat.hole` | `face`（`top|bottom|front|back|left|right`）, `points`（模型坐标孔口中心列表）, `diameter`, `depth`, `through`, `cbore`（`[直径, 深度]`或 null）, `csink`（直径或 null）, `drill_point`（bool，盲孔 118° 尖）。实现可用旋转切除（推荐，形状完全可控）或 Creo 孔特征 |
| `feat.cosmetic_thread` | `hole_feature`（孔特征名）, `major_diameter`, `length` |
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
| `drw.dim` | `view`, `kind`（`linear|diameter|radius|ordinate`）, `p1`, `p2`（被标注边上的点；diameter/radius 只用 p1）, `at`（尺寸文字位置）, `orient`（`h|v|aligned`）, `text`（覆盖文字，可空；如 `Φ20k6`） |
| `drw.datum` | `view`, `at`（贴边点）, `pos`（方框位置）, `label` |
| `drw.gtol` | `view`, `at`（被测要素点，null 表示不带引线）, `pos`, `type`（flatness/position/... 与 IR 同名）, `value`, `diameter`（bool）, `datums`（≤3） |
| `drw.surface_finish` | `view`, `at`, `pos`, `ra`, `removal`（`required|prohibited`） |
| `drw.table` | `origin`（左下角）, `columns`（`[{"title","width"}]`）, `rows`（字符串二维表）, `row_height`（默认 6）, `header_bottom`（bool：GB 明细表表头在下） |
| `drw.section` | `parent`（画剖切线的视图）, `replaces`（被替代视图名，可空=新建投影剖视）, `label`, `kind`（full/step/half/local）, `cut_points`（模型坐标折线）, `center`（视图位置） → 创建剖截面 `X`（`ProXsecCreate` / 偏移剖截面）并设视图为 2D 剖视 |
| `drw.balloon` | `view`, `component`（组件路径名列表）, `item`, `at`, `pos` |
| `drw.readback` | – → `{"dims": n, "notes": n, "tables": [{"rows": [[...]]}], "views": [...], "symbols": n}` |

## 4. Python 端
- `src/cadmcp/adapters/creo/toolkit.py`：`ToolkitClient(host, port)`：`call(cmd, **args)`，超时、id 递增、ok=false 抛 `ToolkitError(code, msg)`。
- 建模：`creo/compile.py` 的 `compile_part_tk(spec, resolved)` → `NativeOp("tk.<cmd>", args)`；CREOSON 只做打开/参数/保存。
- 出图：`creo/drawing.py` 现有 `ext.cadmcp_*` 粗粒度命令改为逐项 `tk.drw.*`（坐标来自规划层：`Dim.extra["label"]`、`sheet_pts`、`sheet_pos`、`sheet_at`，`hole_table_box` 等）。
- 测试用 `tests/fake_toolkit.py`：本地线程起一个假服务，按本文 schema 校验 args（缺必填/多余键报 BAD_ARGS）。

## 5. 编译与部署（Windows）
见 `creo_toolkit/README.md`：CMake + VS2022，`PROTOOLKIT_DIR` 指向 `<Creo>\Common Files\protoolkit\x86e_win64`；
运行时需 Creo 已启动，`cadmcp_tk.exe --port 9057`；`config.yaml` 中 creo 目标加 `toolkit: {host, port}`。
