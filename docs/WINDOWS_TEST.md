# Windows 实机测试手册（SolidWorks 2024 + Creo 10）

> 目的：验证"AI 写 spec → SolidWorks/Creo 自动建模并出图"在真实软件上能否成立。
> 平台核心（spec、规则、出图规划、会话、MCP）已在 Mac 上用模拟 CAD 跑通；
> **SolidWorks 执行器与 Creo 适配器尚未在实机运行过**，这次测试就是为了找出它们的问题。
> 每一步都会生成日志，失败了不要紧，把日志发回来即可逐条修复。

## 0. 测试机准备

| 项目 | 要求 |
|------|------|
| 系统 | Windows 10/11 64 位 |
| SolidWorks | 2024（已授权）。**本机最好只装这一个 SW 大版本** |
| Creo | 10.0（已授权），安装时勾选 **J-Link / Object TOOLKIT Java**（CREOSON 需要） |
| CREOSON | 从 GitHub SimplifiedLogic/creoson releases 下载与 Creo 10 匹配的版本，解压后运行 `CreosonSetup.exe`，指向 Creo 10 安装目录，点 Start |
| Git | 已安装 |
| 磁盘 | 有 D 盘（或修改配置里的 D:/ 路径） |
| 可选 | Claude Code、Codex CLI（第 5 步用） |

## 1. 安装平台（约 5 分钟）

```powershell
cd D:\
git clone <仓库地址> cad-mcp        # 或把仓库打包拷贝到 D:\cad-mcp
cd D:\cad-mcp
powershell -ExecutionPolicy Bypass -File scripts\win\setup.ps1
```

期望看到：
- `全部通过`（模拟测试，证明平台本身正常）
- `cadmcp doctor` 输出中 `targets` 含 `sw2024`；若 SolidWorks 连接失败会出现在 `unavailable` 中并给出原因
- `creo10` 需要 Creo 和 CREOSON 已启动，否则也会在 `unavailable`（第 4 步再测）

## 2. SolidWorks 探针（最关键，约 5–10 分钟）

1. 关闭所有 SolidWorks 窗口（脚本会自己启动 SolidWorks 2024）。
2. 运行：

```powershell
uv run python scripts\win\probe_sw.py --version 2024 --node-config D:\cadmcp\node.yaml
```

脚本分 4 步，每步打印 ✅/❌：

| 步骤 | 验证什么 | 失败时说明 |
|------|---------|-----------|
| 1 | 用 `SldWorks.Application.32` 连到 SW 2024 并核对版本号 | COM 注册问题 |
| 2 | 从注册表加载 SW 常量类型库 | pywin32 / 类型库问题 |
| 3 | 新建零件，读取基准面名称、模型视图名称（中文版名字不同） | 模板/语言问题 |
| 4 | **端到端**：按 spec 建底板和气缸安装板 → 规则检查 → 自动出工程图并导出 PDF | 看下方说明 |

