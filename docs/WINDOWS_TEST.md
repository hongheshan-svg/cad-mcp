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

### 原生修订表 + 引出标注引线附着（D020）
给图纸 spec 加 `revisions`（2~3 条）出图，核对：
1. 修订表：oplog 中 `revision_table` 应成功（无 `revision_fallback`）；表在标题栏左侧、底边与标题栏对齐，
   最新修订在上、最多 6 行，列为 版本/更改内容/日期/签名，与预览 SVG 一致。
   API 假设：`IDrawingDoc.InsertRevisionTable2(UseAnchorPoint, X, Y, AnchorType, TableTemplate)`（5 参，模板传空串用默认）
   返回 IRevisionTableAnnotation；`AddRevision(Revision)`（1 参）逐行加；单元格通过 ITableAnnotation 的 Text(row, col)
   PROPERTYPUT 写入（行 0 为表头，数据行从 1 起；SW 可能把新行加在表尾而非表头下，行序反了请记录）。
   锚点常量复用 swBOMConfigurationAnchor_BottomLeft（默认 3）；表长出图框请记录。
   失败时退回注释"更改记录"；回读行数不等于方案时报 D020（error）。
2. 引出标注引线：孔标注/圆角/型腔标注应带指向边线的引线，oplog 中 leader_note 返回 `attached`，
   `leaders_attached` 为附着成功数。假设：视图激活后 `SelectByID2("", "EDGE", ax, ay, 0, False, 0, None, 0)`
   用图纸坐标（anchor，m）能选中边线，选中后 InsertNote 自动带引线并附着。若 attached 总是 False，
   说明 EDGE 选择需要视图内坐标或改用 SelectByRay；若文字位置被 SW 吸附偏移，记录偏差。

## 原生轴类尺寸 / 非坐标视图总体尺寸（op_shaft_dimension / op_overall_dimension）

规划层（`detailing.shaft_dim_sheet` / `overall_dim_sheet`）给每个尺寸写 `Dim.extra`：`sheet_pts`（两个被标注点，图纸 mm）、
`sheet_pos`（尺寸线/文字放置点，图纸 mm）、`orient`（h/v）。执行器激活视图 → 在 `sheet_pts` 处两次 `Extension.SelectByID2("", "EDGE", x, y, 0, append, 0, None, 0)`
→ `AddDimension2(x, y, 0)` 放到 `sheet_pos`。失败抛 OpFailed（NON_CRITICAL），成功的 ref 记入 `readback["dim_refs"]`
（不转给 rendered，避免 D005 改按 ref 全量核对；尺寸数量仍由 `dim_count` 回读核对）。

实机核对：
1. 出一张 SHAFT 图，看主视图：各轴段长度链、总长、键槽长/定位、各段直径是否都已标注，位置与 SVG 预览一致；查看 oplog 中 shaft_dimension / overall_dimension 的失败原因。
2. 线性尺寸选边点：轴肩/端面取偏离轴线 0.85r 处的竖直线；键槽端部取轴线高度的圆弧端点。若选不中，检查视图缩放/旋转后图纸坐标是否仍与规划一致（`SelectByID2` 的 x、y 是图纸坐标，单位 m）。
3. API 假设（需确认）：
   - 两条平行边线选中后 `AddDimension2(x, y, 0)` 直接生成两线间距离尺寸（水平/竖直由两线方向决定），返回 IDisplayDimension；不行就改 AddHorizontalDimension2 / AddVerticalDimension2（同为 3 参）。
   - 直径尺寸是"上下两条轮廓线间距"，用 `IDisplayDimension.SetText(swDimensionTextAll, "<MOD-DIAM><DIM>k6")` 覆盖为 Φ+值+公差带（`swDimensionTextAll` 缺失时默认 0；`<MOD-DIAM>`/`<DIM>` 标记需确认）。
     公差带只是文字，未写入 IDimensionTolerance（拟合公差类型枚举写不准）；失败记入 `readback["dim_text_failed"]`，尺寸仍保留。
   - 轴类视图的边线是否能被 `SelectByID2(..., "EDGE", ...)` 按图纸坐标选中，取决于该视图当前精度；选不中时 oplog 会写明坐标。
4. 非坐标视图的总体尺寸（零件/装配的各视图）取左右（或上下）轮廓线的中点；L 形等非矩形轮廓可能选不中，属预期的软失败。

## 轴类移出断面图 / 断面图与展开图尺寸（op_section_view）

规划层新增（CAD 无关，坐标与 SVG 预览一致）：`_shaft_section_dim_sheet`（断面图槽宽、d−t；`extra["section"]` = 断面标签，
`extra["arc_end"] = ("max", None)` 表示第 1 点在圆周上、应量到圆的远端）、`flat_dim_sheet`（展开图总宽/总高，选边点避开折弯线；
折弯说明 `sheet_pos`），以及轴类倒角/挡圈槽引出标注的 `extra["label"]`。

执行器：
- `op_section_view(label, parent, z, center, points)`：主视图上沿 `points` 画竖直剖切线（上下伸出外形 5 mm）→ `Select4` →
  `CreateSectionViewAt5(x, y, 0, "A", opts, None, 0)` → `RemoveAlignment()` → `Position = 规划位置`；登记为 `views["section:A-A"]`。
- 断面图尺寸走 `op_shaft_dimension`（view = `section:A-A`）：槽宽文字覆盖 `<DIM>N9`；d−t 用 `SetArcEndCondition(1, Max)`。
- 展开图总宽/总高走 `op_overall_dimension`（view = `flat_pattern`）；折弯说明 `op_leader_note` 无引线、放到 `text_pos`。

实机核对（TODO(实机核对)）：
1. `swCreateSectionViewAtOptions_e` 中"只显示切面"的成员名：执行器按 `swCreateSectionView_DisplayOnlySurfaceCut` /
   `_DisplaySurfaceCut` / `_PartialSection` 依次查找，查不到用 0（会生成完整剖视而不是断面），oplog 的 `surface_cut_option` 记录实际值。
2. `IView.RemoveAlignment()` 是否存在、设置 `IView.Position` 能否把断面图移到主视图正下方（`moved` 为 false 时断面图仍在默认位置）。
3. 断面图朝向：规划与 SVG 假定键槽朝上（沿轴线从左向右看）。SW 实际断面方向取决于剖切线画线方向与键槽角度；
   若键槽不在上方，尺寸选边点会落空——请截图回传，按实际方向在 compile 中翻转剖切线或给断面加旋转。
4. `IDisplayDimension.SetArcEndCondition(Index, Condition)` 与 `swArcEndCondition_e`（缺省按 Center=1/Min=2/Max=3）；失败记入 `readback["dim_arc_failed"]`。
5. 展开图视图的 u/v 方向是否与规划 `flat_box` 一致（SW 展开图可能旋转 90°）；不一致时总宽/总高选边会失败（软失败）。

## 装饰螺纹 / 命名参考 / 边选择（sw-cosmetic-refs）

