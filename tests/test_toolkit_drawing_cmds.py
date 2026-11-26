"""cmd_drawing*.cpp 与 docs/CREO_TOOLKIT.md §3.3 的一致性检查（不依赖 TOOLKIT，纯文本比对）。"""
import glob
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "CREO_TOOLKIT.md"
SRC = sorted(glob.glob(str(ROOT / "creo_toolkit" / "src" / "cmd_drawing*.cpp")))

COMMON_KEYS = {"drawing"}  # 所有出图命令的公共键


def _doc_cmds() -> dict[str, set[str]]:
    """解析文档 §3.3 表：命令名 → args 列里的反引号标识符集合。"""
    text = DOC.read_text(encoding="utf-8")
    sec = text.split("### 3.3", 1)[1].split("\n## ", 1)[0]
    out: dict[str, set[str]] = {}
    for line in sec.splitlines():
        m = re.match(r"\|\s*`(drw\.\w+)`\s*\|(.*)\|\s*$", line)
        if not m:
            continue
        args_col = m.group(2).split("→")[0]  # → 之后是说明/返回值
        out[m.group(1)] = set(re.findall(r"`([A-Za-z_]\w*)`", args_col))
    return out


def _source() -> str:
    assert SRC, "找不到 creo_toolkit/src/cmd_drawing*.cpp"
    return "\n".join(Path(p).read_text(encoding="utf-8") for p in SRC)


def _registered(src: str) -> dict[str, str]:
    return dict(re.findall(r'register_cmd\(\s*"(drw\.\w+)"\s*,\s*(\w+)\s*\)', src))


def _func_body(src: str, name: str) -> str:
    m = re.search(r"\bjson\s+" + re.escape(name) + r"\s*\(\s*const\s+json&\s+a\s*\)\s*\{", src)
    assert m, f"找不到处理函数 {name}"
    i, depth = m.end(), 1
    while depth:
        c = src[i]
        depth += (c == "{") - (c == "}")
        i += 1
    return src[m.end():i]


def _keys_read(body: str) -> set[str]:
    keys = set(re.findall(r'\b(?:arg_\w+|has)\(\s*a\s*,\s*"(\w+)"', body))
    keys |= set(re.findall(r'\ba\s*(?:\[|\.at\(|\.value\()\s*"(\w+)"', body))
    return keys


def test_doc_has_commands():
    assert len(_doc_cmds()) == 12


def test_all_commands_registered():
    reg = _registered(_source())
    missing = set(_doc_cmds()) - set(reg)
    assert not missing, f"未注册的命令: {sorted(missing)}"
    extra = set(reg) - set(_doc_cmds())
    assert not extra, f"文档未定义的命令: {sorted(extra)}"


def test_arg_keys_within_doc():
    src, reg, doc = _source(), _registered(_source()), _doc_cmds()
    for cmd, fn in reg.items():
        used = _keys_read(_func_body(src, fn)) - COMMON_KEYS
        extra = used - doc[cmd]
        assert not extra, f"{cmd} 读取了文档 args 列未列出的键: {sorted(extra)}"


def test_required_keys_are_read():
    """文档列出的键至少被读取（防止漏实现某个参数）；readback 无参数。"""
    src, reg, doc = _source(), _registered(_source()), _doc_cmds()
    for cmd, fn in reg.items():
        if cmd == "drw.readback":
            continue
        used = _keys_read(_func_body(src, fn))
        unused = doc[cmd] - used
        assert not unused, f"{cmd} 文档中的参数未被读取: {sorted(unused)}"
