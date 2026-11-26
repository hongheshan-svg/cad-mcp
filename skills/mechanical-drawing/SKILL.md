---
name: mechanical-drawing
description: 用 cad MCP 完成非标自动化设备零件/装配建模与出图（替代人工画图）。当用户要求设计、画图、出图、改尺寸、做装配、出 BOM 时使用。
---

# 机械设计出图工作流

## 0. 开始前
- `cad_list_targets` 确认目标 CAD 与版本；文件将来要在哪个版本打开，就用哪个版本的目标。
- `cad_change_begin(project=<项目号>)`。需要参考历史设计时把路径放进 `sources`。

## 1. 先把需求变成参数
- 列出驱动参数（外形、孔距、板厚、行程…），写进 `parameters`，尺寸一律用表达式引用参数。
- 关键工程参数（气缸推力、电机扭矩、导轨寿命、型材挠度）**必须计算**，不要凭感觉；把计算过程写给用户看。

## 2. 零件 spec 要点
- 板件：`base.type=extrude`，草图在 XY，厚度向 +Z；`top` 是加工主面。
- 孔：用 `type` + 公制规格，平台自动查标准：
  - 螺纹孔 `tapped`（写 `depth` 与 `thread_depth`；铝件螺纹深度 ≥ 1.5d）
  - 螺钉过孔 `clearance`；沉头螺钉 `counterbore`（内六角）/`countersink`
  - 销孔 `simple` + `tolerance: "H7"`
- 阵列孔用 `pattern`（grid / circular），不要逐个列坐标。
- 腰型孔（调节孔）`slot`：`size` 写螺钉规格（如 `"M6"`，宽度取过孔）或数值宽度，`slot_length` 为两端圆心距，`slot_angle` 为方向。
- L 形/台阶件的内侧平面：孔、型腔、凸台写 `at`（该面所在平面的坐标），例如 L 形立板内侧面 `face: "front", at: 8`。
- 轴类：`base.type: "shaft"`，`steps` 从左到右写每段 `length`/`diameter`，配合段写 `tolerance`（如 `k6`、`h7`），`chamfer` 为两端倒角。
- 只写公差带代号即可，平台按 GB/T 1800.2 查表在图上附极限偏差（小一号上下叠写，如 Φ20k6 后 +0.015/+0.002；键槽宽 N9 同样）。
  支持 IT5–IT11、轴 d–p、孔 D–P 及 js/JS，0–500 mm；表外只注代号。不想要偏差时出图设 `fit_deviations: false`。
  - 键槽 `{"feature": "keyway", "step": 段号, "length": 长度}`：宽、深按 GB/T 1095 自动取，长度取 GB/T 1096 系列；默认居中，`offset` 指定起点。
  - 挡圈槽 `{"feature": "groove", "type": "circlip", "step": 段号, "position": "end"}` 按 GB/T 894 自动取；退刀槽 `type: "undercut"`（默认 2×0.5）。
  - 外螺纹 `{"feature": "thread", "step": 段号, "size": "M20"}`（细牙 `"M16x1.5"`）：该段直径须等于螺纹公称直径；
    螺距按 GB/T 193 查，默认 6g、全段螺纹，`length` 指定螺纹长度（自动标注），`side` 为 start/end（缺省：首段在左端、其余在右端）。
    图上以螺纹代号代替 Φ 标在大径上（GB/T 4459.1），SW/Creo 生成装饰螺纹。螺纹收尾处通常配退刀槽 `undercut`。
  - 轴类图纸自动采用：主视图轴线水平、键槽朝前、每个键槽一个移出断面图（标 bN9 与 d-t1）、链式轴段长度 + 总长。
- 钣金：`base.type: "sheet_metal"`，`profile` 为底板外形，`flanges` 写翻边所在边、外形高度、方向；
  翻边上的孔写 `face: "front"` 等（坐标为 (x, z)，z 为距底板外表面的高度），展开位置由平台计算。
  出图加 `{"type": "flat_pattern"}` 视图；展开尺寸按 K 因子计算，技术要求会注明。孔离折弯 < 2t+r 会报 R008。