以下 API 用法在假 COM 上验证了调用流程与实参个数，未经实机验证，需逐项核对（失败均为 NON_CRITICAL，不影响几何）：

1. **装饰螺纹**：做一个带 `tapped` 孔的零件（`holes_via=cut`），看 oplog 中 `cosmetic_thread` 是否 ok，`readback["cosmetic_threads"]` 应等于孔数；特征树出现 `<孔名>_CT1…`；工程图中螺纹线自动显示。
   - 假设：先 `Extension.SelectByID2("", "EDGE", x, y, z, ...)` 在孔口圆边（孔面内偏移钻孔半径处）选边，再 `FeatureManager.InsertCosmeticThread3(Type, Diameter, Depth, EndCondition, Standard, FastenerType, Size, Class)`（8 个实参，**参数个数/顺序待对照 API 帮助**）。若不符，改 `executor.op_cosmetic_thread` 和 `tests/fake_sw.py` ARITY。
   - 常量 `swCosmeticThreadBlind/ThroughAll/Internal/External` 为猜测名，取不到时用默认 0/1/0/1，需核对。
   - 轴类外螺纹：IR 目前没有螺纹字段，`op_cosmetic_thread(external=True)` 已支持但 compile 未生成。
2. **边选择**：含 `fillet`/`chamfer`（`outer_vertical`/`top_outer`/`bottom_outer`）的 extrude 零件。假设 `GetBodies2(swSolidBody, False)`、`Body2.GetBodyBox()`（xmin,ymin,zmin,xmax,ymax,zmax）、`Face2.Normal`/`GetBox()`/`GetLoops()`、`Loop2.IsOuter()/GetEdges()`、`Edge.GetStartVertex().GetPoint()`、`Edge.Select4(Append, Data)` 均可用；基体沿 +Z 拉伸（顶面 = Z 最大平面）。核对 `readback["selected_edges"]` 的边数（矩形板 outer_vertical=4）。
3. **圆角/倒角**（原为占位，现依赖上面的选边）：假设 `FeatureFillet3(195, R, R, 0×9)`（12 个实参，待核对）和 `InsertFeatureChamfer(4, swChamferEqualDistance, d, 0, d, 0, 0, 0)`（8 个实参，待核对）；核对圆角半径/倒角值与 spec 一致。
4. **命名基准**：`MP_*`（面）→ 默认基准面偏移 `InsertRefPlane(Distance=8, 偏移, 0,0,0,0)`，偏移为 0 时用重合约束 2；负偏移（如底面 Z<0）假设传负距离即反向，需核对位置。`AX_*` → 先对孔壁圆柱面 `SelectByRay` 后 `InsertAxis2(True)`，失败退回两个辅助偏移面求交（辅助面命名 `<名>_aux1/2`）；`PT_*` → 三个辅助偏移面 + `InsertReferencePoint(swRefPointIntersection(默认4), 0, 0, 1)`，失败退回 3D 草图点。核对特征树里名称与 spec 一致，且装配里能被配合引用。

## Creo 修改现有模型（edit.py：inspect / apply_edits / references / refresh）

代码在 `src/cadmcp/adapters/creo/edit.py`，测试在 `tests/test_creo_edit.py`（假 creopyson，函数与参数名已对照 creopyson 0.7.8 源码）。
真实 Creo 行为未验证，请在 Creo 10 + CREOSON 上按下列步骤核对，并把不一致的返回字段发回。

### 步骤
1. 用 `cad_change_begin(sources=[含 .prt/.asm/.drw 的目录])`，对一个零件调用 `cad_inspect_existing`：
   - `dimensions`：名称（d1、d2…）与数值是否对；单位是 inch 的模型值应已换成 mm；角度尺寸 unit 为 deg。
   - `features`：名称/类型/状态；压缩特征 `suppressed=true`。
   - `variables`/`properties`：DOUBLE/INTEGER 参数进 variables，STRING 参数进 properties。
   - `equations`：来自 `file_relations_get`，应是 "d2 = d1*2" 这类行；关系式驱动的尺寸再被改会报 E003。
   - `mass_kg`：对照 Creo 里的质量属性（核对质量单位换算）。
2. `cad_edit_existing` 改一个尺寸 + 压缩一个特征：Creo 中应看到尺寸变化；工作区文件应更新。
3. 故意制造再生失败（如把孔径改到超出板厚）：应返回 `regen.status=errors`，磁盘文件不变，会话中该模型已被 erase。
4. 对装配调 `cad_inspect_existing`：`components` 应是直接子件；`used_by` 应列出引用该零件的 .asm/.drw。
5. 改零件后检查引用它的装配/工程图是否被 refresh（再生并保存）。

### 需要实机确认的 creopyson / CREOSON 假设
- `dimension_list_detail` 返回 `name/value/dim_type/dwg_dim`；`dim_type == "angular"` 视为角度（deg）。
  CREOSON 不返回"是否从动"：按名称 `rdN` 判定为参考（从动）尺寸。若公司模板把尺寸改了名，这个判断会失效。
- 尺寸与特征无直接对应：`dimensions[].feature` 恒为 None，`features[].parents` 恒为 []（E004 连带压缩警告因此不会触发）。
- `feature_list` 返回字段按 `name / feat_id|feature_id / feat_type|type / status` 容错读取；状态含 SUPPRESSED、INACTIVE 等视为已压缩。
  调用时带了 `inc_unnamed=True`（否则无名特征不列出）。
- `file_get_length_units` / `file_get_mass_units` 返回的单位字符串（如 "inch"、"mm"、"lbm"、"tonne"）能否被识别；
  不能识别时按 mm / kg 处理并在 `warnings` 中提示。mmNs 单位制的质量单位是 tonne，务必核对 `mass_kg`。
- `file_massprops()["mass"]` 的单位是模型质量单位（而非固定 kg）。
- `bom_get_paths(top_level=True)` 返回 `{"children": {"file":…, "children":[{"file", "seq_path", "status"}]}}`，
  直接子件在根节点的 children 里；若实际结构是平铺列表，`_children` 也兼容。instance 取 `seq_path`。
- `drawing_list_models(drawing=…)` 返回工程图引用的模型文件名。
- `file_save` 在 Creo 里会生成新版本号文件（`part.prt.2`）；工作区里文件名因此可能从 `.prt` 变为 `.prt.N`。
  需确认 Gateway 以哪个文件为准（必要时在 CREOSON 侧保存后清理旧版本，或配置 `keep_versions` 策略）。
- 再生失败是否以异常抛出（本实现假定抛异常；若只返回状态，需改为读取 `file_open_errors`/特征状态）。
- 失败时用 `file_erase(file_=…)` 把未保存的修改清出会话，磁盘文件保持原样。

### 当前不支持（如实返回）
- `replace_component`：creopyson 没有替换组件函数，抛 `CAPABILITY_UNSUPPORTED`（能力表 `replace_component=none`）。
  需要 CREOSON 扩展命令（J-Link 替换组件）。
