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
  - 键槽 `{"feature": "keyway", "step": 段号, "length": 长度}`：宽、深按 GB/T 1095 自动取，长度取 GB/T 1096 系列；默认居中，`offset` 指定起点。
  - 挡圈槽 `{"feature": "groove", "type": "circlip", "step": 段号, "position": "end"}` 按 GB/T 894 自动取；退刀槽 `type: "undercut"`（默认 2×0.5）。
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
- 铝型材机架用 `cad_build_frame`（外形 + 型材 + 横梁层高），不要逐根手写组件；返回 cut_list（下料清单）。
  机架以 `asm:<name>` 装进总装；附件、紧固件只进 BOM 时用装配的 `bom_extras`。
- 选型先于建模：气缸、伺服+丝杠、同步带模组、受力螺栓先 `cad_calc` 计算（计算书随版本归档），
  按 `selection` 选库里的型号（`library_matches`），按缸径/法兰定安装板孔位；`warnings` 必须向用户说明。
- 出图结果的 `rules.findings` 中 D009（文字重叠）、D010（孔所在面缺视图）要处理：按提示调整 `views` 或 `sheet`。
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
- 当前 spec 支持：板/块类机加工件、阶梯轴、简单钣金（直边折弯）、标准件/外购件装配。
- 不支持：铸件、焊接件、复杂曲面、斜孔、螺纹以外的特殊工艺 → 告诉用户需要人工建模，或提出扩展 spec。
- 发布：`cad_change_commit(revision, description, by)`。改版时 description 写清楚改了什么（如"立柱间距 160→180"），
  平台把版本号写进标题栏、把更改记录写进每张图纸，并在发布目录保存 revisions.json。