- 精度要求（有配合、定位、运动的零件必须写，不要只靠"未注公差"）：
  - `datums`：基准，如 `{"name": "A", "target": "face:bottom"}`、`{"name": "A", "target": "step:0"}`。
  - `tolerances`：几何公差，`type` 取 flatness/parallelism/perpendicularity/position/coaxiality/circular_runout 等，
    方向/位置/跳动公差必须带 `datums`，形状公差不带。销孔位置度示例：`{"type": "position", "value": 0.05, "target": "hole:Pin", "datums": ["A", "B"]}`。
  - `roughness`：个别表面 Ra，其余表面按出图的 `surface_finish`。IT6/IT7 配合轴段要标 Ra0.8/1.6（规则 G004 会提醒）。
  - 典型做法：安装底面作基准 A（平面度），侧面基准 B；轴以两轴承位为基准 A、B，其他配合段标圆跳动。
- 和外购件对接的孔（气缸法兰、电机止口+螺钉孔、导轨安装孔、型材端面）一律用 `mount` 特征，不要手抄孔位：
  `{"feature": "mount", "name": "Motor", "item": "SRV-60-400W", "center": [x, y]}` 自动开孔并生成 `CS_Motor`，
  装配时外购件用库里登记的接口坐标系（结果里的 frame，如 CS_Flange）对齐到 `plate.CS_Motor`。A003（装错件/对齐错）必须修，A004 提示还没用 mount。
- 焊接件（方管/角钢/槽钢机架、底座、支架）：`base.type: "weldment"`，`members` 写型钢截面（SHS40x3、RHS60x40x3、
  L50x5、CHS48x3、FB50x6、C10、I14）和两端面中心坐标（只能沿 X/Y/Z），`plates` 写焊接板（地脚板、安装板、封板），
  孔写在板的 `holes` 里，安装基准板设 `machined: true`。杆件端面要贴在别的件上，不要穿入（W001）；
  焊缝、焊脚、切割清单由平台自动算。敞口管端会报 W003：加封板或设 `end_caps: true`。
  简单焊接机架直接用 `cad_build_frame(construction="welded")`。
- 修改共用件：先 `cad_edit_existing(dry_run=true)` 看 `dependents`。E017（已定版目录引用）→ 用 `save_as="新文件名"`
  另存为新图号再改，`set_property` 改图号，再在本项目装配里 `replace_component` 换成新件；
  E018（只报告目录）→ 修改照常进行，但要把受影响文件清单告诉用户，由相关项目负责人复核。
- 铸件（轴承座、底座、箱体类，批量大或形状复杂时）：spec 写**加工后的零件**，`category: "casting"`，材料用
  HT200/HT250/QT450-10/ZG270-500/ZL102/ZL104，加 `casting: {process: sand|investment|die, machined_faces: [...]}`。
  平台自动生成毛坯件（图号加 -M：加工面加余量、大孔铸小、小孔铸后钻）并在出零件图时一起出毛坯图。
  有基准/形位公差/粗糙度要求的面必须列入 machined_faces（C004）；壁厚不得小于材料最小铸造壁厚（C001）。
- 铝型材机架用 `cad_build_frame`（外形 + 型材 + 横梁层高），不要逐根手写组件；返回 cut_list（下料清单）。
  机架以 `asm:<name>` 装进总装；附件、紧固件只进 BOM 时用装配的 `bom_extras`。
- 选型先于建模：气缸、伺服+丝杠、同步带模组、受力螺栓先 `cad_calc` 计算（计算书随版本归档），
  按 `selection` 选库里的型号（`library_matches`），按缸径/法兰定安装板孔位；`warnings` 必须向用户说明。
- 出图结果的 `rules.findings` 中 D009（文字重叠）、D010（孔所在面缺视图）、D022（引线/尺寸线穿过标注文字）要处理：
  按提示调整 `views` 或 `sheet`（多为图幅偏小、标注过密）。
- 等距一排的孔自动标成 `n×p(=L)` 节距尺寸（不进孔表）；装配图的底板安装孔自动标孔距并写入技术要求。
- 每个将被装配的零件都要建 `CS_` 坐标系：放在安装面上、Z 轴朝外。
- 必填：`part_no`、`title`、`material`、`surface_treatment`（铝件常用"阳极氧化"，钢件"发黑/镀锌"）。

## 3. 检查与修正
- 每次 `cad_build_part` 都看 `rules`：`error` 必须修（孔破边、盲孔超料厚、孔重叠），`warning` 逐条判断并告诉用户理由。
- 对比 `measured.mass_kg` 与估算：真实 CAD 偏差 > 3% 说明有特征没按预期生成。

