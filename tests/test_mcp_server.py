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


if __name__ == "__main__":
    unittest.main()