- `retarget`：Creo 装配/工程图按模型名 + 搜索路径引用，不在文件内存路径，直接返回 0（不是错误）。
- `refresh` 的悬空标注数（`dangling`）固定为 0：CREOSON 没有读取命令，需扩展；工程图 refresh 也不导出 PDF。
- 包围盒 `bbox_mm` 为 None；`set_material` 要求材料已在 Creo 材料库（`file_set_cur_material`）。

## Creo 出图（CREOSON / creopyson，partial）

实现在 `src/cadmcp/adapters/creo/drawing.py`；节点配置（`config/node.windows.example.yaml` 的 creo 目标）可加：
`templates: {drawing_A3: "a3_gb.drw", drawing_A4: …}`（各图幅 Creo 图纸模板，含参数化标题栏）、
`sheet_unit_scale: 1.0`（模板图纸单位 mm；英寸模板填 0.03937）、`view_map: {iso: "TRIMETRIC"}`（规划视图 → 模型命名视图）。
函数名与参数名已对照 creopyson 0.7.8 源码核对（`tests/creopyson_0.7.8_sigs.json`，`tests/test_creopyson_sigs.py` 保证假模块不偏离）；实机仍需核对的是返回值形态、坐标/单位与朝向，以及 `cadmcp` 扩展命令（J-Link 侧需另行实现）。

### creopyson API 假设（先 `help(creopyson.Client.<name>)` 核对函数名和参数名）
- `drawing_create(template, model, drawing, scale, display, activate, new_window)`：返回图纸名；`scale` 是数值（1:2 → 0.5）。
- `drawing_create_gen_view(model_view, point, drawing, view, sheet, model, scale, ...)`、
  `drawing_create_proj_view(parent_view, point, drawing, view, sheet, ...)`：`point` 为 `{"x","y","z"}`。
- `drawing_regenerate(drawing)`、`drawing_list_views(drawing)`（假设返回视图名字符串列表；若返回 dict 列表，执行器取 `view` 键）。
- `parameter_set(name, value, file_, type_="STRING")`、`file_open(file_, dirname, display, activate)`、`file_save(file_)`、
  `interface_export_pdf(file_, dirname, filename)`（与既有 `export()` 同一调用；PDF 导出选项可能需 `pdf_export_*` 配置项）。
- 扩展命令走 `Client._creoson_post({"command": "cadmcp", "function": <name>, "data": {...}})`（私有方法，版本间可能改名）。

### 坐标/朝向假设
1. `origin_on_sheet`（mm）按图纸左下角为原点写入 `point`；核对视图是否落在预期位置，不对则调 `sheet_unit_scale` 或记录实际原点。
2. 模型有命名视图 FRONT/TOP/LEFT/RIGHT/BOTTOM/BACK/TRIMETRIC，且 FRONT 与规划主视图（从 -Y 看、Z 向上）一致；
   `cadmcp_start.prt` 需按此定向，否则用 `view_map` 覆盖或在起始件里重定义视图。
3. 俯/左视图用投影视图（`proj_view`），投影方向由相对主视图的位置决定；模板 `projection_type` 须为 first_angle（GB），
   creopyson 不能设置，需在模板 drawing setup 文件中固定。
4. 轴测图（iso）以 0.5×主比例创建；视图显示样式（隐藏线/消隐）未设置，沿用模板 `drawing_view_display` 默认，请核对虚线是否符合预期。

### 能力边界与企业扩展（缺失时 oplog/regen.warnings 有明确警告，不静默）
creopyson 无法创建尺寸、注释、表格、剖视图、公差符号，计划里规划为 CREOSON 扩展命令（需 J-Link 实现，约定同 `compile.py`）：
`cadmcp_show_dims`（显示模型尺寸并回读）、`cadmcp_note`（技术要求/其余粗糙度/更改记录）、`cadmcp_table`（BOM/孔表/切割清单）、
`cadmcp_section_view`（剖视/局部剖/移出断面）、`cadmcp_flat_pattern_view`、`cadmcp_gdt`（基准/形位公差/粗糙度/焊缝）、`cadmcp_balloons`。
未部署扩展时：图纸只有视图 + 标题栏参数 + PDF，`regen.status = warnings`，warnings 逐条列出缺哪些内容。
扩展就绪后在此补每个命令的 data 字段验收（字段见 `compile_drawing` 的 args）。

### 实机核对步骤
1. 配好 `templates` 后出 `tests/test_core.py` 的 PLATE_PRECISION 零件图：确认 drawing 创建、视图数 = 方案、比例/位置正确、PDF 生成。
2. 标题栏：模板中用 `&PART_NO &TITLE &MATERIAL &SCALE &MASS &DRAWN_BY &CHECKED_BY &DATE &REVISION` 引用模型参数；
   检查是否显示。若模板读的是图纸（drawing）参数而非模型参数，需改为 `&mdl_param` 或改本实现为同时给 .drw 设参数。
3. 打开 `<name>.oplog.json`：逐条看 ok/error；`creoson.*` 关键步骤失败会中止并报 NATIVE_ERROR，把 oplog 发回。
4. 保存路径：本实现 `creo_cd(out_dir)` 后保存；确认 .drw 落在 out_dir（Creo 可能生成 `.drw.N` 版本号文件）。
5. `rendered.views` 只含 `drawing_list_views` 回读到的视图名；尺寸/注释数量未回读（无 API），D005 因此不会触发。

## Creo TOOLKIT（cadmcp_tk：从零建模 + 出图细节）

编译、运行与核对顺序见 `creo_toolkit/README.md`，协议见 `docs/CREO_TOOLKIT.md`。creo 目标配置加
`toolkit: {host: 127.0.0.1, port: 9057}` 后：
- `build_part`：`compile_part_tk` → `tk.model.new_part` → 参数 → `tk.feat.*` / `tk.datum.*` → 再生 → 特征状态 → 保存；
  oplog 中每步的 args 就是发给 cadmcp_tk 的原始请求，可直接用 `ToolkitClient().call(cmd, **args)` 单步重放。
- `build_drawing`：视图仍由 CREOSON 建（被剖视替代的视图也先建成投影视图），细节逐项 `tk.drw.*`；
  坐标全部来自规划层（与 SVG 预览一致），对照 `<name>.svg` 检查位置。

### 验收
1. PLATE_PRECISION：零件建出 Base + Pin 孔；工程图有总体尺寸、6 个坐标尺寸、孔标注、基准 A/B、3 个公差框格、2 个粗糙度、技术要求。
2. SHAFT：旋转体 + 2 个键槽 + 挡圈槽；主视图直径尺寸带 Φ 和公差带。
3. GRID（`tests/test_hole_table.py`）：孔表位置与 SVG 一致，孔号注释在孔旁。
4. 坐标映射：在 Creo 中查看 TOP 面草绘的矩形是否与 IR 的 X/Y 一致、FRONT 命名视图是否从 −Y 看（Z 向上）。

## SW 装配：偏移/旋转/阵列坐标系配合 + regenerate（sw-asm-mates）

