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