## 4. 装配
- `cad_search_library` 找标准件/外购件，用它们的 `CS_` 基准定位。
- 组件 `place.align`（自己的 CS）→ `place.to`（`<组件id>.<CS名>`）。
- 建完看 `cad_check_interference`；包围盒法会误报，需结合几何判断。

## 5. 出图（每个自制件都要出图）
- `cad_build_drawing({"model": <名称>, "title_block": {"designer": <用户名>}})`，默认 GB 第一角、自动图幅比例。
- 看 `rules`：缺尺寸/标题栏缺项必须修；预览图（preview）给用户确认。
- 装配图带明细表和序号。

## 6. 修改与发布
- 改尺寸用 `cad_set_parameters`，依赖它的装配和图纸自动重建——不要重建整套。
- `cad_validate` 全部通过后 `cad_change_commit(revision)`；发布后的版本不可覆盖。
- 不满意随时 `cad_checkpoint` / `cad_change_rollback`。

## 能力边界（如实告诉用户）
- 当前 spec 支持：板/块类机加工件、阶梯轴（键槽、挡圈槽、退刀槽、外螺纹）、简单钣金（直边折弯）、
  型钢焊接件、铸件（含毛坯图）、标准件/外购件装配。
- 直齿圆柱齿轮（外齿、渐开线标准齿廓）：底座写 `extrude` + `circle`（直径 = 齿顶圆 da = m(z+2+2x)，厚度 = 齿宽），
  加 `gear: {module, teeth, shift?, accuracy?, tip_tolerance?, flank_ra?, mate?}`；轴孔写中心 `hole`（带 H7），
  轮毂键槽写贯通矩形 `pocket`（宽 b、槽顶 = 孔半径 + t2，GB/T 1095）。平台按渐开线建真实齿形，
  出图按 GB/T 4459.2：主视图全剖、轮齿不剖，左视图三圆 + 轴孔键槽，右上角参数表（含公法线长度 Wk 与跨齿数 k）。
  默认自动标齿坯公差：轴孔为基准 A，齿顶圆径向圆跳动、基准端面圆跳动（按分度圆直径与精度等级查表）；
  不要时写 `blank_tolerances: false`，不要再在 `tolerances` 里重复写。
- 齿轮轴：轴类上加 `{"feature": "gear", "name", "step", "module", "teeth", ...}`，该轴段直径 = 齿顶圆、长度 = 齿宽。
  建模切出真实齿槽；出图在主视图齿段画分度线/齿根线，齿顶圆直径带公差，右上角参数表。
  齿轮轴的跳动公差以两端轴承轴颈为公共基准，按需写在 `datums`/`tolerances` 里（平台不自动加）。
- 细长轴（长 ≥ 8 倍最大直径，且有无键槽/环槽/螺纹、无基准与几何公差的长光轴段）自动用断开画法缩短绘制
  （GB/T 4458.1，波浪线），比例按缩短后的长度选，尺寸仍注实长；不想断开时出图设 `broken_view: false`。
  Creo 目标暂不支持原生断开视图，自动画全长。
- 套类/空心轴：`base.bores: [{"diameter": 30, "tolerance": "H7"}, {"diameter": 36, "length": 8, "side": "start"}]`
  （省略 length = 通孔；盲孔平底，从 side 端起）。主视图自动过轴线原位全剖，内孔注直径（带偏差）与孔深。
  壁厚 < 1 mm、两端盲孔重叠会报错；暂不支持内孔与键槽/外螺纹/齿轮同在一件上（可用环槽）。
  内孔可作精度目标 `bore:<序号>`（从 0 起）：基准 `{"name": "A", "target": "bore:0"}` 画在孔径尺寸线处的孔壁上；
  `auto_precision: true` 时套类自动以精度最高的配合内孔为基准 A，外圆配合段注对 A 的径向圆跳动，内孔补 Ra。
- 轴类可设 `"auto_precision": true` 让平台按工程惯例补精度：两端无键槽的轴承位（js/k/m/n、IT5–6）为基准 A、B 并注
  圆柱度（GB/T 275），其余配合段注对 A-B 的径向圆跳动（GB/T 1184），齿轮段按齿坯公差，配合面按公差等级补 Ra。
  已写了 datums/tolerances 时这两类不动，只补粗糙度。设计意图特殊（如轴承不在两端）时自己写。
