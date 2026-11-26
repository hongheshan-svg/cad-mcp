"""命令行：cadmcp serve [--stdio|--http HOST:PORT] [--node-config FILE] [--policy FILE] [--workspace DIR]

也可用环境变量 CADMCP_NODE_CONFIG / CADMCP_POLICY / CADMCP_WORKSPACE。"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .gateway.service import CadService


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cadmcp")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the MCP server")
    s.add_argument("--http", metavar="HOST:PORT", help="streamable HTTP instead of stdio")
    s.add_argument("--stdio", action="store_true", help="stdio transport (default)")
    for p in (s, sub.add_parser("doctor", help="check targets and configuration")):
        p.add_argument("--node-config")
        p.add_argument("--policy")
        p.add_argument("--workspace")
    a = ap.parse_args(argv)
    node_config = a.node_config or os.environ.get("CADMCP_NODE_CONFIG") or None
    policy = a.policy or os.environ.get("CADMCP_POLICY") or None
    workspace = a.workspace or os.environ.get("CADMCP_WORKSPACE") or None
    service = CadService.from_config(node_config, policy, workspace)
    if a.cmd == "doctor":
        print(json.dumps({"python": sys.version.split()[0], "platform": sys.platform,
                          "workspace_root": str(service.policy.workspace_root),
                          "release_root": str(service.policy.release_root),
                          **service.list_targets()}, ensure_ascii=False, indent=1))
        return 0 if service.targets else 1
    from .gateway.server import init
    server = init(service)
    if a.http:
        host, _, port = a.http.rpartition(":")
        server.run("streamable-http", host=host or "127.0.0.1", port=int(port))
    else:
        server.run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
