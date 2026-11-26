"""假 creopyson 与真实 API 一致（签名表由 creopyson 0.7.8 源码生成：Client 方法名 → 参数名，去掉 client）。"""
import inspect
import json
from pathlib import Path

import fake_creopyson as fc
import test_creo_edit as te

REAL = json.loads((Path(__file__).parent / "creopyson_0.7.8_sigs.json").read_text(encoding="utf-8"))


def test_drawing_fake_matches_real_api():
    for name, params in fc.SIGS.items():
        assert name in REAL, name
        assert set(params) <= set(REAL[name]), (name, params, REAL[name])


def test_edit_fake_matches_real_api():
    for name, fn in inspect.getmembers(te.FakeCreoClient, inspect.isfunction):
        if name.startswith("_") and name != "_creoson_post":
            continue
        ps = [p for p in inspect.signature(fn).parameters if p != "self"]
        assert name in REAL, name
        assert set(ps) <= set(REAL[name]), (name, ps, REAL[name])


def test_ext_call_signature():
    assert inspect.signature(fc.FakeCreoClient._creoson_post).parameters.keys() >= {"command", "function", "data"}
