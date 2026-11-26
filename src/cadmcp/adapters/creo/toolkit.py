"""cadmcp_tk（Creo TOOLKIT 异步程序）的 JSON-lines 客户端，协议见 docs/CREO_TOOLKIT.md §2。

每行一个 JSON；请求 {"id","cmd","args"}，应答 {"id","ok","result"|"error","code"}。
单线程同步调用：发一条、读到 id 相同的应答为止（丢弃 id 不符的残留应答）。
"""
from __future__ import annotations

import json
import socket
from typing import Any

from ...errors import TARGET_UNAVAILABLE, CadError

START_HINT = ("先启动 Creo，再在同一台机器运行 `cadmcp_tk.exe --port 9057`（TOOLKIT 异步程序，"
              "编译与部署见 creo_toolkit/README.md、docs/CREO_TOOLKIT.md §5）；"
              "并确认 config.yaml 中 creo 目标的 toolkit.host/port 正确")


class ToolkitError(Exception):
    """服务端返回 ok=false。code 为 ProError 名或 BAD_ARGS / UNKNOWN_CMD / EXCEPTION。"""

    def __init__(self, code: str, message: str, cmd: str = ""):
        super().__init__(f"{cmd + ': ' if cmd else ''}{code}: {message}")
        self.code, self.message, self.cmd = code, message, cmd


class ToolkitClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 9057, timeout: float = 30):
        self.host, self.port, self.timeout = host, port, timeout
        self._sock: socket.socket | None = None
        self._buf = b""
        self._id = 0

    def connect(self) -> "ToolkitClient":
        if self._sock is not None:
            return self
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self._sock.settimeout(self.timeout)
        except OSError as e:
            self._sock = None
            raise CadError(TARGET_UNAVAILABLE, f"cannot reach cadmcp_tk at {self.host}:{self.port}: {e}",
                           hint=START_HINT) from None
        return self

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
                self._buf = b""

    def __enter__(self) -> "ToolkitClient":
        return self.connect()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _readline(self) -> bytes:
        assert self._sock is not None
        while b"\n" not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("cadmcp_tk closed the connection")
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return line

    def call(self, cmd: str, **args: Any) -> dict[str, Any]:
        if self._sock is None:
            self.connect()
        self._id += 1
        rid = self._id
        line = json.dumps({"id": rid, "cmd": cmd, "args": args}, ensure_ascii=False, separators=(",", ":"))
        try:
            self._sock.sendall(line.encode("utf-8") + b"\n")  # type: ignore[union-attr]
            while True:
                resp = json.loads(self._readline().decode("utf-8"))
                if resp.get("id") == rid:
                    break
        except (OSError, ConnectionError) as e:
            self.close()
            raise CadError(TARGET_UNAVAILABLE, f"cadmcp_tk connection lost during {cmd}: {e}",
                           hint=START_HINT) from None
        if not resp.get("ok"):
            raise ToolkitError(str(resp.get("code", "EXCEPTION")), str(resp.get("error", "")), cmd)
        return resp.get("result") or {}


# 失败只记警告、不中断建模的命令（装饰螺纹首期服务端未实现；几何不受影响）
TK_NON_CRITICAL = {"feat.cosmetic_thread", "model.param_set"}


def run_tk_ops(tk: "ToolkitClient", ops: list, out_dir, name: str) -> dict[str, Any]:
    """逐条执行 `tk.<cmd>` 计划（compile_part_tk 的输出）；oplog 格式与其他执行器一致 {i, op, args, ok, error, ms}。
    返回 {"status": ok|warnings|errors, "log", "warnings", "readback": {regen, features, path}}。"""
    import time
    import traceback
    from pathlib import Path

    out_dir = Path(out_dir)
    log: list[dict[str, Any]] = []
    warnings: list[str] = []
    readback: dict[str, Any] = {}
    status = "ok"

    def sub(v: Any) -> Any:
        if v == "$out_dir":
            return str(out_dir)
        if isinstance(v, dict):
            return {k: sub(x) for k, x in v.items()}
        return v

    for i, op in enumerate(ops):
        t0 = time.perf_counter()
        rec: dict[str, Any] = {"i": i, "op": op.op, "args": op.args}
        cmd = op.op.removeprefix("tk.")
        try:
            if not op.op.startswith("tk."):
                raise RuntimeError(f"unknown op {op.op!r}")
            res = tk.call(cmd, **{k: sub(v) for k, v in op.args.items()})
            rec.update(ok=True, result=res)
            if cmd == "model.regenerate":
                readback["regen"] = res
            elif cmd == "model.feature_status":
                readback["features"] = res.get("features", [])
            elif cmd == "model.save":
                readback["path"] = res.get("path")
            elif cmd == "asm.interference":  # 与 SW 执行器回读同形：{components, volume_mm3}
                readback["interferences"] = [{"components": list(p.get("components", [])),
                                              "volume_mm3": round(float(p.get("volume_mm3", 0)), 1)}
                                             for p in res.get("pairs", [])]
        except Exception as e:  # noqa: BLE001
            rec.update(ok=False, error=f"{type(e).__name__}: {e}", trace=traceback.format_exc(limit=3))
            if cmd in TK_NON_CRITICAL:
                warnings.append(f"{op.op} 失败（不影响几何）：{e}")
                status = "warnings" if status == "ok" else status
            else:
                status = "errors"
                rec["ms"] = round((time.perf_counter() - t0) * 1000)
                log.append(rec)
                break
        rec["ms"] = round((time.perf_counter() - t0) * 1000)
        log.append(rec)
    regen = readback.get("regen") or {}
    if status != "errors" and regen.get("status") == "errors":
        status = "errors"
    if status == "ok" and warnings:
        status = "warnings"
    (out_dir / f"{name}.oplog.json").write_text(json.dumps(log, ensure_ascii=False, indent=1, default=str),
                                                encoding="utf-8")
    return {"status": status, "log": log, "warnings": warnings, "readback": readback}