`compile_assembly` 对每个带 `place` 的实例比较两个世界位姿：组件 align 坐标系 W（= 实例位姿 × align 坐标系）与目标坐标系 T。
- W = T（无 offset/rotate）：组件坐标系与目标坐标系直接做坐标系重合配合（AddMate5 swMateCOORDINATE）。
- W ≠ T（有 offset/rotate，或阵列副本）：先在装配中用 `CreateCoordinateSystemUsingNumericalValues` 按 W 建参考坐标系
  `MC_<实例>`（`#` 换成 `_`），再与组件坐标系重合。旋转角由 `euler_xyz` 分解，约定 R = Rz·Ry·Rx（固定轴 X→Y→Z）。
- 不采用"坐标系重合 + 距离/角度配合"：坐标系配合已约束 6 个自由度，再加会过定义；拆成 3 距离 + 3 角度有翻转歧义。
  代价：`MC_*` 是装配级数值坐标系，目标组件移动后不随动（全部位姿由 spec 重算重建，影响有限）。
- 任一步失败仍退回"按位姿固定"（oplog `result.mate = fixed_at_transform` 与 `reason`）。

`SolidWorksAdapter.regenerate(path)`：OpenDoc6 → ForceRebuild3(False) → 遍历特征 GetErrorCode2，
返回 `{status: ok|warnings|errors, errors: ["特征: error 码"], warnings: [...], oplog}`；oplog 为 `<名>.regen.oplog.json`。

### 实机核对
1. 出 `examples/cylinder_station.py` 装配：两个螺钉（offset [12,12,0] + 阵列步距 [-24,0,0]）的 oplog 应为 `mate=coordinate, via=MC_screw_1/2`，
   且螺钉头落在安装板 CS_Cyl 偏移 (±12, 12) 处、配合树中无过定义/错误。
2. TODO(实机核对)：装配环境下 `IFeatureManager.CreateCoordinateSystemUsingNumericalValues` 是否可用（SW 2021+）；
   若返回 None，记录版本，改用"装配中插入参考点 + 坐标系（原点=点，X/Y 轴=草图线）"方案。
3. TODO(实机核对)：数值坐标系的 X/Y/Z 旋转顺序是否为固定轴 X→Y→Z。验证：在 spec 中给某组件 `rotate: [90, 0, 90]`，
   比较 SW 中 MC_ 坐标系三轴方向与平台预测（`euler_xyz` 的逆 `rotate_xyz`）；不一致时调整 `op_mate_coordinate` 中的分解顺序。
4. TODO(实机核对)：装配级特征 `Feature.Select2(True, 1)` 能否与 `SelectByID2(..., "COORDSYS", ..., mark=1)` 组合成坐标系配合的两个选择。
5. regenerate：对一个故意引用丢失的零件调用，确认 errors 列出特征名与错误码。

### Creo TOOLKIT 装配（asm.*）
1. 先建 `examples/cylinder_station.py` 的两块板，再建 ASSEMBLY：库件无 `<library_root>/<编码>.prt` 时平台先经 TOOLKIT 建包络占位件。
2. 核对元件位置与 SVG/模拟结果一致（坐标映射 `(x, z, −y)` + `Q·T·Qᵀ`）；oplog 中 `asm.add_component` 的 `placement` 应为 `csys`（有 place 无偏移）或 `fixed`。
3. 人为让两件重叠，`asm.interference` 回读 `{components, volume_mm3}` 应出现在 build 结果的 interferences 中。

## SW 草图完全定义 / 边线法兰（dimension_profile / sheet_metal_edge_flange）

规划在 CAD 无关层：`src/cadmcp/sketch_plan.py`（外形草图的关系 + 驱动尺寸，自由度恰好为 0）、
`ir/resolve.py` 的 `bends[].edge_point / expect_z`（翻边选边点与翻边后 Z 向范围）。

### 待实机核对
1. `SketchAddConstraints` 的关系 ID：`sgCOINCIDENT`、`sgVERTICALPOINTS2D`（两点竖直对齐 = 同 x）、`sgHORIZONTALPOINTS2D`、`sgHORIZONTAL2D`、`sgVERTICAL2D`。
2. 草图原点选择：`SelectByID2("Point1@Origin", "EXTSKETCHPOINT", …)`；中文界面名称可能不同（代码已退回按坐标选）。
3. `AddDimension2(x, y, 0)`：两点尺寸按放置位置决定水平/竖直（规划把 dx 放在两点上方、dy 放在右侧）；确认无"修改尺寸"弹窗（已临时关 `swInputDimValOnCreate`）。
4. 回读：oplog 中 `dimension_profile.result.status` 应为 `fully`；否则为 `raw:<值>`，对照 `swConstrainedStatus_e`。
   方程式 `"D1@SK_Base" = "W"` 是否被接受（尺寸名来自 `IDimension.Name`）。
5. `InsertSheetMetalEdgeFlange2` 14 个实参顺序与含义（FlangeEdges, SketchFeats, Options, Angle(rad), Radius(m), BendPosition,
   Length(m), ReliefType, ReliefRatio, ReliefWidth, ReliefDepth, ReliefTearType, CustomBendAllowance, ByRef Errors），
   `SketchFeats` 传 `[None]` 是否可行；`swFlangePositionTypeMaterialInside`、`swInsertEdgeFlangeUseDefaultRelief` 常量名。
6. 翻边方向：向上翻选顶面（z=t）边、向下翻选底面（z=0）边。`check_body_extent` 会比较实体 Z 范围与规划；
   报"不符"时看是方向反了还是长度类型不是"外部虚拟交点"（需经 `GetDefinition()` 的 IEdgeFlangeFeatureData 设置）。
7. 验收：`tests/test_sw_sheetmetal.py` 的 SM（U 形，前边上翻 30、后边下翻 25）建模后 Z 范围 −23..30 mm，展开尺寸与 SVG 展开图一致。

## Creo 轴类移出断面（drw.removed_section）
TOOLKIT 路径：CREOSON 以 `view_map.section`（默认 `TOP`，即沿 IR Z 轴线观察）建一般视图 `V_SEC_A`… →
`drw.removed_section{view, label, z, parent}`：偏移剖截面（平面 IR z = 剖切位置）→ 2D 剖视（箭头在主视图）→
`ProDrawingView2DSectionAreaSet` 只显示切面。断面尺寸（槽宽 N9、d−t）发到 `V_SEC_*`，文字覆盖。
1. 出 SHAFT（tests/test_core.py）图：两个断面位置在键槽正下方、只显示切面、键槽朝上（若朝下：改 `view_map.section` 为 BOTTOM 或在起始件定义命名视图）。
2. 剖截面为空 → `cmd_drw_removed_section` 的 flip / SIDE_2 取反；`section_only=false` → 查 `ProDrawingView2DSectionAreaSet` 返回码。
3. d−t 尺寸：Creo 对圆边默认附着圆心，文字覆盖显示正确数值，但尺寸界线需实机确认是否到圆的远端。


