"""cmd_assembly.cpp 与 docs/CREO_TOOLKIT.md §3.4 的一致性检查（不依赖 TOOLKIT，纯文本比对）。"""
import re
from pathlib import Path

from test_toolkit_drawing_cmds import _func_body, _keys_read

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "CREO_TOOLKIT.md"
SRC = ROOT / "creo_toolkit" / "src" / "cmd_assembly.cpp"
COMMON_KEYS = {"asm"}
NESTED = {"ref", "target", "target_ref"}  # csys 对象内部的键，不从顶层 args 读取


def _doc_cmds() -> dict[str, set[str]]:
    sec = DOC.read_text(encoding="utf-8").split("### 3.4", 1)[1].split("\n## ", 1)[0]
    out = {}
    for line in sec.splitlines():
        m = re.match(r"\|\s*`(asm\.\w+)`\s*\|(.*)\|\s*$", line)
        if m:
            keys = set(re.findall(r"`([A-Za-z_]\w*)`", m.group(2).split("→")[0]))
            out[m.group(1)] = {k for k in keys if k.islower()}
    return out


def _registered() -> dict[str, str]:
    return dict(re.findall(r'register_cmd\(\s*"(asm\.\w+)"\s*,\s*(\w+)\s*\)', SRC.read_text(encoding="utf-8")))


def test_all_commands_registered():
    assert set(_doc_cmds()) == set(_registered()) == {"asm.new", "asm.add_component", "asm.interference"}


def test_arg_keys_match_doc():
    src, doc = SRC.read_text(encoding="utf-8"), _doc_cmds()
    for cmd, fn in _registered().items():
        used = _keys_read(_func_body(src, fn)) - COMMON_KEYS
        assert used <= doc[cmd], f"{cmd} 读取了文档未列出的键: {sorted(used - doc[cmd])}"
        assert doc[cmd] - NESTED <= used, f"{cmd} 文档参数未被读取: {sorted(doc[cmd] - NESTED - used)}"


def test_wired_into_main_and_mapping():
    root = ROOT / "creo_toolkit" / "src"
    assert "register_assembly_cmds();" in (root / "main.cpp").read_text(encoding="utf-8")
    assert "void register_assembly_cmds();" in (root / "registry.h").read_text(encoding="utf-8")
    assert "{{1, 0, 0}, {0, 0, 1}, {0, -1, 0}}" in SRC.read_text(encoding="utf-8")  # IR → Creo (x, z, −y)
