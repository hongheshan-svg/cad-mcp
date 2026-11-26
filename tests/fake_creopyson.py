"""假 creopyson：记录调用，并用与 creopyson 真实 API 一致的签名校验函数名/参数名。

签名依据 creopyson 0.7 的 drawing / parameter / file / interface 模块（记忆整理，未联网核对，
实机核对见 docs/WINDOWS_TEST.md「Creo 出图」）。拼错函数名/参数名会立即抛错。
"""
from __future__ import annotations

import sys
import types
from typing import Any

SIGS = {
    "creo_cd": ["dirname"],
    "creo_set_creo_version": ["version"],
    "connect": [],
    "is_creo_running": [],
    "file_open": ["file_", "dirname", "generic", "display", "activate", "new_window", "regen_force"],
    "file_save": ["file_"],
    "parameter_set": ["name", "value", "file_", "type_", "designate", "encoded", "no_create"],
    "drawing_create": ["template", "model", "drawing", "scale", "display", "activate", "new_window"],
    "drawing_create_gen_view": ["model_view", "point", "drawing", "view", "sheet", "model", "scale",
                                "display_data", "exploded"],
    "drawing_create_proj_view": ["parent_view", "point", "drawing", "view", "sheet", "display_data", "exploded"],
    "drawing_regenerate": ["drawing"],
    "drawing_list_views": ["drawing", "view"],
    "interface_export_pdf": ["file_", "dirname", "filename"],
}


class FakeCreoClient:
    def __init__(self):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail: set[str] = set()  # 让这些方法抛异常；"ext" 表示所有扩展命令
        self.views: list[str] = []

    def __getattr__(self, name):
        if name not in SIGS:
            raise AttributeError(f"creopyson.Client has no method {name!r}")

        def call(*args, **kw):
            allowed = SIGS[name]
            assert len(args) <= len(allowed), f"{name}: too many positional args"
            kw = {**dict(zip(allowed, args)), **kw}
            bad = set(kw) - set(allowed)
            assert not bad, f"{name}: unknown parameter(s) {bad}; real: {allowed}"
            self.calls.append((name, kw))
            if name in self.fail:
                raise RuntimeError(f"{name} failed")
            if name == "is_creo_running":
                return True
            if name == "drawing_create":
                return (kw.get("drawing") or "drw") + ".drw"
            if name in ("drawing_create_gen_view", "drawing_create_proj_view"):
                self.views.append(kw["view"])
            if name == "drawing_list_views":
                return list(self.views)
            return None
        return call

    def _creoson_post(self, request):
        self.calls.append(("ext:" + request["function"], request))
        if "ext" in self.fail:
            raise RuntimeError("Unknown command: cadmcp")
        return None


def install_fake_creopyson() -> FakeCreoClient:
    holder = FakeCreoClient()
    mod = types.ModuleType("creopyson")
    mod.Client = lambda ip_adress="localhost", port=9056: holder  # type: ignore[attr-defined]
    sys.modules["creopyson"] = mod
    return holder