## 孔中心符号 / 剖视图孔壁粗糙度 / 折弯线定位（本轮新增，TODO(实机核对)）
- `op_center_marks`：每个正投影视图调 `IView.AutoInsertCenterMarks2(types=5, connection=0, …, UseDocumentDefaults=True)`。
  核对：`swAutoInsertCenterMarkTypes_e` 是否为位掩码（孔 1 / 圆角 2 / 槽 4）；外形 R 圆角不应出现中心符号；
  剖视图（全剖/阶梯剖）中被剖孔的轴线是否由 SW 自动生成，若没有，记下 oplog 并截图。
  分布圆（细点画线圆）目前只在预览中画；SW 原生需要 `connection` 取"圆形连接线"对应的枚举值，请在 API 帮助里查
  `swCenterMarkConnectionLine_e` 后告诉我们。
- 孔壁粗糙度：大孔被剖开时 `surface_finish` 的 `sheet_at` 落在剖视图孔壁上，检查符号是否附着到孔壁边线、方向朝孔内。
- 钣金展开图：`overall_dimension` 的 `flat_bend0/1` 一端选外轮廓边、一端选折弯线，检查能否选中折弯线（草图线）。
- Creo `drw.show_axes`：每个正投影视图应显示孔/圆柱的基准轴（中心线）。核对 `ProSolidAxisVisit` 与
  `ProAnnotationShow`（PRO_AXIS 模型项）在 Creo 7/9/10 的可用性；应答 `failed > 0` 时把 oplog 发回。
- SW 配合公差：轴段直径 / 键槽宽应显示代号 + 叠写偏差（SW 自带 ISO 286 表）。核对 `swTolFITWITHTOL`
  取值（缺省 9）与 `IDimensionTolerance.SetFitValues(孔, 轴)`；数值应与预览一致（如 Φ20k6 +0.015/+0.002）。
  失败时 readback 有 `dim_fit_failed`，图上退回文字后缀。Creo 端 `drw.dim` 的 `tol` 参数走原生正负公差（ProDimensionToltypeSet + ProDimensionToleranceSet，
  会话里打开 tol_display）；应答 `tol_set: false` 时核对下偏差符号约定与签名。
- 齿轮（tests/test_gear.py 的 GR1）：SW/Creo 零件应有 40 个渐开线齿（折线草图约 680 段，核对建模耗时与质量 ≈0.70 kg）。
  出图：端面视图（SW *Front / Creo 沿 IR Z）+ 过键槽的全剖主视图（轴线水平；Creo 用 RIGHT 命名视图，核对朝向）+
  右上角参数表（SW `op_table` 的 swBOMConfigurationAnchor_TopLeft 取值缺省 1）。原生图上齿形按实际几何显示（非规定画法），
  分度线 / Φd 不生成（warn）。
- 齿轮轴（tests/test_gear.py 的 GS）：Front Plane 上齿廓折线 + 外圆，`keyway_cut` 起始偏移 40、深 36 切出 20 个齿槽；
  核对 FeatureCut4 对嵌套轮廓只切两轮廓之间的区域。Creo 同理（TOP 偏移 40 拉伸切除）。

## 细长轴断开画法（break_view，TODO(实机核对)）

样例：`tests/test_broken_view.py` 的 LS（652 mm 长轴，第 3 段 520 mm 光轴）。规划 A3 1:1，断开区间 z 82–542，图上间隙 8 mm。

SW：
- `IView.InsertBreak3(orientation, pos1, pos2, style, 1, False)`（失败时退到 InsertBreak2 / InsertBreak），然后选中视图 `IDrawingDoc.BreakView()`。
- 核对：`swBreakLineVertical`、`swBreakLine_Curve` 常量取值（常量库缺失时用 1、2）；pos1/pos2 是相对视图原点的**模型** m 还是图纸 m
  （当前传模型 z × 0.001，轴左端 = 模型原点）；断裂线间隙 `swDetailingBreakLineGap` 设为 8 mm 是否生效。
- 断开后视图中心是否回到规划位置（`IView.Position`）；尺寸、剖切线、精度符号的选边点是否落在断开后的几何上。
- 520 尺寸应注实长（SW 断开视图默认如此）。

Creo：Toolkit 没有创建断开视图的函数（只有 `ProDrawingViewBrokenVisibleAreaGet/Set`），Creo 目标自动 `broken_view: false` 画全长。
如需支持，可核对 `ProDrawingViewBrokenVisibleAreaSet` 能否把一般视图改成断开视图（PTC 文章 CS412079）。

## 轴端中心孔标注（GB/T 4459.5）

样例：`tests/test_center_holes.py`。SW 走 `leader_note`（引线箭头在端面轴线处），Creo 走 `tk.drw.note` + `leader_to`。
核对：引线是否附着到端面（轴线与端面交点不是边线，可能附着失败而成为自由注释）；预览里的保留符号 "<" 原生端没有画，
保留要求只体现在技术要求里。

## 待核实的标准数值（不需要 Windows，需要标准原文）

| 表 | 位置 | 状态 |
|---|---|---|
| GB/T 145 中心孔 A 型 D、B 型 D2 | `standards.CENTER_HOLES` | 已对照 IS 2473:1975（等同 ISO 866）核实 |
| GB/T 145 中心孔 l1 | 同上 | 未核实（出图不用） |
| GB/T 275 轴颈圆柱度（0 级轴承） | `precision._BEARING_CYL` | 未核实 |
| GB/T 275 轴肩端面圆跳动（0 级轴承） | `precision._BEARING_SHOULDER` | 未核实 |
| GB/T 1184 垂直度 7/8 级 | `precision._PERP` | 未核实 |
| GB/T 1184 对称度 8 级（键槽，主参数键宽） | `precision._SYM8` | 未核实 |
| GB/T 1184 圆跳动 6/7 级 | `precision._RUNOUT` | 未核实 |
| 齿坯跳动公差 | `gear._BLANK_RUNOUT` | 未核实 |
| 按轴端直径推荐中心孔 d | `standards._CH_BY_DIA` | 经验值，非标准表 |

## 套类/空心轴（shaft bores，TODO(实机核对)）

样例：`tests/test_shaft_bores.py` 的 SL（法兰轴套，Φ30H7 通孔 + Φ36×8 沉孔）。
- 建模：SW 每个内孔 = Front Plane 画圆 + FeatureCut4（起始偏移 z0、深度 = 孔深）；Creo = TOP 偏移 z0 拉伸切除。核对盲孔方向（side=end 时 z0 = L − 深度，向 +Z 切）。
- 出图：主视图原位全剖。SW = 视图外框矩形 + `CreateBreakOutSection(深度 = 外圆半径)`；核对深度是否正好剖到轴线、剖面线是否只在材料上。
  Creo = `drw.section kind=local`，边界 = 视图外框，`cut_points` 取轴线两点；核对剖截面是否过轴线且垂直于视线。
- 尺寸：剖视中端面/轴肩选边点取在孔壁与外圆之间，盲孔孔底取孔径内的点；核对能否选中。

## 轴端螺孔（end_holes，TODO(实机核对)）