- 轴端中心孔：`base.center_holes: {"type": "B", "d": 2.5, "ends": "both", "keep": "required"}`（GB/T 145；d 缺省按
  轴端直径推荐，锥口超出轴端会报错）。图上按 GB/T 4459.5 简化标注 `GB/T 4459.5-B2.5/8`，保留要求写入技术要求；
  只出图不建模。需要在完工零件上保留（磨削、检测用）时设 `keep: "required"`。
- 不支持：复杂曲面、斜孔、斜齿轮/锥齿轮/蜗轮/花键、曲线折弯钣金 → 告诉用户需要人工建模，或提出扩展 spec。
- Creo 目标首期完整支持拉伸件与轴类；钣金为部分支持（展开图不建），焊接件不支持——这两类优先用 SolidWorks 目标。
- 发布：`cad_change_commit(revision, description, by)`。改版时 description 写清楚改了什么（如"立柱间距 160→180"），
  平台把版本号写进标题栏、把更改记录写进每张图纸，并在发布目录保存 revisions.json。
- 修改公司现有模型（不是平台建的）：
  1. `cad_change_begin(project, sources=[原文件或目录])` 把零件和图纸一起复制进工作区（原文件不会被改）；
  2. `cad_inspect_existing(file)` 读特征树、尺寸全名、方程式/全局变量、used_by；`used_by_outside` 是共享盘上
     其他引用它的装配/工程图（含间接引用），修改时会自动纳入并一起更新、检查、回写——改共用件前把这个列表告诉用户；
  3. `cad_edit_existing(file, edits)`：尺寸由方程式驱动时改全局变量（set_variable），不要直接改尺寸；
     不确定时先 `dry_run=true`；结果里看 diff（质量/包围盒变化）和 refreshed（图纸是否更新、悬空标注）；
  4. 有 E010 悬空标注先处理（回滚或改图）再发布；`cad_change_commit` 的更改说明会自动生成；
  5. 需要放回原处时 `cad_change_writeback()` 先看计划，再 `confirm=true`；只读文件提示用户在 PDM 检出。
- 替换装配中的零部件（换气缸缸径/行程、换电机）：`cad_edit_existing(装配文件, [{"op":"replace_component",
  "instance":"<实例名>","source":"lib:<编码>"}])`。先 `dry_run=true` 看 E012（安装接口变了：要同步改安装板孔位）、
  E013（配合基准不存在，会断开）、E014（外形变大，查干涉）；替换前先用 `cad_calc` 确认新规格够用。

## BOM 与采购
- 总装完成后 `cad_export_bom(name)`：一个 xlsx 含多级 BOM、汇总 BOM、采购清单（外购/标准件/外协铸件）、原材料汇总、下料明细。
- 导入 ERP 用 `profile="erp"`（父项编码 + 子项编码 + 用量），企业自定义格式在 policy.bom_profiles 配。
- 先处理 B001（同图号不同名称，error）、B004（自制件无材料）；B002 没出图的件要补图；B003 外购件缺品牌要告诉用户；
  B005 是 ERP 里没有的新物料，把清单交给用户建档。

## 装配图明细表与序号
- 序号由平台沿主视图四周按顺时针排布并编号，明细表序号随之重排（只进明细表的紧固件排在最后）；不要手工指定序号。
- D011（明细表与 BOM 不一致）/ D012（序号缺失、重复或无对应行）是 error：先查零件 PartNo 属性，再重出图。

## 剖视图
- 默认 `sections: "auto"`：零件有沉孔/螺纹孔/盲孔/盲型腔/配合孔/侧面孔时，主视图自动画成全剖（一个剖切面能剖到全部）
  或阶梯剖（最多 3 个剖切位置），剖切符号画在俯视图上。一般不用管。
- 需要时手工指定：对称件内外都要表达用 `{"kind": "half", "through": ["孔名"]}`；左视图剖用 `"view": "left"`。
- 内部结构集中在一小段（如长条件上的一个螺纹孔）时自动用局部剖（波浪线分界，视图保留）；
  也可指定 `{"kind": "local", "through": ["孔名"]}`。

## 孔表
- 默认 `hole_table: "auto"`：一个视图孔数 ≥16 或坐标值过多时，孔旁只标孔号（A1、A2…，每组孔一个字母），
  坐标和规格列在孔表里（相对视图左下角 X0 Y0）。孔表放不下时先让出轴测图，仍放不下报 D018，改用更大图幅。
- `hole_table: true` 强制使用，`false` 关闭。
- `sections: "none"` 会报 D015 提醒；D016 表示剖视图没生成，看 oplog。
