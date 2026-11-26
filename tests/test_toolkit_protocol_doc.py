"""docs/CREO_TOOLKIT.md §3.1 与 cmd_session.cpp 的同步检查（不依赖 TOOLKIT / Windows）。"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "CREO_TOOLKIT.md"
SRC = ROOT / "creo_toolkit" / "src" / "cmd_session.cpp"


def _doc_session_cmds() -> list[str]:
    text = DOC.read_text(encoding="utf-8")
    m = re.search(r"^### 3\.1[^\n]*\n(.*?)(?=^### |\Z)", text, re.S | re.M)
    assert m, "文档中找不到 §3.1"
    return re.findall(r"^\|\s*`([A-Za-z_][\w.]*)`\s*\|", m.group(1), re.M)


def _registered_cmds() -> list[str]:
    return re.findall(r'register_cmd\(\s*"([^"]+)"', SRC.read_text(encoding="utf-8"))


def test_doc_lists_session_commands():
    cmds = _doc_session_cmds()
    assert "ping" in cmds and len(cmds) >= 7


def test_every_documented_session_cmd_is_registered():
    registered = set(_registered_cmds())
    missing = [c for c in _doc_session_cmds() if c not in registered]
    assert not missing, f"cmd_session.cpp 未注册文档中的命令: {missing}"


def test_no_undocumented_session_cmd():
    extra = set(_registered_cmds()) - set(_doc_session_cmds())
    assert not extra, f"cmd_session.cpp 注册了文档未列出的命令: {extra}"