样例：`tests/test_end_holes.py`。SW：底孔 FeatureCut4（Φ = d − P，平底）+ 孔口圆边 `InsertCosmeticThread3`（Internal、Metric Tap）；
核对孔口边选点 (tap/2, 0, z端) 能否选中、主视图隐藏线（swHIDDEN_GREYED）是否显示螺孔虚线。
Creo：拉伸切除 + `feat.cosmetic_thread hole_feature=EHn`；核对装饰螺纹能否加在拉伸切除的孔面上（原用于孔特征）。

### 套类内键槽（bores[].keyway）
- 模型里键槽在 **−Y**：按 SW 轴类主视图 `*Right` 转 180° 时 −Y 朝上推算，左视图用 `*Back` 转 180°（从左端看、−Y 朝上）。
  实机核对两个视图里键槽是否都朝上、左视图是否真是从 z=0 端看；不对就改 `SW_SHAFT_VIEW` 与左视图映射（compile.py 一处）。
- Creo 左视图是主视图的投影视图，随主视图朝向（仍依赖 FRONT 命名视图定向为轴线水平）。
- 轮毂槽深 t2 表（`standards.HUB_T2`）按 GB/T 1095 常用值录入，未对照原文。

## 砂轮越程槽 + 局部放大图（TODO(实机核对)）

样例：`tests/test_grinding_undercut.py`（GR1：两处越程槽 → 放大图 I、II，5:1）。
- SW：`detail_view` = 主视图里 `CreateCircleByRadius` → `CreateDetailViewAt4`（12 个参数；Style/Showtype 枚举取 0；
  锯齿轮廓）。核对放大图位置、比例 5:1、标签 I；放大图里槽宽/槽深两个尺寸能否按选边点生成（视图名 `detail:I`）。
- Creo：`tk.drw.detail_view` → `ProDrawingViewDetailCreate`（签名、边界样条坐标系待核对）+ `ProDrawingViewScaleSet`；
  新视图用别名 V_DET_I 登记在 TOOLKIT 会话里，后续 `drw.dim` 按别名找。CREOSON 扩展路径不建放大图（发 warn）。
- 越程槽表 `standards.GRINDING_UNDERCUT`（GB/T 6403.5 磨外圆 b1/h/r）按手册常用值录入，未对照原文；槽底圆角 r 未建模
  （模型和图上都是直角槽底）。

## 热处理粗点画线（TODO(实机核对)）
样例：`tests/test_heat_treatment.py`。SW `view_line`：视图草图直线 → `SetLineStyle("CENTER")` + `SetLineWidth(swLW_THICK)`，
核对线型名与线宽枚举；Creo `tk.drw.line`：`ProDtlentityCreate`，线型 CTRLFONT、线宽 0.5。
热处理硬度经验值（`materials.HEAT_TREATMENT`）未对照手册。

### 空心轴外键槽
- 空心轴的键槽固定建在 −Y（angle 270：Top Plane 草图、翻转切除），按 SW 轴类主视图 −Y 朝上推算，主视图原位全剖时
  剖切面过键槽。实机核对：主视图上方是否出现键槽缺口；移出断面 A-A 中键槽是否朝上、内孔是否显示。

## 法兰均布孔（flange_holes，TODO(实机核对)）
样例：`tests/test_flange_holes.py`（EC1 轴承端盖）。第一个孔在模型 −Y（按 SW 轴类主视图 −Y 朝上），原位全剖时剖切面过该孔；
**孔数为奇数时原生剖视只切到上方一个孔**，下方不会像预览（GB 规定画法：旋转到剖切平面）那样出现孔——需手工补画或接受。
左视图（`*Back` 转 180°）核对孔的分布方位与预览一致（+X 朝右）。

## 轴肩圆角（shoulder_fillet）
SW：`select_edges_at` 按模型点 (r, 0, z) 选轴肩根部圆边（SelectByID2 EDGE，追加选择）→ FeatureFillet3；核对选中边数 = 轴肩数。
Creo：`feat.round` edges 同样给点坐标。

## 轴肩端面几何公差（shoulder:<i>，TODO(实机核对)）
- `auto_precision` 的齿轮轴样例：轴承位两侧的定位轴肩各有一个对 A-B 的端面圆跳动框格。
- SW：`gtol` 的 `at` 落在主视图轴肩端面竖线中点，`_select_edge_at` 应选中端面轮廓边，框格引线指向它；核对引线是否
  附着在端面（而不是相邻圆柱面的轮廓线）。Creo：`tk.drw.gtol` 同一点附着。
- SVG 预览的折线引线（小端一侧水平箭头）在原生图里是直引线，位置差异属预期。

## 轴径向横孔（cross_holes，TODO(实机核对)）
- `tests/test_cross_holes.py` 的 XS 样例：通孔 Φ6H7 = Right Plane 上两个草图各切一半（`XH1` 向 +X、`XH1_B` 向 −X，
  起始偏移 0）；盲螺孔 M6 = 草图面偏移到孔底（R − 底孔深）向 +X 切出。核对 SW 主视图（*Right）上两个孔是圆、
  盲孔开口朝向观察者；`XH2_THR` 装饰螺纹选边点 (√(R²−r²), r, z) 是否落在孔口交线上。
- Creo：通孔草绘面 RIGHT 偏移 R+1、反向切 2R+2；盲孔草绘面在孔底向 +X 切。核对 Creo 主视图方向与 SW 一致（+X 朝观察者）。
- 定位尺寸第二点选孔圆顶点：SW AddDimension 应量到圆心。

## 键槽对称度（keyway:<name>，TODO(实机核对)）
- 齿轮轴 auto_precision 样例：断面图 D-D 左侧 ⌯ 0.015 C。SW `gtol` 的 view = `section:D-D`，`at` 在槽宽尺寸线左端
  （槽侧壁延长线上，模型边线不一定在那里）——核对 `_select_edge_at` 是否选中槽侧壁；选不中时框格无引线附着，
  需改为附着到槽宽尺寸（SW：选中尺寸后 InsertGtol）。Creo `tk.drw.gtol` 同。

### 竖直横孔局部剖（direction: vertical）
- `tests/test_cross_holes.py` 的 XV 样例：SW 每孔一个 `broken_out_section`（孔周矩形、深度 = 轴半径），核对局部剖
  边界是否画成波浪线（SW 样条边界）、剖面线是否只在剖到的实体上；Top Plane 上的孔从 −Y（主视图上方）钻入。
- Creo：同一视图上多处 `tk.drw.section kind=local` 能否共存（TODO）。
- 竖直横孔定位尺寸：规划坐标 `sheet_pts` 量到孔轴线（预览一致），原生选边用 `pick_pts`（左孔壁线上一点，SW/Creo 都优先读它）。
  核对选孔壁后原生尺寸值是否为“到孔轴线”（如 7.5）；若量成到孔壁（5），需改为选孔轴线或改文字覆盖。

### 型腔底面粗糙度（roughness target `pocket:<名>`）
- 主视图全剖中，符号应立在型腔底面线上（型腔内、靠左 1/4）；核对 SW/Creo 能否在剖视图的底面边上挂符号。

