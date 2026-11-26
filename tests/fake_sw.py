"""假 SolidWorks COM 对象：在任意平台上把 SW 执行器完整跑一遍。

它不验证几何，只抓"Python 层"的问题：编译计划与执行器处理器不匹配、参数名写错、
COM 方法参数个数不对、返回值处理出错等。真实效果仍需在 Windows + SolidWorks 上验证。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

# 已核对 SOLIDWORKS API 帮助的参数个数（实参个数，含可选参数）
ARITY = {
    "FeatureExtrusion3": 23, "FeatureCut4": 27, "NewDocument": 4, "OpenDoc6": 6, "SaveAs3": 3,
    "SelectByID2": 9, "SelectByRay": 11, "CreateCircleByRadius": 4, "CreateCenterRectangle": 6,
    "CreateSketchSlot": 14, "AddComponent5": 8, "AutoDimension": 5, "CreateDrawViewFromModelView3": 5,
    "SetUserPreferenceInteger": 3, "Add3": 4, "InsertSketch": 1, "CreateLine": 6,
    "SketchManager.CreatePoint": 3, "GetMathUtility().CreatePoint": 1,
    "CreateFillet": 2, "ReplaceComponents2": 5, "SetSystemValue3": 3, "Save3": 3, "GetDocumentDependencies2": 4, "FeatureRevolve2": 20, "InsertWeldmentFeature": 0, "InsertWeldmentTable": 6, "CreateCenterLine": 6, "ForceRebuild3": 1, "InsertNote": 1, "ClearSelection2": 1,
    "InsertBomTable4": 10, "InsertBOMBalloon2": 6, "MoveRow": 3, "SetHeader": 2, "GetComponents2": 2,
    "SetColumnTitle2": 3, "SetColumnCustomProperty": 2, "AutoBalloon5": 1, "Text": 2, "Text2": 3,
    "InsertWeldSymbol3": 0, "CreateSectionViewAt5": 7, "SetText": 6, "SetPeripheral": 1, "SetProcess": 2,
}


class ArityError(AssertionError):
    pass


class Fake:
    """任意属性/调用都返回 Fake；少数方法返回执行器需要的具体值。"""

    def __init__(self, root: "FakeSw", path: str = "sw"):
        object.__setattr__(self, "_root", root)
        object.__setattr__(self, "_path", path)

    def __getattr__(self, name: str):
        root = self._root
        special = {"ArrayData": [0.0, 0.0, 0.0], "Mass": 1.234, "Position": [0.1, 0.1], "Name": "V1",
                   "Volume": 0.0, "Components": [], "Name2": "C1", "RowCount": 0, "ColumnCount": 6,
                   "ReferencedConfiguration": "Default"}
        if name in special:
            return special[name]
        return FakeMethod(root, name, self._path + "." + name)

    def __setattr__(self, name, value):
        self._root.sets.append((self._path + "." + name, value))

    def __bool__(self):
        return True


class FakeMethod(Fake):
    def __init__(self, root, name, path):
        super().__init__(root, path)
        object.__setattr__(self, "_name", name)

    def __call__(self, *args):
        root, name = self._root, self._name
        key = next((k for k in ARITY if "." in k and self._path.endswith(k)), name)
        if key in ARITY and len(args) != ARITY[key]:
            raise ArityError(f"{key} expects {ARITY[key]} args, got {len(args)}")
        root.calls.append((name, args))
        if name == "SaveAs3":
            Path(args[0]).write_text("fake", encoding="utf-8")
            return 0
        if name == "FirstFeature":
            return root.feature_chain()
        if name == "GetFirstView":
            return root.view_chain()
        ret = {"ForceRebuild3": True, "GetErrorCode2": (0, False), "GetTypeName2": "RefPlane",
               "GetPartBox": [0, 0, 0, 0.1, 0.1, 0.01], "GetSize": (0, 0.42, 0.297),
               "GetInterferences": [], "GetDisplayDimensions": [1, 2, 3], "CreateCenterRectangle": [1, 2, 3, 4],
               "GetTitle": "doc", "RevisionNumber": "32.1.0", "SetUserPreferenceInteger": True,
               "Select2": True, "Add2": 0, "GetCount": 0, "GetNames": None, "SetSystemValue3": 0,
               "GetDocumentDependencies2": [], "GetType": 0, "AutoDimension": 0, "GetMaterialPropertyName2": ("6061-T6 (SS)", "SOLIDWORKS Materials")}
        if name in ret:
            return ret[name]
        return Fake(root, self._path + "()")


class _Chain(Fake):
    def __init__(self, root, items, i):
        super().__init__(root, f"chain[{i}]")
        object.__setattr__(self, "_items", items)
        object.__setattr__(self, "_i", i)

    def __getattr__(self, name):
        if name in ("GetNextFeature", "GetNextView"):
            nxt = self._i + 1
            return lambda: _Chain(self._root, self._items, nxt) if nxt < self._items else None
        if name == "GetTypeName2":
            return lambda: "RefPlane"
        if name == "Name":
            return f"F{self._i}"
        if name == "GetFirstDisplayDimension":
            return lambda: _Dim(self._root, f"D1@F{self._i}")
        if name == "GetNextDisplayDimension":
            return lambda dd: None
        return super().__getattr__(name)


class _Dim(Fake):
    """显示尺寸 + 尺寸（GetDimension2 返回自身）。"""
    def __init__(self, root, full):
        super().__init__(root, "dim")
        object.__setattr__(self, "_full", full)

    def __getattr__(self, name):
        if name == "GetDimension2":
            return lambda i: self
        if name == "FullName":
            return self._full
        if name == "SystemValue":
            return 0.01
        if name in ("Type2", "DrivenState"):
            return 0
        return super().__getattr__(name)


class FakeSw(Fake):
    def __init__(self):
        object.__setattr__(self, "calls", [])
        object.__setattr__(self, "sets", [])
        super().__init__(self, "sw")

    def feature_chain(self):
        return _Chain(self, 5, 0)

    def view_chain(self):
        return _Chain(self, 3, 0)


class FakeConstants:
    swAutodimStatusSuccess = 0

    def __getattr__(self, name):
        return (hash(name) & 0xFFFF) | 1


def install_fake_win32() -> None:
    """注入假 pythoncom / win32com 模块（非 Windows 平台）。"""
    if "pythoncom" in sys.modules and not getattr(sys.modules["pythoncom"], "_fake", False):
        return
    pc = types.ModuleType("pythoncom")
    pc._fake = True
    for k in ("VT_DISPATCH", "VT_ARRAY", "VT_R8", "VT_BSTR", "VT_I4", "VT_BOOL", "VT_EMPTY"):
        setattr(pc, k, 1)
    w = types.ModuleType("win32com")
    wc = types.ModuleType("win32com.client")
    wc.VARIANT = lambda vt, value: value
    wc.Dispatch = lambda *a: FakeSw()
    wc.CastTo = lambda obj, iface: obj
    w.client = wc
    sys.modules.update({"pythoncom": pc, "win32com": w, "win32com.client": wc})
