"""假 cadmcp_tk：本地线程起 JSON-lines TCP 服务，按 docs/CREO_TOOLKIT.md §3 的命令表校验请求。

- 未知命令 → UNKNOWN_CMD；args 含文档该命令 args 列未出现的键 → BAD_ARGS（防止 Python 端与协议漂移）。
- 与 C++ 端一致：drw.section kind=local 缺 boundary → BAD_ARGS；ordinate 缺 orient=h|v → BAD_ARGS。
- `fail` 集合中的命令一律返回 ok=false。
"""
from __future__ import annotations

import json
import re
import socket
import threading
from pathlib import Path
from typing import Any

DOC = Path(__file__).resolve().parent.parent / "docs" / "CREO_TOOLKIT.md"
COMMON = {"asm": {"asm"}, "model": {"model", "feature"}, "feat": {"model", "feature"}, "datum": {"model", "feature"},
          "drw": {"drawing"}, "ping": set()}


def doc_schema() -> dict[str, set[str]]:
    text = DOC.read_text(encoding="utf-8").split("## 3. 命令", 1)[1].split("\n## 4", 1)[0]
    out: dict[str, set[str]] = {}
    for line in text.splitlines():
        m = re.match(r"\|\s*`([a-z]+(?:\.\w+)?)`\s*\|(.*)\|\s*$", line)
        if m:
            out[m.group(1)] = set(re.findall(r"`([A-Za-z_]\w*)`", m.group(2).split("→")[0]))
    return out


class FakeToolkit:
    def __init__(self):
        self.schema = doc_schema()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail: set[str] = set()
        self.interference: list[dict[str, Any]] = []  # asm.interference 返回的干涉对
        self._next_id = 100
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self) -> None:
        self.srv.close()

    def cmds(self) -> list[str]:
        return [c for c, _ in self.calls]

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn: socket.socket) -> None:
        buf = b""
        with conn:
            while True:
                try:
                    chunk = conn.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    req = json.loads(line)
                    conn.sendall(json.dumps(self._handle(req), ensure_ascii=False).encode("utf-8") + b"\n")

    def _err(self, rid, code, msg):
        return {"id": rid, "ok": False, "code": code, "error": msg}

    def _handle(self, req: dict[str, Any]) -> dict[str, Any]:
        rid, cmd, args = req["id"], req["cmd"], req.get("args") or {}
        self.calls.append((cmd, args))
        if cmd not in self.schema:
            return self._err(rid, "UNKNOWN_CMD", cmd)
        allowed = self.schema[cmd] | COMMON.get(cmd.split(".")[0], set())
        extra = set(args) - allowed
        if extra:
            return self._err(rid, "BAD_ARGS", f"{cmd}: 文档未定义的参数 {sorted(extra)}")
        if cmd in self.fail:
            return self._err(rid, "PRO_TK_GENERAL_ERROR", f"{cmd} failed")
        if cmd == "drw.section" and args.get("kind") == "local" and len(args.get("boundary") or []) < 3:
            return self._err(rid, "BAD_ARGS", "local 需要 boundary")
        if cmd == "drw.dim" and args.get("kind") == "ordinate" and args.get("orient") not in ("h", "v"):
            return self._err(rid, "BAD_ARGS", "ordinate 须给 orient=h|v")
        return {"id": rid, "ok": True, "result": self._result(cmd, args)}

    def _result(self, cmd: str, a: dict[str, Any]) -> dict[str, Any]:
        if cmd == "ping":
            return {"creo": "10.0.0.0", "toolkit": "fake"}
        if cmd == "asm.new":
            return {"name": a["name"]}
        if cmd == "asm.add_component":
            self._next_id += 1
            return {"id": self._next_id, "placement": "csys" if a.get("csys") else "fixed"}
        if cmd == "asm.interference":
            return {"pairs": list(self.interference)}
        if cmd == "model.new_part":
            return {"name": a["name"]}
        if cmd == "model.regenerate":
            return {"status": "ok", "failed": []}
        if cmd == "model.save":
            return {"path": str(Path(a.get("dir") or ".") / f"{a['name'].lower()}.prt")}
        if cmd == "model.feature_status":
            return {"features": [{"name": x.get("feature"), "id": i, "type": c, "status": "active"}
                                 for i, (c, x) in enumerate(self.calls) if c.startswith(("feat.", "datum."))]}
        if cmd == "feat.cosmetic_thread":  # 每个孔一个螺纹特征；假服务按 1 个
            self._next_id += 1
            return {"id": self._next_id, "features": [self._next_id]}
        if cmd.startswith(("feat.", "datum.")):
            self._next_id += 1
            return {"id": self._next_id}
        if cmd == "drw.readback":
            n = lambda c: sum(1 for k, _ in self.calls if k == c)  # noqa: E731
            return {"dims": n("drw.dim"), "notes": n("drw.note"), "symbols": n("drw.datum") + n("drw.gtol")
                    + n("drw.surface_finish"), "views": [], "tables": [{"rows": a2.get("rows", [])}
                                                                        for k, a2 in self.calls if k == "drw.table"]}
        return {}