3. 第 4 步是重点。不管成功失败，都到 `D:\cadmcp\workspaces\c-<时间>\work\` 里查看：
   - `*.oplog.json`：每个 CAD 操作的执行记录（哪一步、耗时、错误）
   - `*.SLDPRT`：打开检查特征树（应有 SK_Base / Base / Fix / Post 等命名特征）、孔位、质量
   - `*.SLDDRW` / `*.PDF`：工程图

4. **人工核对清单**（请拍照或截图）：

| # | 检查项 | 期望 |
|---|-------|------|
| 1 | 底板外形 300×200×15，四角 R5 | 正确 |
| 2 | 4×沉孔（M8 内六角）距边 20；2×M8 螺纹孔孔距 160 | 孔位正确、沉孔方向朝上表面 |
| 3 | 质量：SW「评估-质量属性」 vs 报告中的估算 6.996 kg | 偏差 < 3%（材料映射正确时） |
| 4 | 工程图视图方向 | 板面视图（俯视）显示 300×200 板面；主视图显示厚度 |
| 5 | 自动坐标尺寸 | 孔位从左下角起标注，数值与模型一致 |
| 6 | 孔标注注释 | 有"4×Φ9 通 ⌴Φ14↧8.6""2×M8-6H 通"等文字 |
| 7 | 技术要求 / 标题栏 | 文字齐全；标题栏是否显示图号、材料（取决于模板是否链接属性） |

> 标准件/外购件（型材、气缸、螺钉）若 `library_root` 下没有对应的 `.SLDPRT`，平台会自动生成**包络块占位零件**并给出 A002 警告，
> 装配仍能建出来；把带 CS_ 基准的真实模型放入 `library_root` 后重建即可替换。

> 已知会降级为"警告"的步骤（不影响主体几何）：草图尺寸参数化链接、装饰螺纹线、倒角、命名基准面/轴、非坐标视图的总体尺寸。
> 这些需要根据本次实测结果补齐。

## 3. 用公司真实零件做对照（强烈建议）

挑 1–2 块**已经出过图的真实板件**（10 个孔以内），让平台重画一遍，与工程师原图对比：

```powershell
# 在 Claude Code 中（第 5 步配置好后）直接描述零件，或把原图尺寸告诉它
```

记录：尺寸是否全对、图面是否可直接用于加工、工程师还需要改哪些地方、改图用了多少分钟。
**这一步的结论决定"替代画图"能否成立。**

### 3b. 修改一个现有零件（约 10 分钟）

复制一个**不重要的**旧零件及其工程图到 `D:\designs\test\`（确保在 policy 的 `read_allow` 中），在 Claude Code 中说：
"打开 D:\designs\test 里的 XX 零件，把板厚改成 12，更新图纸"。平台会：
`cad_change_begin(sources=[目录])` → `cad_inspect_existing` → `cad_edit_existing` → `cad_change_commit`。

记录：inspect 读到的尺寸名是否完整（中文版特征名如"凸台-拉伸1"）、改后质量是否变化、工程图是否自动更新、
有无悬空尺寸。需要回写时先在 policy 加 `writeback_allow`，再 `cad_change_writeback(confirm=true)`；
**原文件会先备份到发布目录 `_writeback_backup`**。
再试一次"只复制单个零件"：另一个文件夹里放一个引用该零件的装配，确认 inspect 的 `used_by_outside` 能找到它、
修改后它被更新，回写后在 SW 中打开原位置的装配，**检查它引用的是原位置的新零件而不是工作区路径**
（`ReplaceReferencedDocument` 重定向是否生效）。
共用件被很多文件引用时（上限默认 10000），每个引用文件都要在 SW 中打开、重建、保存一次：
请记录单个文件的刷新耗时（见 `*.refresh.oplog.json` 中的 ms），用于估算大批量修改的时间。发回 `*.inspect.oplog.json`、`*.edit.oplog.json`、`*.refresh.oplog.json`。

## 4. Creo 探针（约 5 分钟）

1. 启动 Creo 10，启动 CREOSON（CreosonSetup → Start，默认端口 9056）。
2. 准备一个真实零件文件（脚本会复制到临时目录，不改原文件）：

```powershell
uv run python scripts\win\probe_creo.py --version 10.0 --model D:\designs\sample\plate.prt
```

验证：连接、打开、参数读写、尺寸列表、再生、质量属性、保存、导出 STEP。报告：`probe_report_creo.json`。

> Creo 从零建模依赖"起始件 + UDF 库"或 TOOLKIT 授权（见 DESIGN.md §8.2）。本次只验证 CREOSON 基础能力。
> 请同时确认：**公司是否有 Creo TOOLKIT（Object TOOLKIT C++）授权**。

## 5. 接入 Claude Code / Codex

**Claude Code**：在 `D:\cad-mcp` 目录启动 `claude`，项目里的 `.mcp.json` 会自动提示启用 `cad` 服务器。需要先设置环境变量：

```powershell
setx CADMCP_NODE_CONFIG D:\cadmcp\node.yaml
```

**Codex**：把 `config\codex.example.toml` 的内容追加到 `%USERPROFILE%\.codex\config.toml`，然后 `codex mcp list` 确认。

测试提示词（两个客户端各跑一次）：

```
用 cad MCP，目标选 sw2024。设计一块电机安装板，材料 AL6061-T6，外形 250×150，厚 12，四角 R5。
中心 Φ50H7 止口通孔；止口周围 4×M5 螺纹孔（深10，螺纹深8）均布在 Φ70 圆上，起始角45°；
四角 4×M6 内六角沉孔，孔中心距边 15。图号 MP-001，名称 电机安装板，表面处理 阳极氧化黑，设计人 张工。
完成后检查规则、出图，并提交版本 A。
```

（该提示词已在 Mac 模拟环境由 Claude Code 一次跑通。）

## 6. 需要发回的文件

1. `probe_report_sw.json`、`probe_report_creo.json`
2. `D:\cadmcp\workspaces\c-*\work\` 下的 `*.oplog.json`、`*.PDF`、第 2 步中的截图
3. 第 3 步的对照记录、3b 的修改记录与 oplog
4. SolidWorks 语言版本（中文/英文）、SP 号；Creo 维护版本号（如 10.0.2.0）
5. 公司 GB 图框模板文件（`.drwdot`）、材料库名称——用于下一轮把图纸做成公司标准样式

### 共用件按目录区分（policy.dependents_rules）
在 policy 中把一个测试项目目录设为 `frozen`、另一个设为 `report`，修改它们共同引用的零件：
应返回 E017 拒绝；改用 `save_as` 后原件和两个项目文件均不变（核对文件修改时间）。

### 装配图原生明细表与序号（D011 / D012）
出气缸工位装配图（`examples/cylinder_station.py`），核对：
1. 明细表是 SW 原生 BOM 表，表头在下、序号自下而上；“代号/名称/材料”列来自零件属性 PartNo/Description/Material。
2. 明细表行顺序与平台方案一致（`*_DRW.oplog.json` 中 bom_table 的 rows）；如顺序不对，记录 MoveRow 是否生效、
   SW 是否自动重编序号，以及 `swTableHeader_Bottom` / `swTableItemInsertPosition_Before` 常量名是否存在。
3. 序号为 BOM 序号（双击可见链接到明细表），位置在主视图四周、按顺时针编号；oplog 中 bom_balloons 的
   placed / missing（missing 多说明 `RootDrawingComponent.GetChildren` 或组件匹配有问题，此时会退回自动序号）。
4. 出图结果 rules 中没有 D011/D012；有的话把 oplog 和 PDF 一起发回。

### 焊接件：原生焊缝符号与切割清单（D013 / D014）
建 `examples/weld_base.py` 并出图，核对：
1. 零件特征树“切割清单”下每个文件夹有属性 Description / LENGTH / MATERIAL（oplog 中 cut_list_properties 的 folders 数；
   若报 rows matched 不足，记录切割清单文件夹的类型名与实体名）。
2. 工程图焊缝符号是 SW 原生焊接符号：周围焊有小圆、双面焊两侧都有三角形、尾部 135。
   oplog 中 weld_symbol 返回 native=false 时记录 fallback 原因。常见原因是符号代码不对：在 SW 里手工插一个角焊缝符号，
   用宏读 `WeldSymbol.GetText(True, …)` 得到代码，写到节点配置 `templates.weld_fillet_symbol`。
3. 切割清单表表头在下，长度/数量与预览一致；rules 中没有 D013/D014。

### 剖视图（D015 / D016）
建 `examples/motor_plate.py` 并出图（默认 sections=auto → 主视图阶梯剖 A-A），核对：
1. 俯视图上的剖切折线位置与预览 SVG 一致（oplog 中 section_cut 的 points 为图纸坐标 mm）。
   如果线画到了别处，记录：激活视图后 SketchManager.CreateLine 用的是图纸坐标还是视图坐标。
2. A-A 剖视图位于原主视图位置、投射方向正确（从前向后看）；方向反了记录下来（需加 ChangeDirection 选项）。
3. 阶梯剖转折处没有分界线；沉孔、螺纹孔、盲孔、型腔都剖到；剖面线为 45°。
4. 再试 `sections: [{"kind": "half", "through": ["Pilot"]}]`：SW 能否用 L 形线生成半剖；不行记录报错。

### 局部剖、孔表、引出标注位置（D016 / D017）
1. 局部剖：tests/test_section.py 里的 BAR（压条，单个 M8 螺纹孔）出图，主视图应在螺纹孔处局部剖开。
   核对 oplog 中 broken_out_section：轮廓是否画在视图里（图纸坐标），CreateBreakOutSection 的深度（m）是否剖到孔中心；
   如果 SW 要求轮廓为样条，记录报错（届时改用 CreateSpline2）。
2. 孔表：tests/test_hole_table.py 的 GRID（21 个孔）出图，俯视图应只有孔号（A1…C4）和 X0 Y0 原点，
   右侧技术要求上方有 4 列孔表。核对：表格是否生成、单元格文字是否写入（`_set_cell` 用 PROPERTYPUT 写 Text(row, col)），
   回读不一致会报 D017。表格位置/列宽不对请截图。
3. 引出标注：孔标注、4×R 圆角、型腔标注的文字位置应与预览 SVG 一致（leader_note 的 text_pos）；
   引线目前没有关联到模型边线（P1），记录 SW 中引线的实际样子。

### 精度符号原生化（D019）
用 tests/test_core.py 的 PLATE_PRECISION（基准 A/B、平面度、垂直度、位置度、粗糙度）和 SHAFT 出图，核对：
1. oplog 中 probe_gdt_constants 的结果：把列出的 swGcs*/swMc*/swSF* 常量名和数值整段发回。
   执行器按候选名查找（见 executor.GCS_NAMES）；如果名字不在候选里，对应公差会退回注释并报 D019。
2. 基准：三角形是否贴在边线上、方框字母是否为 A/B（InsertDatumTag2 + SetLabel）。
   如果没贴上边线（选边 SelectByID2("", "EDGE", x, y) 落空），记录 datum_tag 返回的 attached。
3. 框格：符号是否正确（平面度 ⏥、位置度 ⌖ 等）、位置度前是否有 Φ、基准字母顺序；
   孔的位置度框格应在孔标注文字下方。
4. 表面结构符号：是否为"去除材料"√，值显示为 Ra 3.2；非顶面的符号是否带引线。

## 装饰螺纹 / 命名参考 / 边选择（sw-cosmetic-refs）

以下 API 用法在假 COM 上验证了调用流程与实参个数，未经实机验证，需逐项核对（失败均为 NON_CRITICAL，不影响几何）：

1. **装饰螺纹**：做一个带 `tapped` 孔的零件（`holes_via=cut`），看 oplog 中 `cosmetic_thread` 是否 ok，`readback["cosmetic_threads"]` 应等于孔数；特征树出现 `<孔名>_CT1…`；工程图中螺纹线自动显示。
   - 假设：先 `Extension.SelectByID2("", "EDGE", x, y, z, ...)` 在孔口圆边（孔面内偏移钻孔半径处）选边，再 `FeatureManager.InsertCosmeticThread3(Type, Diameter, Depth, EndCondition, Standard, FastenerType, Size, Class)`（8 个实参，**参数个数/顺序待对照 API 帮助**）。若不符，改 `executor.op_cosmetic_thread` 和 `tests/fake_sw.py` ARITY。
   - 常量 `swCosmeticThreadBlind/ThroughAll/Internal/External` 为猜测名，取不到时用默认 0/1/0/1，需核对。
   - 轴类外螺纹：IR 目前没有螺纹字段，`op_cosmetic_thread(external=True)` 已支持但 compile 未生成。
2. **边选择**：含 `fillet`/`chamfer`（`outer_vertical`/`top_outer`/`bottom_outer`）的 extrude 零件。假设 `GetBodies2(swSolidBody, False)`、`Body2.GetBodyBox()`（xmin,ymin,zmin,xmax,ymax,zmax）、`Face2.Normal`/`GetBox()`/`GetLoops()`、`Loop2.IsOuter()/GetEdges()`、`Edge.GetStartVertex().GetPoint()`、`Edge.Select4(Append, Data)` 均可用；基体沿 +Z 拉伸（顶面 = Z 最大平面）。核对 `readback["selected_edges"]` 的边数（矩形板 outer_vertical=4）。
3. **圆角/倒角**（原为占位，现依赖上面的选边）：假设 `FeatureFillet3(195, R, R, 0×9)`（12 个实参，待核对）和 `InsertFeatureChamfer(4, swChamferEqualDistance, d, 0, d, 0, 0, 0)`（8 个实参，待核对）；核对圆角半径/倒角值与 spec 一致。
4. **命名基准**：`MP_*`（面）→ 默认基准面偏移 `InsertRefPlane(Distance=8, 偏移, 0,0,0,0)`，偏移为 0 时用重合约束 2；负偏移（如底面 Z<0）假设传负距离即反向，需核对位置。`AX_*` → 先对孔壁圆柱面 `SelectByRay` 后 `InsertAxis2(True)`，失败退回两个辅助偏移面求交（辅助面命名 `<名>_aux1/2`）；`PT_*` → 三个辅助偏移面 + `InsertReferencePoint(swRefPointIntersection(默认4), 0, 0, 1)`，失败退回 3D 草图点。核对特征树里名称与 spec 一致，且装配里能被配合引用。