### 装配图配合尺寸（tests/test_fit_dims.py 销座组件）
- SW：`overall_dimension` 选销 Φ20 两条轮廓边，前缀 Φ、后缀 H7/k6，核对显示为 Φ20H7/k6。
- Creo：`tk.drw.dim` 文字覆盖 Φ20H7/k6，核对选边点落在销的轮廓上（装配视图中两零件边重合时可能选到孔边，同样正确）。

### 装配图主视图全剖（tests/test_fit_dims.py 销座组件、轴承组件）
- SW：`section_cut` 的 `exclude_components`（Section Scope 排除零部件）API 待核对；相邻零件剖面线方向是否自动错开。
- Creo：`tk.drw.section` 装配剖面 + 排除零部件待实现/核对。

## 带凸台的板（boss，TODO(实机核对)）
- 样例：`tests/test_plate_review.py::test_boss_auto_surface`（Φ30 凸台 + 板面型腔 + 四角螺孔 + 凸台顶螺孔）、
  `test_sw_boss_sketch_on_own_base`（两个凸台）。
- SW：每个凸台的 `sketch_on_face` 射线从凸台自身中心、凸台底面（板面）上方 0.5 mm 打下；两个凸台都应立在板面上，
  第二个不落在第一个凸台顶上。板面上的型腔/螺孔从板面（z = 板厚）起切，凸台上的螺孔从凸台顶起切。
- Creo：型腔/孔的草绘面 = TOP 偏移 `at`（解析层自动取所在表面：板面或凸台顶）；凸台 = TOP 偏移 z0 向上拉伸。
- 工程图：主视图（或剖视）外形为板 + 凸台，剖视中凸台与板之间无分界线；俯视图有凸台轮廓；
  引出标注“Φ30 高10”指在凸台轮廓上；凸台圆心进坐标尺寸。

## 侧面孔与右视图（TODO(实机核对)）
- 样例：`tests/test_plate_review.py::SIDE_HOLES`（顶面通孔 + 前面 2×M6 盲螺孔 + 右侧面 M8 盲螺孔）。
- 侧面特征只在右侧面时，左视图换成右视图（第一角画法放在主视图左侧）：SW `*Right`（旋转 90°）、Creo `RIGHT` 投影视图，
  核对视图方向（+Y 朝右）与位置。
- 主视图局部剖剖出右侧面螺孔整段孔深；前面螺孔两个实例都在主视图上可见（不被剖切移去）。

## 轴类短轴段直径偏差写全（TODO(实机核对)）
- 直径极限偏差一律写全（不再在放不下时只注公差带代号）；文字放不进该段时向上伸出轮廓，规划层 `extra["over_at"]`
  给出文字下端高度（越过上方尺寸行），`sheet_pos` 同步。核对 SW/Creo 直径尺寸文字是否放在该位置、尺寸线是否随之延长，
  上下偏差是否叠写（GB/T 4458.5）。样例：`tests/test_fuzz_layout.py::test_short_step_keeps_deviations`。
- 向下伸出的同样处理：文字移到下方尺寸行之下（`over_at` 仍为文字下端），被延长直径线穿过的长度尺寸数字
  在本尺寸界线之间偏开（`text_x`）。
- 键槽断面图（小比例）d−t 尺寸放不进尺寸线之间时，预览把数字向**下**伸出（上方是槽宽尺寸及公差带）。
  核对 SW/Creo 断面图里该尺寸数字是否同样不压槽宽标注；压到时把数字拖到尺寸线下方延长处。
- 轴右端贴图框的短定位尺寸（如键槽到端面 3.5）：数字写在左侧界线外（`text_x`），相邻长度尺寸的数字在自己界线之间左移
  留出 ≥ 3 mm（避免读成“183.5”）。核对 SW/Creo 里两个数字的位置是否按 `text_x` 放置、没有出图框。

## 轴段长度链开口环（参考尺寸，TODO(实机核对)）
- 段长全注 + 总长会成封闭尺寸链（GB/T 4458.4）：规划层把最不重要的一段注成参考尺寸，`extra["reference"] = True`，文字 `(L)`。
- SolidWorks：`IDisplayDimension.ShowParenthesis = True`（失败记 readback `dim_paren_failed`）。核对显示为带括号的数值。
- Creo：尺寸文字写 `(@D)`（保留实测值加括号）。核对 `ProDimensionTextWstringsSet` 接受 `@D` 且显示为 `(40)` 形式。
- 样例：`tests/test_plate_review.py::test_shaft_open_ring_reference_dim`。
- 横孔引线标注（`xh:`）：上方穿字或出框时可改为**向下**引出——箭头在孔圆周右下/左下 45°，水平线在主视图最低一道尺寸之下 8 mm。
  规划层改写 `label.anchor`（箭头点）；核对 SW/Creo 引线注释的箭头是否落在孔圆周、文字位置是否按 `text_at`。
- 尺寸文字位置（`extra["text_x"]`）：规划层避让挪过的数字、写不进两界线之间的短尺寸数字（写到界线外哪一侧）
  现在都在规划层定好。SW `AddDimension2` 与 Creo TOOLKIT 的放置点 x 取 `text_x`、y 取 `sheet_pos` 的尺寸线行
  （`detailing.dim_text_pos`）。核对：短尺寸（如键槽到轴肩 3.5）数字是否与预览同侧、尺寸线是否延长到数字下；
  SW 若把文字拉回两箭头之间，记下版本与现象。Creo 扩展路径（`ext.cadmcp_show_dims`）收到的 `extra` 含 `text_x`，扩展须同样使用。
- 轴/套主视图直径尺寸放置点：`sheet_pos` 的 x 现在就是规划层选定的直径线位置（`shaft_dia_z` / `bore_dia_place`，
  已避开键槽、横孔、轴肩），原先是 x + 6，会让原生直径线右移 6 mm、可能压到键槽。核对：SW/Creo 直径尺寸线与预览同一位置；
  文字在线左侧（GB 竖直尺寸文字在线左、字头朝左）。预览文字写在线右侧的段（`shaft_dia_place` 返回 right，左侧有键槽/轴肩线）
  原生图暂未翻转，记下哪些段文字压线。
- 空心轴/套内孔直径（`bore<k>_dia`）孔里写不下时：文字整段写到外轮廓之外（`over_at` = 文字下端，向上时为外轮廓上方 1 mm，
  必要时再抬过上方尺寸行），`sheet_pos` 同步为文字中点；尺寸线穿过孔壁剖面延长到文字。核对 SW/Creo 文字是否在零件外、
  尺寸线是否随之延长。
