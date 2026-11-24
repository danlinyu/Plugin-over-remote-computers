"""End-to-end stdio JSON-RPC protocol tests for the Remote Fleet MCP server."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class TestMCPServerStdio(unittest.TestCase):
    def test_mcp_ndjson_and_content_length_framing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg_dir = tmp_path / "cfg"
            cfg_dir.mkdir()
            project_root = tmp_path / "projects"
            project_root.mkdir()

            cfg_file = cfg_dir / "fleet.json"
            cfg_file.write_text(
                json.dumps(
                    {
                        "prefer_lan": True,
                        "auto_discover_tailscale": False,
                        "fleet_mount_dir": str(tmp_path / "Fleet"),
                        "machines": [
                            {
                                "id": "mac-mini",
                                "name": "Mac Mini",
                                "lan_host": "localhost",
                                "os": "macos",
                                "default_roots": [str(project_root)],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            env = os.environ.copy()
            env["PYTHONPATH"] = str(REPO_ROOT / "src")
            env["REMOTE_FLEET_CONFIG_DIR"] = str(cfg_dir)
            env["REMOTE_FLEET_CONFIG"] = str(cfg_file)
            env["REMOTE_FLEET_SELF"] = "mac-mini"

            proc = subprocess.Popen(
                [sys.executable, "-m", "remote_fleet.mcp_server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            assert proc.stdin is not None and proc.stdout is not None

            def send_ndjson(payload: dict) -> dict:
                proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
                proc.stdin.flush()
                line = proc.stdout.readline()
                return json.loads(line.decode("utf-8"))

            try:
                # 1. Initialize (NDJSON framing)
                init_resp = send_ndjson(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2025-03-26", "capabilities": {}},
                    }
                )
                self.assertEqual(init_resp["result"]["protocolVersion"], "2025-03-26")
                self.assertEqual(init_resp["result"]["serverInfo"]["name"], "remote-fleet")

                # Notification (must NOT emit a response)
                proc.stdin.write(
                    (json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode("utf-8")
                )
                proc.stdin.flush()

                # 2. List tools
                tools_resp = send_ndjson({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                tool_names = {t["name"] for t in tools_resp["result"]["tools"]}
                self.assertIn("fleet_list_machines", tool_names)
                self.assertIn("fleet_browse_folders", tool_names)
                self.assertIn("fleet_select_project", tool_names)
                self.assertIn("fleet_create_project", tool_names)
                self.assertIn("fleet_run_command", tool_names)

                # 3. Create project via MCP tool call
                create_resp = send_ndjson(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "fleet_create_project",
                            "arguments": {
                                "machine": "mac-mini",
                                "path": str(project_root / "mcp-demo"),
                                "init_git": True,
                                "template": "python",
                            },
                        },
                    }
                )
                self.assertFalse(create_resp["result"]["isError"])
                body = json.loads(create_resp["result"]["content"][0]["text"])
                self.assertTrue(body["ok"])

                # 4. Write + run command via Content-Length LSP framing to verify dual-framing support
                lsp_req = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {
                            "name": "fleet_run_command",
                            "arguments": {"command": "echo hello-from-mcp"},
                        },
                    }
                ).encode("utf-8")
                proc.stdin.write(f"Content-Length: {len(lsp_req)}\r\n\r\n".encode("ascii") + lsp_req)
                proc.stdin.flush()

                hdr = proc.stdout.readline().decode("ascii")
                self.assertTrue(hdr.lower().startswith("content-length:"))
                length = int(hdr.split(":", 1)[1].strip())
                blank = proc.stdout.readline()
                self.assertIn(blank, (b"\r\n", b"\n"))
                raw_resp = proc.stdout.read(length)
                cmd_resp = json.loads(raw_resp.decode("utf-8"))
                cmd_result = json.loads(cmd_resp["result"]["content"][0]["text"])
                self.assertEqual(cmd_result["stdout"].strip(), "hello-from-mcp")
            finally:
                proc.stdin.close()
                proc.stdout.close()
                if proc.stderr:
                    proc.stderr.close()
                proc.wait(timeout=5)

    def test_mcp_auto_reload_mounts_reporting_and_cwd_resolution(self) -> None:
        from unittest.mock import patch

        from remote_fleet.config import save_mounts_registry
        from remote_fleet.mcp_server import RemoteFleetMCPServer

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg_dir = tmp_path / "cfg"
            cfg_dir.mkdir()
            cfg_file = cfg_dir / "fleet.json"
            proj_dir = tmp_path / "proj"
            sub_dir = proj_dir / "sub"
            sub_dir.mkdir(parents=True)

            cfg_file.write_text(
                json.dumps(
                    {
                        "prefer_lan": True,
                        "auto_discover_tailscale": False,
                        "fleet_mount_dir": str(tmp_path / "Fleet"),
                        "machines": [
                            {"id": "mac-mini", "name": "Mac Mini", "lan_host": "localhost", "os": "macos"},
                            {"id": "pc", "name": "PC", "lan_host": "192.168.68.70", "os": "windows"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            old_env = {k: os.environ.get(k) for k in ("REMOTE_FLEET_CONFIG_DIR", "REMOTE_FLEET_CONFIG", "REMOTE_FLEET_SELF")}
            os.environ["REMOTE_FLEET_CONFIG_DIR"] = str(cfg_dir)
            os.environ["REMOTE_FLEET_CONFIG"] = str(cfg_file)
            os.environ["REMOTE_FLEET_SELF"] = "mac-mini"
            try:
                save_mounts_registry(
                    {
                        str(tmp_path / "RemoteComputers" / "pc"): {"machine_id": "pc", "remote_root": "C:/Users/danli", "container_only": "1"},
                        str(tmp_path / "RemoteComputers" / "pc" / "C"): {"machine_id": "pc", "remote_root": "C:/"},
                    }
                )
                srv = RemoteFleetMCPServer()

                # 1. `fleet_list_machines` reports live mounts (skipping container_only) and works with `include_load=True`
                res1 = srv.call_tool("fleet_list_machines", {"probe": False})
                payload1 = json.loads(res1["content"][0]["text"])
                by_id = {m["id"]: m for m in payload1["machines"]}
                self.assertIn("pc", by_id)
                self.assertEqual(len(by_id["pc"]["mounts"]), 1)
                self.assertEqual(by_id["pc"]["mounts"][0]["remote_root"], "C:/")

                # 2. Updating `fleet.json` on disk auto-reloads on the next tool call!
                new_mtime = cfg_file.stat().st_mtime + 2.0
                cfg_file.write_text(
                    json.dumps(
                        {
                            "prefer_lan": True,
                            "auto_discover_tailscale": False,
                            "fleet_mount_dir": str(tmp_path / "Fleet"),
                            "machines": [
                                {"id": "mac-mini", "name": "Mac Mini", "lan_host": "localhost", "os": "macos"},
                                {"id": "spark04", "name": "Spark 04", "lan_host": "spark04.local", "os": "linux"},
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                os.utime(cfg_file, (new_mtime, new_mtime))
                res2 = srv.call_tool("fleet_list_machines", {"probe": False})
                payload2 = json.loads(res2["content"][0]["text"])
                ids2 = [m["id"] for m in payload2["machines"]]
                self.assertIn("spark04", ids2)
                self.assertNotIn("pc", ids2)

                # 3. `_resolve_tool_cwd` preserves 2-char Windows drive root `C:` and joins relative subpaths
                self.assertEqual(RemoteFleetMCPServer._resolve_tool_cwd("C:", "C:/Users/danli"), "C:")
                self.assertEqual(RemoteFleetMCPServer._resolve_tool_cwd("D:\\data", "C:/Users/danli"), "D:\\data")
                self.assertEqual(RemoteFleetMCPServer._resolve_tool_cwd("sub", "/home/u/proj"), "/home/u/proj/sub")

                # 4. `fleet_start_job` resolves relative `cwd` against active project base
                srv.call_tool("fleet_select_project", {"machine": "mac-mini", "path": str(proj_dir), "create_local_link": False})
                captured_req: dict[str, object] = {}
                with patch.object(
                    srv.transport,
                    "run_worker_op",
                    side_effect=lambda m, req, **kw: captured_req.update(req) or {"ok": True, "job_id": "j1"},
                ):
                    jres = srv.call_tool("fleet_start_job", {"command": "echo hi", "cwd": "sub"})
                self.assertFalse(jres["isError"])
                self.assertEqual(captured_req["cwd"], f"{str(proj_dir)}/sub")
            finally:
                for k, v in old_env.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
