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
3. 第 3 步的对照记录
4. SolidWorks 语言版本（中文/英文）、SP 号；Creo 维护版本号（如 10.0.2.0）
5. 公司 GB 图框模板文件（`.drwdot`）、材料库名称——用于下一轮把图纸做成公司标准样式
