"""cmd_model.cpp 与 docs/CREO_TOOLKIT.md §3.2 的一致性检查（不依赖 TOOLKIT，纯文本比对）。"""
import re
from pathlib import Path

from test_toolkit_drawing_cmds import _func_body, _keys_read

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "CREO_TOOLKIT.md"
SRC = ROOT / "creo_toolkit" / "src" / "cmd_model.cpp"

COMMON_KEYS = {"model", "feature"}
RESERVED = {"feat.pattern_points"}       # 文档标为预留
NOT_IMPLEMENTED = {"feat.cosmetic_thread"}  # 首期返回 PRO_TK_NOT_IMPLEMENTED


def _doc_cmds() -> dict[str, set[str]]:
    text = DOC.read_text(encoding="utf-8")
    sec = text.split("### 3.2", 1)[1].split("### 3.3", 1)[0]
    out: dict[str, set[str]] = {}
    for line in sec.splitlines():
        m = re.match(r"\|\s*`((?:feat|datum)\.\w+)`\s*\|(.*)\|\s*$", line)
        if m:
            out[m.group(1)] = set(re.findall(r"`([A-Za-z_]\w*)`", m.group(2)))
    return out


def _registered() -> dict[str, str]:
    src = SRC.read_text(encoding="utf-8")
    return dict(re.findall(r'register_cmd\(\s*"((?:feat|datum)\.\w+)"\s*,\s*(\w+)\s*\)', src))


def _enum_words(keys: set[str]) -> set[str]:
    """args 列里也写了枚举值（如 `normal`、`TOP`），只保留形如参数名的小写键。"""
    enums = {"normal", "reverse", "both", "add", "cut", "top", "bottom", "front", "back", "left", "right"}
    return {k for k in keys if k.islower() and k not in enums}


def test_all_commands_registered():
    doc, reg = set(_doc_cmds()) - RESERVED, set(_registered())
    assert doc == reg, f"缺: {sorted(doc - reg)}  多: {sorted(reg - doc)}"


def test_arg_keys_match_doc():
    src, doc = SRC.read_text(encoding="utf-8"), _doc_cmds()
    for cmd, fn in _registered().items():
        if cmd in NOT_IMPLEMENTED:
            continue
        used = _keys_read(_func_body(src, fn)) - COMMON_KEYS
        want = _enum_words(doc[cmd])
        assert used <= want, f"{cmd} 读取了文档未列出的键: {sorted(used - want)}"
        assert want <= used, f"{cmd} 文档参数未被读取: {sorted(want - used)}"


def test_coordinate_mapping_documented():
    assert "(x, z, −y)" in DOC.read_text(encoding="utf-8")
    assert "{p[0], p[2], -p[1]}" in SRC.read_text(encoding="utf-8")
