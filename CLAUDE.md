# cad-mcp

统一 MCP 平台：AI 写设计规格（spec），平台编译成 SolidWorks / Creo 原生模型和工程图。设计文档见 `docs/DESIGN.md`。

## 开发
- `uv run pytest -q` —— 全部测试在 mock 目标上运行，不需要 CAD。
- `uv run cadmcp doctor` —— 查看可用目标。
- 真实 CAD 适配器只能在 Windows 上运行；实机测试步骤见 `docs/WINDOWS_TEST.md`。
- 分层：`ir/`（spec 与解析，CAD 无关）→ `rules.py` / `detailing.py`（规则与出图规划，CAD 无关）→
  `adapters/`（编译成原生操作并执行）→ `gateway/`（会话、策略、MCP 工具）。
  新的工程知识放在 CAD 无关层，适配器只做翻译。

## 使用 cad MCP 设计零件时
遵循 `skills/mechanical-drawing/SKILL.md`。
