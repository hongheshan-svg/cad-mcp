"""Creo 实机探针（CREOSON 已启动、Creo 已打开）。生成 probe_report_creo.json。

用法：
    uv run python scripts/win/probe_creo.py --version 10.0 --model D:/designs/sample/plate.prt [--port 9056]
--model 用一个真实的零件（会在工作目录另存副本，不修改原文件）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

report: dict = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "steps": []}


def run(name, fn):
    t0 = time.perf_counter()
    rec = {"step": name}
    try:
        rec["result"] = fn()
        rec["ok"] = True
    except Exception as e:  # noqa: BLE001
        rec.update(ok=False, error=f"{type(e).__name__}: {e}", trace=traceback.format_exc(limit=5))
    rec["s"] = round(time.perf_counter() - t0, 2)
    report["steps"].append(rec)
    print(("✅" if rec["ok"] else "❌"), name, "" if rec["ok"] else rec["error"])
    return rec.get("result")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="10.0")
    ap.add_argument("--model", required=True)
    ap.add_argument("--port", type=int, default=9056)
    a = ap.parse_args()
    import creopyson

    c = creopyson.Client(ip_adress="localhost", port=a.port)
    run("1. 连接 CREOSON", c.connect)
    if int(a.version.split(".")[0]) <= 7:
        run("1b. 设置 Creo 7 兼容", lambda: c.creo_set_creo_version(7))
    run("2. Creo 是否运行", c.is_creo_running)
    src = Path(a.model)
    run("3. 读取文件头（用于版本识别）", lambda: src.read_bytes()[:400].decode("latin-1"))
    work = Path(tempfile.mkdtemp(prefix="cadmcp_creo_"))
    dst = work
    shutil.copy2(src, dst / src.name)
    name = src.name.split(".")[0] + "." + src.name.split(".")[1]
    run("4. 切换工作目录到副本", lambda: c.creo_cd(str(dst)))
    run("5. 打开副本", lambda: c.file_open(name, display=True, activate=True))
    params = run("6. 参数列表", lambda: c.parameter_list(file_=name))
    run("7. 尺寸列表（前 30 个）", lambda: (c.dimension_list(file_=name) or [])[:30])
    run("8. 写入测试参数 CADMCP_PROBE", lambda: c.parameter_set("CADMCP_PROBE", "ok", file_=name, type_="STRING"))
    run("9. 再生", lambda: c.file_regenerate(file_=name))
    run("10. 质量属性", lambda: c.file_massprops(file_=name))
    run("11. 保存副本", lambda: c.file_save(file_=name))
    run("12. 导出 STEP", lambda: c.interface_export_file("STEP", file_=name, dirname=str(dst)))
    run("13. 列出会话中的文件", lambda: c.file_list())
    report["work_dir"] = str(dst)
    report["param_count"] = len(params or [])
    out = Path.cwd() / "probe_report_creo.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\n报告：{out}")


if __name__ == "__main__":
    sys.exit(main())