- 挡圈槽挤不下时的局部放大图（`detailing.circlip_crowded`）：定位尺寸（轴段起点 → 槽左壁）在主视图写不进两界线之间时，
  该槽改画局部放大图（2–2.5:1 为主），主视图只留“挡圈槽 Φd2×m（GB/T 894-d）”引出；放大图内注槽宽 `G*_w`、槽深 `G*_h`、
  定位 `G*_pos`（外圆上方，选边点 `pick_pts` 为轴段端面与槽左壁）。槽宽写不进时箭头在外、数字移到槽深尺寸另一侧（`arrows_out`）。
  核对：SW `CreateDetailViewAt4` 的圆是否包住轴段端面、三个尺寸能否在放大图里选到边；Creo `tk.drw.detail_view` 同理。
  样例：`tests/test_fuzz_layout.py::test_crowded_circlip_goes_to_detail_view`。
- 内孔 Ra 标在孔径尺寸线上（GB/T 131）：孔小、孔径文字伸出外轮廓上方时，Ra 符号转 90°（与竖写尺寸文字同向）、
  尖端落在尺寸线上、接在文字末端之后 3 mm（`extra["on_dim"]`、`rot` = 90，`sheet_at` = 尖端）。
  SW：`surface_finish` 带 `angle` = 90、`at` = None、无引线，插入后设 `ISFSymbol.Angle`（弧度）——核对旋转方向与基点（尖端）是否与预览一致。
  Creo：`tk.drw.surface_finish` 的 `at` 为 null、`angle` = 90 → `ProDtlsyminstdataAngleSet`——核对角度单位（度/弧度）与旋转基点。
  原生尺寸线不会自动延长到符号尖端（预览补了 2 mm 短线），记下实际观感。
  样例：`tests/test_fuzz_layout.py::test_bore_ra_on_dim_line`。
  轴段同样处理：短轴段上方/下方都放不下 Ra、直径文字向上伸出时，Ra 接在轴段直径文字之后（`on_dim` = `step<i>_dia`），
  样例 `test_shaft_ra_on_dia_line_when_no_room`。
- 轴承位等轴段基准（`datum` target `step:<i>`）：基准为轴线，三角形与该段直径尺寸线对齐挂在轮廓下方（GB/T 1182 中心要素），
  `sheet_at` 的 x = 直径线 x。核对 SW/Creo 基准符号是否在直径尺寸线下端箭头处、原生是否识别为轴线基准（SW 附在尺寸上更好，记下现象）。
- 轴键槽移出断面的剖切位置：`ViewPlan.cut_z`（模型 z，键槽平直段内）；该段有基准且剖切符号会碰到基准方框时挪离键槽中点。
  SW `section_view` 的 `z`、Creo `tk.drw` 剖面 `z`（`shaft_section_z`）都读它（断面图互相推开后不再在剖切位置正下方）。
  核对：原生剖切线位置与预览一致、断面形状正确。
- 上方低一道尺寸行（孔深、横孔定位、放不下的短尺寸）在轮廓上方 10 mm（`TRACK_DY`）；`sheet_pos` 的 y 即该行，原生尺寸按它放。
- 齿轮轴齿面粗糙度（`ra:flank:<名>`，GB/T 4459.2 注在分度线延长线上）：右邻轴段比分度圆粗时改标在齿段左端外，
  符号可沿延长线外移避开上方竖线；延长线由规划层给出 `extra["ext_pts"]`（图纸坐标），SW `view_line`（CENTER、细）、
  Creo `tk.drw.line` 画出。核对：原生延长线与分度线对齐、Ra 尖端落在延长线上。
- 齿轮轴主视图齿轮段的分度线（细点画线）、齿根线（细实线）：非模型几何，原先原生图上没有；现在规划层给出 `plan.lines`
  （图纸坐标，穿过直径文字处断开，GB/T 4458.4），SW `view_line`（CENTER / SOLID、细）、Creo `tk.drw.line`（CTRLFONT / SOLIDFONT）画出。
  核对：线型、线宽是否生效；视图移动后线是否跟随（视图草图线随视图）。齿轮参数表压到标注时改放右侧栏之上或左上角（`gear_table.origin`）。

- 齿面 Ra 两侧延长线都被更粗的轴段挡住（或延长线位置压标注）时，符号尖端直接落在齿段内的分度线上（无 `ext_pts`）：核对 SW/Creo 符号挂在分度线高度（非模型边，自由放置）。
- 短齿轮段夹在更粗的轴段之间时，齿面 Ra 改为带箭头引线指向分度线（`leader_pts` / `leader_segs`）：SW 视图草图细实线、Creo `tk.drw.line` 画引线，
  箭头用 5 条细线填满；符号自由放置在折线上。核对箭头观感是否接近实心、草图线是否随视图移动。
- 键槽剖切位置 `cut_z` 现在也会给齿面 Ra（分度线延长线上的符号）让位：核对 SW/Creo 剖切线位置随 `cut_z`，断面图仍为键槽断面。
- 轴上方定位尺寸第三道（`track: 2`，轮廓上方约 6 mm）：孔深与横孔定位都从同一端面量起、低一道也重叠时使用，核对 SW/Creo 尺寸线高度取规划 `sheet_pos`。
- 小齿轮（A4 竖放，左视图横向伸进右上角参数表范围）：两视图整体下移到参数表下方，放不下换 A3；核对 SW/Creo 视图位置与参数表不重叠。
- 齿轮基准 A（轴孔 150° 径向引出）：腹板放不下方框时方框放到齿顶圆外（`extra["reach"]` 加长、`sheet_pos` 随之）；核对原生基准方框位置。
- 轮毂键槽槽宽/槽深尺寸：文字压同心圆（齿根圆、齿顶圆、外圆）或分布孔时槽宽尺寸线上移、槽深尺寸线外移（`sheet_pos`）；核对原生尺寸线位置。
- 两个键槽断面图相邻：右侧断面左边的对称度框格与左侧断面伸出的槽宽数字之间按两者宽度加大中心距；核对原生断面图位置。
- 局部放大图（小比例）槽宽尺寸：数字压材料侧波浪线时尺寸线移到波浪线之下（`sheet_pos`）；核对原生尺寸线高度。

## 轴类直径文字与轴线（TODO(实机核对)）
- 轴段/内孔直径文字整段写在轴线（细点画线）上方，下端离轴线 1 mm（`dia_text_center`），轴线不穿过数字（GB/T 4458.4）。
  SW/Creo 直径尺寸的 `sheet_pos` 已随之上移：核对原生文字是否落在轴线上方（SW 尺寸文字位置可能被"居中"选项拉回中点，需关掉）。
  螺纹段以小径细实线为界。
- 短轴承位上方是框格行、文字在轴线上方写不下（或上方被局部放大图圆挡住）时：文字居中写在轮廓内，轴线在文字处断开
  （`shaft_dia_axis_break`，`plan.shaft_axis` = 断开后的各段，图纸坐标）。SW 用 `sheet_centerlines` 在图纸草图中分段画中心线；
  核对：主视图是否另有自动插入的整条中心线（有则隐藏），分段中心线是否与视图对齐。Creo 只给 `warn`（基准轴不能分段），按提示的
  x 区间隐藏基准轴后草绘中心线。
- "其余"粗糙度位置：压到视图标注时由出图检查改放（`plan.general_ra_at`），SW/Creo 用同一位置。
